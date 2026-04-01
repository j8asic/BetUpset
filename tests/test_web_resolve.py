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


def test_check_bet_resolution_with_flipped_kalshi_ids(monkeypatch):
    """Verify resolution is correct when Kalshi market IDs have been flipped.

    Scenario: Kalshi originally listed Spain vs Egypt (Spain=home).
    After matching, the group is reoriented to Egypt vs Spain (Egypt=home).
    The market IDs should have been flipped so that:
      ids["home"] -> Egypt ticker (was "away" in original Kalshi)
      ids["away"] -> Spain ticker (was "home" in original Kalshi)

    If Egypt (home) wins, the Egypt ticker resolves "yes", and since
    ids["home"] now correctly points to the Egypt ticker, outcome="home"
    should match the bet's harmonized view.
    """
    import json

    web_module = load_web_module(monkeypatch)
    check = getattr(web_module, "_check_bet_resolution")

    class FakeKalshi:
        def get_market_result(self, ticker):
            # Egypt wins — the Egypt ticker resolves "yes"
            if ticker == "TICKER-EGY":
                return "yes"
            return "no"

    # After flip_match, the IDs are correctly oriented:
    # "home" -> TICKER-EGY (Egypt = harmonized home)
    # "away" -> TICKER-ESP (Spain = harmonized away)
    bet = {
        "rejected": "away",  # We rejected Spain (away in harmonized view)
        "kalshi_market_id": json.dumps({
            "home": "TICKER-EGY",
            "draw": "TICKER-TIE",
            "away": "TICKER-ESP",
        }),
        "poly_market_id": "",
    }

    result = check(bet, FakeKalshi(), None)
    assert result is not None
    assert result == ("PASS", "home")  # Egypt (home) won, we rejected away → PASS


def test_check_bet_resolution_wrong_without_flip(monkeypatch):
    """Demonstrate the bug scenario: without flipping, resolution is wrong.

    If IDs were NOT flipped, ids["home"] would still point to Spain's ticker.
    When Egypt wins, the code would find "away" resolves to "yes" (since
    Egypt's ticker is at ids["away"]) and compare outcome="away" with
    rejected="away" → incorrectly reports FAIL.
    """
    import json

    web_module = load_web_module(monkeypatch)
    check = getattr(web_module, "_check_bet_resolution")

    class FakeKalshi:
        def get_market_result(self, ticker):
            if ticker == "TICKER-EGY":
                return "yes"
            return "no"

    # WRONG: IDs not flipped — "home" still points to Spain ticker
    bet_without_flip = {
        "rejected": "away",
        "kalshi_market_id": json.dumps({
            "home": "TICKER-ESP",  # Spain (should be away after harmonization)
            "draw": "TICKER-TIE",
            "away": "TICKER-EGY",  # Egypt (should be home after harmonization)
        }),
        "poly_market_id": "",
    }

    result = check(bet_without_flip, FakeKalshi(), None)
    assert result is not None
    # Egypt won → TICKER-EGY at ids["away"] → outcome="away"
    # rejected="away" → FAIL (WRONG! Egypt won, we bet against Spain/away)
    assert result == ("FAIL", "away")  # This is the BUG behavior