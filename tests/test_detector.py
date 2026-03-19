"""
Tests for the arbitrage detector module.
"""

import pytest
from datetime import datetime, timezone

from platform_base import NormalizedMatch, CrossPlatformMatch
from config import StrategyConfig
from detector import detect_opportunity, detect_all_opportunities


def _make_cross_match(
    poly_prices, kalshi_prices,
    home="Liverpool", away="Chelsea",
):
    """Helper to build a CrossPlatformMatch from two sets of prices."""
    poly = NormalizedMatch(
        platform="polymarket",
        platform_market_id='{"home":"p1","draw":"p2","away":"p3"}',
        home_team=home,
        away_team=away,
        kickoff=None,
        league="Premier League",
        prices=poly_prices,
    )
    kalshi = NormalizedMatch(
        platform="kalshi",
        platform_market_id='{"home":"k1","draw":"k2","away":"k3"}',
        home_team=home,
        away_team=away,
        kickoff=None,
        league="Premier League",
        prices=kalshi_prices,
    )
    return CrossPlatformMatch(
        match_key=f"{home.lower()}_vs_{away.lower()}",
        home_team=home.lower(),
        away_team=away.lower(),
        kickoff=None,
        league="Premier League",
        platform_data={"polymarket": poly, "kalshi": kalshi},
    )


class TestDetectOpportunity:
    """Tests for the core detection algorithm."""

    def test_clear_opportunity(self):
        """Low away price + big gap = opportunity."""
        match = _make_cross_match(
            poly_prices={"home": 0.50, "draw": 0.30, "away": 0.08},
            kalshi_prices={"home": 0.52, "draw": 0.28, "away": 0.10},
        )
        config = StrategyConfig(min_gap=0.03, max_reject_prob=0.15, safety_factor=0.60)
        opp = detect_opportunity(match, config, bankroll=10000)

        assert opp is not None
        assert opp.rejected_outcome == "away"
        assert opp.rejected_price == 0.08  # cheapest away across platforms
        assert opp.gap > 0.03
        assert opp.roi_if_win > 0

    def test_no_opportunity_gap_too_small(self):
        """When prices sum close to 1.0, gap is too small."""
        match = _make_cross_match(
            poly_prices={"home": 0.50, "draw": 0.40, "away": 0.12},
            kalshi_prices={"home": 0.48, "draw": 0.42, "away": 0.11},
        )
        config = StrategyConfig(min_gap=0.03)
        opp = detect_opportunity(match, config)
        # gap = 1 - 0.48 - 0.40 = 0.12... actually this might pass
        # Let's make it tighter
        match2 = _make_cross_match(
            poly_prices={"home": 0.50, "draw": 0.46, "away": 0.06},
            kalshi_prices={"home": 0.49, "draw": 0.47, "away": 0.07},
        )
        opp2 = detect_opportunity(match2, config)
        assert opp2 is None  # gap = 1 - 0.46 - 0.49 = 0.05, reject=0.06, safety: 0.06 >= 0.05*0.6=0.03 fails

    def test_no_opportunity_reject_too_high(self):
        """When the rejected outcome is too likely, skip."""
        match = _make_cross_match(
            poly_prices={"home": 0.40, "draw": 0.30, "away": 0.20},
            kalshi_prices={"home": 0.42, "draw": 0.32, "away": 0.18},
        )
        config = StrategyConfig(max_reject_prob=0.15)
        opp = detect_opportunity(match, config)
        assert opp is None  # min away = 0.18 > 0.15

    def test_safety_factor_filter(self):
        """P_reject must be < gap * safety_factor."""
        match = _make_cross_match(
            poly_prices={"home": 0.45, "draw": 0.42, "away": 0.08},
            kalshi_prices={"home": 0.44, "draw": 0.43, "away": 0.09},
        )
        config = StrategyConfig(min_gap=0.03, max_reject_prob=0.15, safety_factor=0.60)
        opp = detect_opportunity(match, config)
        # gap = 1 - 0.42 - 0.44 = 0.14, reject = 0.08
        # 0.08 < 0.14 * 0.60 = 0.084 -> passes
        assert opp is not None

    def test_safety_factor_rejects(self):
        """Safety factor too tight rejects the trade."""
        match = _make_cross_match(
            poly_prices={"home": 0.45, "draw": 0.42, "away": 0.10},
            kalshi_prices={"home": 0.44, "draw": 0.43, "away": 0.09},
        )
        # gap = 1 - 0.42 - 0.44 = 0.14, reject = 0.09
        # 0.09 < 0.14 * 0.30 = 0.042? No, 0.09 >= 0.042 -> rejected
        config = StrategyConfig(min_gap=0.03, max_reject_prob=0.15, safety_factor=0.30)
        opp = detect_opportunity(match, config)
        assert opp is None

    def test_picks_cheapest_across_platforms(self):
        """Should use the cheapest price for each outcome regardless of platform."""
        match = _make_cross_match(
            poly_prices={"home": 0.55, "draw": 0.25, "away": 0.08},
            kalshi_prices={"home": 0.50, "draw": 0.28, "away": 0.10},
        )
        config = StrategyConfig(min_gap=0.03, max_reject_prob=0.15, safety_factor=0.60)
        opp = detect_opportunity(match, config, bankroll=10000)

        assert opp is not None
        # Should pick Kalshi's home (0.50) and Poly's draw (0.25)
        # and reject Poly's away (0.08)
        assert opp.price_a == 0.25  # draw (cheapest covered after rejecting 0.08)
        assert opp.price_b == 0.50  # home
        assert opp.rejected_price == 0.08

    def test_stake_calculation(self):
        """Verify stake = bankroll * bet_fraction."""
        match = _make_cross_match(
            poly_prices={"home": 0.50, "draw": 0.30, "away": 0.05},
            kalshi_prices={"home": 0.52, "draw": 0.28, "away": 0.07},
        )
        config = StrategyConfig(bet_fraction=0.02)
        opp = detect_opportunity(match, config, bankroll=10000)
        assert opp is not None
        assert opp.stake == pytest.approx(200.0)  # 2% of 10000

    def test_only_two_outcomes_priced_returns_none(self):
        """If a platform only has 2 outcomes, and the other has none, skip."""
        poly = NormalizedMatch(
            platform="polymarket",
            platform_market_id="p1",
            home_team="A", away_team="B",
            kickoff=None, league="Test",
            prices={"home": 0.50, "draw": 0.30},
        )
        kalshi = NormalizedMatch(
            platform="kalshi",
            platform_market_id="k1",
            home_team="A", away_team="B",
            kickoff=None, league="Test",
            prices={"home": 0.48, "draw": 0.32},
        )
        match = CrossPlatformMatch(
            match_key="a_vs_b",
            home_team="a", away_team="b",
            kickoff=None, league="Test",
            platform_data={"polymarket": poly, "kalshi": kalshi},
        )
        config = StrategyConfig()
        opp = detect_opportunity(match, config)
        assert opp is None  # no away price available


class TestDetectAllOpportunities:
    """Tests for batch detection."""

    def test_sorts_by_roi(self):
        """Opportunities should be sorted by ROI descending."""
        match1 = _make_cross_match(
            poly_prices={"home": 0.50, "draw": 0.30, "away": 0.05},
            kalshi_prices={"home": 0.52, "draw": 0.28, "away": 0.07},
            home="Team A", away="Team B",
        )
        match2 = _make_cross_match(
            poly_prices={"home": 0.45, "draw": 0.25, "away": 0.04},
            kalshi_prices={"home": 0.47, "draw": 0.23, "away": 0.06},
            home="Team C", away="Team D",
        )
        config = StrategyConfig()
        opps = detect_all_opportunities([match1, match2], config)
        assert len(opps) >= 1
        if len(opps) >= 2:
            assert opps[0].roi_if_win >= opps[1].roi_if_win

    def test_empty_input(self):
        """No matches = no opportunities."""
        config = StrategyConfig()
        assert detect_all_opportunities([], config) == []
