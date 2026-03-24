"""
Tests for the risk manager module.
"""

import pytest
from datetime import datetime, timezone

from platform_base import ArbOpportunity
from config import RiskConfig
from risk import RiskManager


def _make_opp(
    stake=200,
    match_key="liverpool_vs_chelsea",
    kickoff=None,
) -> ArbOpportunity:
    """Helper to create a test opportunity."""
    return ArbOpportunity(
        match_key=match_key,
        home_team="liverpool",
        away_team="chelsea",
        kickoff=kickoff,
        league="EPL",
        outcome_a="home",
        platform_a="polymarket",
        market_id_a="p1",
        price_a=0.45,
        outcome_b="draw",
        platform_b="kalshi",
        market_id_b="k1",
        price_b=0.30,
        rejected_outcome="away",
        rejected_price=0.08,
        rejected_platform="polymarket",
        gap=0.25,
        roi_if_win=0.33,
        shares=267,
        stake=stake,
    )


def _mgr(risk_cfg: RiskConfig, balance: float = 10000.0) -> RiskManager:
    mgr = RiskManager(risk_cfg)
    mgr.update_bankroll(balance)
    return mgr


class TestRiskManager:
    def test_trade_approved_within_limits(self):
        mgr = _mgr(RiskConfig(max_exposure_per_match=500, max_total_exposure=3000))
        decision = mgr.check_trade(_make_opp(stake=200), [])
        assert decision.approved is True


    def test_max_exposure_per_match(self):
        mgr = _mgr(RiskConfig(max_exposure_per_match=500))
        existing = [{"match_key": "liverpool_vs_chelsea", "stake": 400}]
        decision = mgr.check_trade(_make_opp(stake=200), existing)  # 400 + 200 > 500
        assert decision.approved is False

    def test_max_total_exposure(self):
        mgr = _mgr(RiskConfig(max_total_exposure=1000))
        existing = [
            {"match_key": "match_a", "stake": 500},
            {"match_key": "match_b", "stake": 400},
        ]
        decision = mgr.check_trade(_make_opp(stake=200, match_key="match_c"), existing)  # 900 + 200 > 1000
        assert decision.approved is False

    def test_max_matchday_exposure(self):
        mgr = _mgr(RiskConfig(max_matchday_exposure_pct=0.10))
        kickoff = datetime(2026, 3, 20, 20, 0, tzinfo=timezone.utc)
        existing = [{"match_key": "match_a", "stake": 800, "kickoff": kickoff}]
        # 10% of 10000 = 1000, existing 800 + 300 > 1000
        decision = mgr.check_trade(_make_opp(stake=300, match_key="match_b", kickoff=kickoff), existing)
        assert decision.approved is False

    def test_adjusted_stake(self):
        """Risk manager adjusts stake to fit within limits."""
        mgr = _mgr(RiskConfig(max_exposure_per_match=500, max_total_exposure=3000))
        decision = mgr.check_trade(_make_opp(stake=300), [])
        assert decision.approved is True
        assert decision.adjusted_stake == 300  # fits within limits

