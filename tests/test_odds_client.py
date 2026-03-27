"""
Tests for odds_client.OddsApiClient.

Covers:
- Vig removal (Multiplicative method)
- Consensus probability computation from mock bookmaker data
- Match index lookup (exact and fuzzy)
- Cache TTL and stale-data fallback on HTTP error
- Integration: _opp_to_row() score changes with vs without true odds
- Fallback path: odds_source = "market" when no true probs available
"""

import sys
import os
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

# Ensure project root is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from odds_client import OddsApiClient, _kickoff_close


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_client(api_key="test_key", ttl=300):
    return OddsApiClient(api_key=api_key, regions="eu", cache_ttl_seconds=ttl)


def _game_fixture(home="Liverpool", away="Chelsea", bookmakers=None):
    """Return a minimal Odds API game dict."""
    if bookmakers is None:
        bookmakers = [
            {
                "key": "pinnacle",
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": home, "price": 2.10},
                            {"name": away, "price": 3.50},
                            {"name": "Draw", "price": 3.20},
                        ],
                    }
                ],
            }
        ]
    return {
        "id": "abc123",
        "home_team": home,
        "away_team": away,
        "commence_time": "2026-04-01T15:00:00Z",
        "bookmakers": bookmakers,
    }


# ---------------------------------------------------------------------------
# _vig_remove
# ---------------------------------------------------------------------------

class TestVigRemove:
    def test_sums_to_one(self):
        result = OddsApiClient._vig_remove(2.10, 3.20, 3.50)
        total = result["home"] + result["draw"] + result["away"]
        assert abs(total - 1.0) < 1e-6

    def test_proportions_correct(self):
        # With equal prices all probs should be equal
        result = OddsApiClient._vig_remove(3.0, 3.0, 3.0)
        assert abs(result["home"] - result["draw"]) < 1e-6
        assert abs(result["draw"] - result["away"]) < 1e-6

    def test_favourite_gets_highest_prob(self):
        # Home is big favourite (1.3), draw and away are long shots
        result = OddsApiClient._vig_remove(1.30, 6.00, 9.00)
        assert result["home"] > result["draw"] > result["away"]

    def test_invalid_price_returns_empty(self):
        assert OddsApiClient._vig_remove(0.0, 3.0, 3.0) == {}
        assert OddsApiClient._vig_remove(1.0, 3.0, 3.0) == {}  # <= 1.0 is invalid
        assert OddsApiClient._vig_remove(-1.0, 3.0, 3.0) == {}


# ---------------------------------------------------------------------------
# _consensus_probs
# ---------------------------------------------------------------------------

class TestConsensusProbs:
    def test_single_bookmaker(self):
        client = _make_client()
        game = _game_fixture()
        result = client._consensus_probs(game)
        assert result is not None
        assert abs(result["home"] + result["draw"] + result["away"] - 1.0) < 1e-5
        assert result["bookmaker_count"] == 1

    def test_multiple_bookmakers_averaged(self):
        client = _make_client()
        game = _game_fixture(
            bookmakers=[
                {"key": "bk1", "markets": [{"key": "h2h", "outcomes": [
                    {"name": "Liverpool", "price": 2.00},
                    {"name": "Chelsea", "price": 4.00},
                    {"name": "Draw", "price": 3.50},
                ]}]},
                {"key": "bk2", "markets": [{"key": "h2h", "outcomes": [
                    {"name": "Liverpool", "price": 2.20},
                    {"name": "Chelsea", "price": 3.80},
                    {"name": "Draw", "price": 3.40},
                ]}]},
            ]
        )
        result = client._consensus_probs(game)
        assert result is not None
        assert result["bookmaker_count"] == 2

    def test_no_bookmakers_returns_none(self):
        client = _make_client()
        game = _game_fixture(bookmakers=[])
        assert client._consensus_probs(game) is None

    def test_non_h2h_market_ignored(self):
        client = _make_client()
        game = _game_fixture(
            bookmakers=[{"key": "bk1", "markets": [{"key": "spreads", "outcomes": []}]}]
        )
        assert client._consensus_probs(game) is None


# ---------------------------------------------------------------------------
# get_true_probs — exact and fuzzy match
# ---------------------------------------------------------------------------

class TestGetTrueProbs:
    def _client_with_index(self, home, away, probs=None):
        """Build a client with a pre-populated match index."""
        client = _make_client()
        if probs is None:
            probs = {"home": 0.52, "draw": 0.26, "away": 0.22}
        from matching import canonicalize_team
        key = (canonicalize_team(home), canonicalize_team(away))
        client._match_index[key] = {
            **probs,
            "bookmaker_count": 5,
            "kickoff": datetime(2026, 4, 1, 15, 0, tzinfo=timezone.utc),
            "raw_home": home,
            "raw_away": away,
            "odds_source": "bookmaker_consensus",
        }
        return client

    def test_exact_match(self):
        client = self._client_with_index("Liverpool", "Chelsea")
        result = client.get_true_probs("Liverpool", "Chelsea")
        assert result is not None
        assert abs(result["home"] - 0.52) < 1e-4

    def test_fuzzy_match_man_utd(self):
        client = self._client_with_index("Manchester United", "Arsenal")
        result = client.get_true_probs("Man Utd", "Arsenal")
        assert result is not None

    def test_no_match_returns_none(self):
        client = _make_client()
        result = client.get_true_probs("Liverpool", "Chelsea")
        assert result is None

    def test_kickoff_too_far_returns_none(self):
        client = _make_client()
        from matching import canonicalize_team
        key = (canonicalize_team("Liverpool"), canonicalize_team("Chelsea"))
        client._match_index[key] = {
            "home": 0.52, "draw": 0.26, "away": 0.22,
            "bookmaker_count": 3,
            "kickoff": datetime(2026, 4, 10, 15, 0, tzinfo=timezone.utc),
            "raw_home": "Liverpool",
            "raw_away": "Chelsea",
            "odds_source": "bookmaker_consensus",
        }
        # Query with kickoff 3 days earlier — beyond 24h tolerance
        query_kickoff = datetime(2026, 4, 7, 15, 0, tzinfo=timezone.utc)
        result = client.get_true_probs("Liverpool", "Chelsea", kickoff=query_kickoff)
        assert result is None

    def test_none_kickoff_always_matches(self):
        client = self._client_with_index("Liverpool", "Chelsea")
        result = client.get_true_probs("Liverpool", "Chelsea", kickoff=None)
        assert result is not None


# ---------------------------------------------------------------------------
# Cache TTL and HTTP error fallback
# ---------------------------------------------------------------------------

class TestCacheBehavior:
    def test_fresh_cache_skips_http(self):
        client = _make_client(ttl=300)
        # Pre-populate cache as fresh
        client._cache["soccer_epl"] = {"data": [_game_fixture()], "fetched_at": time.monotonic()}
        with patch.object(client._session, "get") as mock_get:
            data = client.fetch_odds("soccer_epl")
        mock_get.assert_not_called()
        assert len(data) == 1

    def test_stale_cache_triggers_http(self):
        client = _make_client(ttl=1)
        client._cache["soccer_epl"] = {"data": [], "fetched_at": time.monotonic() - 2}
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [_game_fixture()]
        with patch.object(client._session, "get", return_value=mock_resp):
            data = client.fetch_odds("soccer_epl")
        assert len(data) == 1

    def test_http_error_returns_stale(self):
        client = _make_client(ttl=1)
        stale_game = _game_fixture(home="Stale", away="Data")
        client._cache["soccer_epl"] = {"data": [stale_game], "fetched_at": time.monotonic() - 2}
        with patch.object(client._session, "get", side_effect=Exception("network error")):
            data = client.fetch_odds("soccer_epl")
        assert len(data) == 1
        assert data[0]["home_team"] == "Stale"

    def test_422_caches_empty_list(self):
        client = _make_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 422
        with patch.object(client._session, "get", return_value=mock_resp):
            data = client.fetch_odds("soccer_nonexistent")
        assert data == []
        assert "soccer_nonexistent" in client._cache


# ---------------------------------------------------------------------------
# Integration: _opp_to_row() score changes with true odds
# ---------------------------------------------------------------------------

class TestOppToRowIntegration:
    """Test that true odds properly affect score in _opp_to_row()."""

    def _make_opp(self, rejected_price=0.20):
        from platform_base import ArbOpportunity
        return ArbOpportunity(
            match_key="liverpool_vs_chelsea_2026-04-01",
            home_team="Liverpool",
            away_team="Chelsea",
            kickoff=datetime(2026, 4, 1, 15, 0, tzinfo=timezone.utc),
            league="EPL",
            outcome_a="home",
            platform_a="polymarket",
            market_id_a="mkt_a",
            price_a=0.55,
            outcome_b="draw",
            platform_b="kalshi",
            market_id_b="mkt_b",
            price_b=0.10,
            rejected_outcome="away",
            rejected_price=rejected_price,
            rejected_platform="polymarket",
        )

    def _make_match(self, opp):
        from platform_base import CrossPlatformMatch, NormalizedMatch
        nm = NormalizedMatch(
            platform="polymarket",
            platform_market_id='{"_event_slug":"liverpool-chelsea"}',
            home_team="Liverpool",
            away_team="Chelsea",
            kickoff=opp.kickoff,
            league="EPL",
            prices={"home": 0.55, "draw": 0.10, "away": 0.20},
            liquidity={"home": 1000, "draw": 500, "away": 200},
            pre_kickoff_prices=None,
        )
        return CrossPlatformMatch(
            match_key=opp.match_key,
            home_team="Liverpool",
            away_team="Chelsea",
            kickoff=opp.kickoff,
            league="EPL",
            platform_data={"polymarket": nm},
        )

    def test_no_odds_uses_market_prob(self):
        from scan_service import _opp_to_row
        opp = self._make_opp(rejected_price=0.20)
        match = self._make_match(opp)
        row = _opp_to_row(opp, match, get_true_probs=None)
        assert row.odds_source == "market"
        assert row.true_rejected_prob == 0.0

    def test_with_odds_updates_score_and_source(self):
        from scan_service import _opp_to_row
        opp = self._make_opp(rejected_price=0.20)
        match = self._make_match(opp)

        def mock_probs(home, away, kickoff=None):
            return {
                "home": 0.55,
                "draw": 0.27,
                "away": 0.18,
                "bookmaker_count": 7,
                "odds_source": "bookmaker_consensus",
            }

        row = _opp_to_row(opp, match, get_true_probs=mock_probs)
        assert row.odds_source == "bookmaker_consensus"
        assert abs(row.true_rejected_prob - 0.18) < 1e-4
        assert abs(row.true_home_prob - 0.55) < 1e-4

    def test_higher_true_win_prob_raises_score(self):
        """When bookmakers agree the rejected outcome is very unlikely, score should increase."""
        from scan_service import _opp_to_row

        opp_market = self._make_opp(rejected_price=0.20)
        match = self._make_match(opp_market)

        row_no_odds = _opp_to_row(opp_market, match, get_true_probs=None)

        # Bookmakers say rejected_outcome (away) is only 5% likely
        def mock_probs_bullish(home, away, kickoff=None):
            return {"home": 0.75, "draw": 0.20, "away": 0.05,
                    "bookmaker_count": 8, "odds_source": "bookmaker_consensus"}

        row_with_odds = _opp_to_row(opp_market, match, get_true_probs=mock_probs_bullish)
        # true_win_prob = 0.95 → much higher prob_for_score → higher score
        assert row_with_odds.score >= row_no_odds.score

    def test_lower_true_win_prob_reduces_score(self):
        """When bookmakers give the rejected outcome a higher prob, score should drop."""
        from scan_service import _opp_to_row

        opp = self._make_opp(rejected_price=0.15)
        match = self._make_match(opp)

        row_no_odds = _opp_to_row(opp, match, get_true_probs=None)

        # Bookmakers say rejected_outcome (away) is actually 30% likely
        def mock_probs_bearish(home, away, kickoff=None):
            return {"home": 0.45, "draw": 0.25, "away": 0.30,
                    "bookmaker_count": 6, "odds_source": "bookmaker_consensus"}

        row_with_odds = _opp_to_row(opp, match, get_true_probs=mock_probs_bearish)
        assert row_with_odds.score <= row_no_odds.score


# ---------------------------------------------------------------------------
# _kickoff_close helper
# ---------------------------------------------------------------------------

class TestKickoffClose:
    def test_none_inputs(self):
        assert _kickoff_close(None, None, 24) is True
        ko = datetime(2026, 4, 1, 15, 0, tzinfo=timezone.utc)
        assert _kickoff_close(None, ko, 24) is True
        assert _kickoff_close(ko, None, 24) is True

    def test_within_tolerance(self):
        a = datetime(2026, 4, 1, 15, 0, tzinfo=timezone.utc)
        b = datetime(2026, 4, 1, 18, 0, tzinfo=timezone.utc)  # 3h apart
        assert _kickoff_close(a, b, 24) is True
        assert _kickoff_close(a, b, 4) is True

    def test_outside_tolerance(self):
        a = datetime(2026, 4, 1, 15, 0, tzinfo=timezone.utc)
        b = datetime(2026, 4, 3, 15, 0, tzinfo=timezone.utc)  # 48h apart
        assert _kickoff_close(a, b, 24) is False
