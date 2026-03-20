import importlib
import sys
from datetime import datetime, timezone

from fastapi.testclient import TestClient

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


def test_resolve_skips_non_pending_bets_via_tracker(monkeypatch, tmp_path):
    web_module = load_web_module(monkeypatch)
    tracker = PortfolioTracker(
        db_path=str(tmp_path / "trades.db"),
        csv_path=str(tmp_path / "opportunities.csv"),
    )
    monkeypatch.setattr(web_module, "_tracker", tracker, raising=False)

    tracker.add_bet({
        "match_key": "settled_bet",
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
        "result": "PASS",
        "placed_at": "2026-03-19T12:00:00",
    })

    checked = []
    monkeypatch.setattr(web_module, "_get_platform_clients", lambda: {"kalshi": object(), "polymarket": object()})
    monkeypatch.setattr(web_module, "_check_bet_resolution", lambda *args: checked.append(args) or None)

    client = TestClient(web_module.app)
    response = client.post("/api/resolve")

    assert response.status_code == 200
    assert response.json()["resolved"] == 0
    assert checked == []


def test_resolve_skips_future_pending_bets(monkeypatch, tmp_path):
    web_module = load_web_module(monkeypatch)
    tracker = PortfolioTracker(
        db_path=str(tmp_path / "trades.db"),
        csv_path=str(tmp_path / "opportunities.csv"),
    )
    monkeypatch.setattr(web_module, "_tracker", tracker, raising=False)

    tracker.add_bet({
        "match_key": "future_bet",
        "date": "2099-03-21",
        "kickoff_iso": "2099-03-21T18:45:00+00:00",
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
    })

    checked = []
    monkeypatch.setattr(web_module, "_get_platform_clients", lambda: {"kalshi": object(), "polymarket": object()})
    monkeypatch.setattr(web_module, "_check_bet_resolution", lambda *args: checked.append(args) or None)

    client = TestClient(web_module.app)
    response = client.post("/api/resolve")

    assert response.status_code == 200
    assert response.json()["resolved"] == 0
    assert checked == []


def test_resolve_checks_pending_bets_that_are_ready(monkeypatch, tmp_path):
    web_module = load_web_module(monkeypatch)
    tracker = PortfolioTracker(
        db_path=str(tmp_path / "trades.db"),
        csv_path=str(tmp_path / "opportunities.csv"),
    )
    monkeypatch.setattr(web_module, "_tracker", tracker, raising=False)

    tracker.add_bet({
        "match_key": "past_bet",
        "date": "2026-03-19",
        "kickoff_iso": "2026-03-19T18:45:00+00:00",
        "home_team": "past",
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
    })

    monkeypatch.setattr(web_module, "_get_platform_clients", lambda: {"kalshi": object(), "polymarket": object()})
    checked = []

    def fake_check_resolution(bet, _kalshi, _poly):
        checked.append(bet["match_key"])
        return None

    monkeypatch.setattr(web_module, "_check_bet_resolution", fake_check_resolution)

    client = TestClient(web_module.app)
    response = client.post("/api/resolve")

    assert response.status_code == 200
    assert response.json()["resolved"] == 0
    assert checked == ["past_bet"]


def test_bet_is_ready_for_resolution_check_uses_kickoff_when_available(monkeypatch):
    web_module = load_web_module(monkeypatch)

    now = datetime(2026, 3, 20, 12, 0, tzinfo=timezone.utc)
    future_bet = {"date": "2026-03-20", "kickoff_iso": "2026-03-20T16:00:00+00:00"}
    past_bet = {"date": "2026-03-20", "kickoff_iso": "2026-03-20T09:00:00+00:00"}

    is_ready = getattr(web_module, "_bet_is_ready_for_resolution_check")

    assert is_ready(future_bet, now=now) is False
    assert is_ready(past_bet, now=now) is True