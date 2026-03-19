import importlib
import time
import sys

from fastapi.testclient import TestClient

from config import ExecutionConfig


def load_web_module(monkeypatch, password=None):
    if password is None:
        monkeypatch.delenv("APP_PASSWORD", raising=False)
    else:
        monkeypatch.setenv("APP_PASSWORD", password)
    monkeypatch.setenv("APP_SESSION_SECRET", "test-session-secret")

    sys.modules.pop("web", None)
    import web

    return importlib.reload(web)


def make_bet_payload(**overrides):
    payload = {
        "match_key": "liverpool_vs_chelsea",
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
        "stake": 40.0,
        "poly_volume": 2500.0,
        "kalshi_volume": 1800.0,
        "polymarket_url": "https://polymarket.com/event/test",
        "kalshi_url": "https://kalshi.com/event/test",
        "poly_market_id": '{"home":"p1","draw":"p2","away":"p3","_clob_tokens":{"home":"ph","draw":"pd","away":"pa"}}',
        "kalshi_market_id": '{"home":"k1","draw":"k2","away":"k3"}',
        "covered_a": "home",
        "covered_b": "draw",
        "platform_a": "polymarket",
        "platform_b": "kalshi",
        "price_a": 0.45,
        "price_b": 0.33,
        "poly_covered_liq": 5000.0,
        "kalshi_covered_liq": 5000.0,
        "scanned_at": time.time(),
    }
    payload.update(overrides)
    return payload


def allow_live_balances(monkeypatch, web_module, **balances):
    monkeypatch.setattr(
        web_module,
        "_fetch_all_balances",
        lambda: {"polymarket": balances.get("polymarket", 1000.0), "kalshi": balances.get("kalshi", 1000.0)},
    )


def test_execute_blocks_dry_run_mode(monkeypatch):
    web_module = load_web_module(monkeypatch)
    client = TestClient(web_module.app)
    allow_live_balances(monkeypatch, web_module)
    monkeypatch.setattr(
        web_module,
        "_get_execution_config",
        lambda: ExecutionConfig(dry_run_only=True, max_stake_per_trade=500, max_scan_age_seconds=120, max_liquidity_fraction=0.05),
    )

    response = client.post("/api/bets/execute", json=make_bet_payload())

    assert response.status_code == 409
    assert "dry_run_only" in response.json()["detail"]


def test_execute_blocks_stake_above_cap(monkeypatch):
    web_module = load_web_module(monkeypatch)
    client = TestClient(web_module.app)
    allow_live_balances(monkeypatch, web_module)
    monkeypatch.setattr(
        web_module,
        "_get_execution_config",
        lambda: ExecutionConfig(dry_run_only=False, max_stake_per_trade=25, max_scan_age_seconds=120, max_liquidity_fraction=0.05),
    )

    response = client.post("/api/bets/execute", json=make_bet_payload(stake=40))

    assert response.status_code == 409
    assert "max_stake_per_trade" in response.json()["detail"]


def test_execute_blocks_stale_scan_data(monkeypatch):
    web_module = load_web_module(monkeypatch)
    client = TestClient(web_module.app)
    allow_live_balances(monkeypatch, web_module)
    monkeypatch.setattr(
        web_module,
        "_get_execution_config",
        lambda: ExecutionConfig(dry_run_only=False, max_stake_per_trade=500, max_scan_age_seconds=30, max_liquidity_fraction=0.05),
    )

    response = client.post(
        "/api/bets/execute",
        json=make_bet_payload(scanned_at=time.time() - 120),
    )

    assert response.status_code == 409
    assert "stale" in response.json()["detail"].lower()


def test_execute_blocks_liquidity_breach(monkeypatch):
    web_module = load_web_module(monkeypatch)
    client = TestClient(web_module.app)
    allow_live_balances(monkeypatch, web_module)
    monkeypatch.setattr(
        web_module,
        "_get_execution_config",
        lambda: ExecutionConfig(dry_run_only=False, max_stake_per_trade=500, max_scan_age_seconds=120, max_liquidity_fraction=0.05),
    )

    response = client.post(
        "/api/bets/execute",
        json=make_bet_payload(stake=20, poly_covered_liq=100, kalshi_covered_liq=100),
    )

    assert response.status_code == 409
    assert "liquidity" in response.json()["detail"].lower()


def test_execute_blocks_when_live_balance_is_too_low(monkeypatch):
    web_module = load_web_module(monkeypatch)
    client = TestClient(web_module.app)
    allow_live_balances(monkeypatch, web_module, polymarket=20.0, kalshi=20.0)
    monkeypatch.setattr(
        web_module,
        "_get_execution_config",
        lambda: ExecutionConfig(dry_run_only=False, max_stake_per_trade=500, max_scan_age_seconds=120, max_liquidity_fraction=0.05),
    )

    response = client.post("/api/bets/execute", json=make_bet_payload(stake=40))

    assert response.status_code == 409
    assert "live balance" in response.json()["detail"].lower()


def test_execute_blocks_when_live_balance_cannot_be_verified(monkeypatch):
    web_module = load_web_module(monkeypatch)
    client = TestClient(web_module.app)
    monkeypatch.setattr(
        web_module,
        "_fetch_all_balances",
        lambda: {"polymarket": None, "kalshi": 1000.0},
    )
    monkeypatch.setattr(
        web_module,
        "_get_execution_config",
        lambda: ExecutionConfig(dry_run_only=False, max_stake_per_trade=500, max_scan_age_seconds=120, max_liquidity_fraction=0.05),
    )

    response = client.post("/api/bets/execute", json=make_bet_payload(stake=40))

    assert response.status_code == 409
    assert "could not verify" in response.json()["detail"].lower()


def test_execute_calls_order_placement_when_checks_pass(monkeypatch):
    web_module = load_web_module(monkeypatch)
    client = TestClient(web_module.app)
    allow_live_balances(monkeypatch, web_module, polymarket=1000.0, kalshi=1000.0)
    monkeypatch.setattr(
        web_module,
        "_get_execution_config",
        lambda: ExecutionConfig(dry_run_only=False, max_stake_per_trade=500, max_scan_age_seconds=120, max_liquidity_fraction=0.05),
    )

    calls = []

    def fake_place_orders(data):
        calls.append(data)
        return {
            "polymarket": {"ok": True, "order_id": "poly-1"},
            "kalshi": {"ok": True, "order_id": "kal-1"},
        }

    monkeypatch.setattr(web_module, "_place_orders", fake_place_orders)

    response = client.post("/api/bets/execute", json=make_bet_payload(stake=40))

    assert response.status_code == 200
    assert len(calls) == 1
    assert response.json()["execution"]["polymarket"]["ok"] is True
    assert response.json()["balances"]["polymarket"] == 1000.0