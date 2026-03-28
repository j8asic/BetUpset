import importlib
import sys
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from platform_base import NormalizedMatch
from tracker import PortfolioTracker


def load_web_module(monkeypatch, password=None):
    if password is None:
        monkeypatch.delenv("APP_PASSWORD", raising=False)
    else:
        monkeypatch.setenv("APP_PASSWORD", password)
    monkeypatch.setenv("APP_SESSION_SECRET", "test-session-secret")

    sys.modules.pop("web", None)
    import web

    return importlib.reload(web)


def test_saved_bet_preserves_kickoff_iso(monkeypatch, tmp_path):
    web_module = load_web_module(monkeypatch)
    client = TestClient(web_module.app)

    tracker = PortfolioTracker(
        db_path=str(tmp_path / "trades.db"),
        csv_path=str(tmp_path / "opportunities.csv"),
    )
    monkeypatch.setattr(web_module, "_tracker", tracker, raising=False)

    response = client.post(
        "/api/bets",
        json={
            "match_key": "liverpool_vs_chelsea_2026-03-19",
            "date": "2026-03-19",
            "kickoff_iso": "2026-03-19T19:45:00+01:00",
            "home_team": "liverpool",
            "away_team": "chelsea",
            "best_home": 0.45,
            "best_draw": 0.33,
            "best_away": 0.20,
            "roi": 0.12,
            "win_prob": 0.84,
            "score": 0.91,
            "rejected": "away",
            "rejected_price": 0.20,
            "profit_if_win": 15.0,
            "loss_if_reject": 100.0,
            "stake": 40.0,
        },
    )

    assert response.status_code == 200

    bets_response = client.get("/api/bets")
    assert bets_response.status_code == 200
    bets = bets_response.json()["bets"]
    assert len(bets) == 1
    assert bets[0]["kickoff_iso"] == "2026-03-19T19:45:00+01:00"


def test_bets_list_backfills_kickoff_from_platform_market_ids(monkeypatch, tmp_path):
    web_module = load_web_module(monkeypatch)
    tracker = PortfolioTracker(
        db_path=str(tmp_path / "trades.db"),
        csv_path=str(tmp_path / "opportunities.csv"),
    )
    monkeypatch.setattr(web_module, "_tracker", tracker, raising=False)

    tracker.add_bet({
        "match_key": "liverpool_vs_chelsea_2026-03-19",
        "date": "2026-03-19",
        "home_team": "liverpool",
        "away_team": "chelsea",
        "best_home": 0.45,
        "best_draw": 0.33,
        "best_away": 0.20,
        "roi": 0.12,
        "win_prob": 0.84,
        "score": 0.91,
        "rejected": "away",
        "rejected_price": 0.20,
        "profit_if_win": 15.0,
        "loss_if_reject": 100.0,
        "result": "PENDING",
        "placed_at": "2026-03-19T12:00:00",
        "poly_market_id": '{"_event_slug":"liverpool-vs-chelsea"}',
        "stake": 40.0,
    })

    class FakePolymarketClient:
        def fetch_soccer_markets(self):
            return [
                NormalizedMatch(
                    platform="polymarket",
                    platform_market_id='{"_event_slug":"liverpool-vs-chelsea"}',
                    home_team="Liverpool",
                    away_team="Chelsea",
                    kickoff=datetime(2026, 3, 19, 18, 45, tzinfo=timezone.utc),
                    league="Premier League",
                    prices={"home": 0.45, "draw": 0.33, "away": 0.20},
                    liquidity={},
                )
            ]

    monkeypatch.setattr(
        web_module,
        "_get_platform_clients",
        lambda: {"polymarket": FakePolymarketClient()},
    )

    client = TestClient(web_module.app)
    response = client.get("/api/bets")

    assert response.status_code == 200
    bets = response.json()["bets"]
    assert bets[0]["kickoff_iso"] == "2026-03-19T18:45:00+00:00"


def test_bets_list_caches_empty_kickoff_lookup(monkeypatch, tmp_path):
    web_module = load_web_module(monkeypatch)
    tracker = PortfolioTracker(
        db_path=str(tmp_path / "trades.db"),
        csv_path=str(tmp_path / "opportunities.csv"),
    )
    monkeypatch.setattr(web_module, "_tracker", tracker, raising=False)

    tracker.add_bet({
        "match_key": "unknown_vs_unknown_2026-03-19",
        "date": "2026-03-19",
        "home_team": "unknown",
        "away_team": "unknown",
        "best_home": 0.45,
        "best_draw": 0.33,
        "best_away": 0.20,
        "roi": 0.12,
        "win_prob": 0.84,
        "score": 0.91,
        "rejected": "away",
        "rejected_price": 0.20,
        "profit_if_win": 15.0,
        "loss_if_reject": 100.0,
        "result": "PENDING",
        "placed_at": "2026-03-19T12:00:00",
        "poly_market_id": '{"_event_slug":"missing-event"}',
        "stake": 40.0,
    })

    class FakePolymarketClient:
        def __init__(self):
            self.calls = 0

        def fetch_soccer_markets(self):
            self.calls += 1
            return []

    fake_client = FakePolymarketClient()
    monkeypatch.setattr(
        web_module,
        "_get_platform_clients",
        lambda: {"polymarket": fake_client},
    )

    client = TestClient(web_module.app)

    first = client.get("/api/bets")
    second = client.get("/api/bets")

    assert first.status_code == 200
    assert second.status_code == 200
    assert fake_client.calls == 1


def test_bets_list_skips_kickoff_backfill_for_resolved_bets(monkeypatch, tmp_path):
    web_module = load_web_module(monkeypatch)
    tracker = PortfolioTracker(
        db_path=str(tmp_path / "trades.db"),
        csv_path=str(tmp_path / "opportunities.csv"),
    )
    monkeypatch.setattr(web_module, "_tracker", tracker, raising=False)

    tracker.add_bet({
        "match_key": "settled_bet",
        "date": "2026-03-19",
        "home_team": "settled",
        "away_team": "team",
        "best_home": 0.45,
        "best_draw": 0.33,
        "best_away": 0.20,
        "roi": 0.12,
        "win_prob": 0.84,
        "score": 0.91,
        "rejected": "away",
        "rejected_price": 0.20,
        "profit_if_win": 15.0,
        "loss_if_reject": 100.0,
        "result": "PASS",
        "placed_at": "2026-03-19T12:00:00",
        "poly_market_id": '{"_event_slug":"settled-event"}',
        "stake": 40.0,
    })

    fake_calls = []
    monkeypatch.setattr(
        web_module,
        "_load_kickoff_indexes",
        lambda: fake_calls.append(True) or {"polymarket": {}, "kalshi": {}},
    )

    client = TestClient(web_module.app)
    response = client.get("/api/bets")

    assert response.status_code == 200
    assert fake_calls == []


def test_bets_list_includes_live_diff_for_pending_bets(monkeypatch, tmp_path):
    web_module = load_web_module(monkeypatch)
    tracker = PortfolioTracker(
        db_path=str(tmp_path / "trades.db"),
        csv_path=str(tmp_path / "opportunities.csv"),
    )
    monkeypatch.setattr(web_module, "_tracker", tracker, raising=False)

    tracker.add_bet({
        "match_key": "egypt_vs_spain_2026-03-28",
        "date": "2026-03-28",
        "home_team": "egypt",
        "away_team": "spain",
        "best_home": 0.45,
        "best_draw": 0.10,
        "best_away": 0.07,
        "roi": 5.0,
        "win_prob": 0.9,
        "score": 9.0,
        "rejected": "home",
        "rejected_price": 0.45,
        "profit_if_win": 34.03,
        "loss_if_reject": 6.97,
        "result": "PENDING",
        "placed_at": "2026-03-28T12:00:00",
        "stake": 6.97,
        "shares": 41,
        "covered_a": "draw",
        "covered_b": "away",
        "platform_a": "polymarket",
        "platform_b": "kalshi",
        "price_a": 0.10,
        "price_b": 0.07,
        "poly_market_id": '{"draw":"p2","_clob_tokens":{"draw":"pd"}}',
        "kalshi_market_id": '{"away":"k3"}',
    })

    class FakePolymarketClient:
        def get_clob_bid_price(self, token_id):
            assert token_id == "pd"
            return 0.12

    class FakeKalshiClient:
        def _get(self, endpoint):
            assert endpoint == "/markets/k3"
            return {"market": {"yes_bid_dollars": 0.09}}

    monkeypatch.setattr(
        web_module,
        "_get_platform_clients",
        lambda: {"polymarket": FakePolymarketClient(), "kalshi": FakeKalshiClient()},
    )

    client = TestClient(web_module.app)
    response = client.get("/api/bets")

    assert response.status_code == 200
    bet = response.json()["bets"][0]
    assert bet["current_exit_value"] == 8.61
    assert bet["current_diff"] == 1.64


def test_sell_endpoint_keeps_bet_when_liquidation_fails(monkeypatch, tmp_path):
    web_module = load_web_module(monkeypatch)
    tracker = PortfolioTracker(
        db_path=str(tmp_path / "trades.db"),
        csv_path=str(tmp_path / "opportunities.csv"),
    )
    monkeypatch.setattr(web_module, "_tracker", tracker, raising=False)

    bet_id = tracker.add_bet({
        "match_key": "sell_test",
        "date": "2026-03-28",
        "home_team": "egypt",
        "away_team": "spain",
        "best_home": 0.45,
        "best_draw": 0.10,
        "best_away": 0.07,
        "roi": 5.0,
        "win_prob": 0.9,
        "score": 9.0,
        "rejected": "home",
        "rejected_price": 0.45,
        "profit_if_win": 34.03,
        "loss_if_reject": 6.97,
        "result": "PENDING",
        "placed_at": "2026-03-28T12:00:00",
        "poly_market_id": '{"draw":"p2","_clob_tokens":{"draw":"pd"}}',
        "stake": 6.97,
    })

    monkeypatch.setattr(
        web_module,
        "_sell_bet_positions",
        lambda bet: {"polymarket_draw": "sell_failed"},
    )

    client = TestClient(web_module.app)
    response = client.post(f"/api/bets/{bet_id}/sell")

    assert response.status_code == 409
    remaining = tracker.get_all_bets()
    assert len(remaining) == 1
    assert remaining[0]["id"] == bet_id


def test_bets_list_skips_kickoff_backfill_for_future_pending_bets(monkeypatch, tmp_path):
    web_module = load_web_module(monkeypatch)
    tracker = PortfolioTracker(
        db_path=str(tmp_path / "trades.db"),
        csv_path=str(tmp_path / "opportunities.csv"),
    )
    monkeypatch.setattr(web_module, "_tracker", tracker, raising=False)

    tracker.add_bet({
        "match_key": "future_bet",
        "date": "2099-03-21",
        "home_team": "future",
        "away_team": "team",
        "best_home": 0.45,
        "best_draw": 0.33,
        "best_away": 0.20,
        "roi": 0.12,
        "win_prob": 0.84,
        "score": 0.91,
        "rejected": "away",
        "rejected_price": 0.20,
        "profit_if_win": 15.0,
        "loss_if_reject": 100.0,
        "result": "PENDING",
        "placed_at": "2026-03-19T12:00:00",
        "poly_market_id": '{"_event_slug":"future-event"}',
        "stake": 40.0,
    })

    fake_calls = []
    monkeypatch.setattr(
        web_module,
        "_load_kickoff_indexes",
        lambda: fake_calls.append(True) or {"polymarket": {}, "kalshi": {}},
    )

    client = TestClient(web_module.app)
    response = client.get("/api/bets")

    assert response.status_code == 200
    assert fake_calls == []