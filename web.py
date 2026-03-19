#!/usr/bin/env python3
"""
BetUpset Web — Mobile-friendly betting simulator.

FastAPI backend serving a single-page HTML frontend with:
  - Left panel:  Scan results (opportunities)
  - Right panel: Placed bets with PASS/FAIL tracking, P&L, balances

Usage:
  python web.py          # Live scan from Polymarket + Kalshi
  python web.py --demo   # Demo mode with simulated data
"""

import asyncio
import hashlib
import json
import os
import secrets
import sys
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from scan_service import MatchRow, run_scan

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).parent
DEMO_MODE = "--demo" in sys.argv
BETS_CSV = BASE_DIR / "bets.csv"
APP_PASSWORD = os.getenv("APP_PASSWORD", "").strip()
APP_SESSION_SECRET = os.getenv("APP_SESSION_SECRET") or (
    hashlib.sha256(APP_PASSWORD.encode("utf-8")).hexdigest()
    if APP_PASSWORD
    else "betupset-dev-session-secret"
)
SESSION_COOKIE_MAX_AGE = 60 * 60 * 24 * 7

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(title="BetUpset")
app.add_middleware(
    SessionMiddleware,
    secret_key=APP_SESSION_SECRET,
    same_site="lax",
    max_age=SESSION_COOKIE_MAX_AGE,
)

# ---------------------------------------------------------------------------
# State: scan cache + tracker
# ---------------------------------------------------------------------------

_scan_cache: list[dict] = []
_scan_total: int = 0
_scan_time: float = 0
_scan_lock = asyncio.Lock()
SCAN_CACHE_TTL = 30  # seconds

_tracker = None


def get_tracker():
    global _tracker
    if _tracker is None:
        from tracker import PortfolioTracker
        _tracker = PortfolioTracker()
        _tracker.migrate_csv_bets(str(BETS_CSV))
    return _tracker


def _is_auth_enabled() -> bool:
    return bool(APP_PASSWORD)


def _is_authenticated(request: Request) -> bool:
    return not _is_auth_enabled() or bool(request.session.get("authenticated"))


def _require_auth(request: Request) -> None:
    if not _is_authenticated(request):
        raise HTTPException(401, "Authentication required")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _match_rows_to_dicts(rows: list[MatchRow]) -> list[dict]:
    return [asdict(r) for r in rows]


def _get_platform_clients():
    """Instantiate platform clients from config for balance/resolution checks."""
    from config import load_config
    from main import initialize_platforms
    config = load_config("config.yaml")
    platforms = initialize_platforms(config)
    clients = {}
    for p in platforms:
        clients[p.name] = p
    return clients


# ---------------------------------------------------------------------------
# API: Auth
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    password: str = ""


@app.get("/api/auth/status")
async def auth_status(request: Request):
    enabled = _is_auth_enabled()
    return {
        "enabled": enabled,
        "authenticated": _is_authenticated(request),
    }


@app.post("/api/auth/login")
async def auth_login(request: Request, req: LoginRequest):
    if not _is_auth_enabled():
        return {"enabled": False, "authenticated": True}

    if not secrets.compare_digest(req.password, APP_PASSWORD):
        request.session.clear()
        raise HTTPException(401, "Invalid password")

    request.session["authenticated"] = True
    return {"enabled": True, "authenticated": True}


@app.post("/api/auth/logout")
async def auth_logout(request: Request):
    request.session.clear()
    return {
        "enabled": _is_auth_enabled(),
        "authenticated": False,
    }


# ---------------------------------------------------------------------------
# API: Scan
# ---------------------------------------------------------------------------

@app.get("/api/scan")
async def scan_get(request: Request, demo: Optional[bool] = None):
    """Return cached scan results, or run initial scan."""
    _require_auth(request)
    global _scan_cache, _scan_total, _scan_time
    use_demo = demo if demo is not None else DEMO_MODE

    if _scan_cache and (time.time() - _scan_time < SCAN_CACHE_TTL):
        return {"matches": _scan_cache, "total_matches": _scan_total, "scanned_at": _scan_time, "cached": True}

    async with _scan_lock:
        if _scan_cache and (time.time() - _scan_time < SCAN_CACHE_TTL):
            return {"matches": _scan_cache, "total_matches": _scan_total, "scanned_at": _scan_time, "cached": True}

        rows, total = await asyncio.to_thread(run_scan, demo=use_demo)
        _scan_cache = _match_rows_to_dicts(rows)
        _scan_total = total
        _scan_time = time.time()
        return {"matches": _scan_cache, "total_matches": _scan_total, "scanned_at": _scan_time, "cached": False}


@app.post("/api/scan")
async def scan_post(request: Request, demo: Optional[bool] = None):
    """Force a fresh rescan."""
    _require_auth(request)
    global _scan_cache, _scan_total, _scan_time
    use_demo = demo if demo is not None else DEMO_MODE

    async with _scan_lock:
        rows, total = await asyncio.to_thread(run_scan, demo=use_demo)
        _scan_cache = _match_rows_to_dicts(rows)
        _scan_total = total
        _scan_time = time.time()
        return {"matches": _scan_cache, "total_matches": _scan_total, "scanned_at": _scan_time, "cached": False}


# ---------------------------------------------------------------------------
# API: Bets
# ---------------------------------------------------------------------------

class PlaceBetRequest(BaseModel):
    match_key: str
    date: str
    home_team: str
    away_team: str
    best_home: float
    best_draw: float
    best_away: float
    roi: float
    win_prob: float
    score: float
    rejected: str
    rejected_price: float
    profit_if_win: float
    loss_if_reject: float
    stake: float = 100.0
    poly_volume: float = 0.0
    kalshi_volume: float = 0.0
    polymarket_url: str = ""
    kalshi_url: str = ""
    poly_market_id: str = ""
    kalshi_market_id: str = ""
    # Per-platform allocation (populated from scan data, used for order placement)
    covered_a: str = ""
    covered_b: str = ""
    platform_a: str = ""
    platform_b: str = ""
    price_a: float = 0.0
    price_b: float = 0.0


@app.get("/api/bets")
async def bets_list(request: Request):
    _require_auth(request)
    tracker = get_tracker()
    bets = tracker.get_all_bets()
    pnl = tracker.get_bets_pnl()
    return {"bets": bets, "pnl": pnl}


def _place_single_order(
    platform: str, outcome: str, price: float, shares: int,
    clients: dict, kalshi_ids: dict, poly_ids: dict,
) -> dict:
    """Place one order on one platform. Returns status dict with 'ok' bool."""
    client = clients.get(platform)
    if not client:
        return {"error": f"no {platform} client", "ok": False}

    if platform == "kalshi":
        ticker = kalshi_ids.get(outcome)
        if not ticker:
            return {"error": f"no ticker for {outcome}", "ok": False}
        price_cents = max(1, min(99, round(price * 100)))
        order_id = client.place_order(ticker, "yes", shares, price_cents)
        return {
            "order_id": order_id, "ticker": ticker,
            "outcome": outcome, "count": shares,
            "price_cents": price_cents, "ok": order_id is not None,
        }

    if platform == "polymarket":
        clob_tokens = poly_ids.get("_clob_tokens", {})
        token_id = clob_tokens.get(outcome)
        if not token_id:
            return {"error": f"no CLOB token for {outcome}", "ok": False}
        order_id = client.place_order(token_id, "BUY", shares * price, price)
        return {
            "order_id": order_id, "token_id": token_id,
            "outcome": outcome, "ok": order_id is not None,
        }

    return {"error": f"unknown platform {platform}", "ok": False}


def _place_orders(bet: dict) -> dict:
    """Place orders on both platforms with auto-rollback on partial fill.

    Flow:
      1. Place order on platform A
      2. Place order on platform B
      3. If B fails but A succeeded → cancel A automatically
    """
    covered_a = bet.get("covered_a", "")
    covered_b = bet.get("covered_b", "")
    platform_a = bet.get("platform_a", "")
    platform_b = bet.get("platform_b", "")
    price_a = bet.get("price_a", 0.0)
    price_b = bet.get("price_b", 0.0)
    stake = bet.get("stake", 0.0)

    if not covered_a or not covered_b or not stake or not (price_a + price_b):
        return {}

    shares = max(1, int(stake / (price_a + price_b)))

    try:
        clients = _get_platform_clients()
    except Exception as e:
        return {"error": str(e)}

    kalshi_ids = {}
    poly_ids = {}
    try:
        kalshi_ids = json.loads(bet.get("kalshi_market_id", "{}") or "{}")
    except (json.JSONDecodeError, TypeError):
        pass
    try:
        poly_ids = json.loads(bet.get("poly_market_id", "{}") or "{}")
    except (json.JSONDecodeError, TypeError):
        pass

    # Place order A
    res_a = _place_single_order(
        platform_a, covered_a, price_a, shares, clients, kalshi_ids, poly_ids,
    )

    # Place order B
    res_b = _place_single_order(
        platform_b, covered_b, price_b, shares, clients, kalshi_ids, poly_ids,
    )

    # Auto-rollback: if one succeeded and the other failed, cancel the successful one
    if res_a.get("ok") and not res_b.get("ok"):
        client_a = clients.get(platform_a)
        if client_a and hasattr(client_a, "cancel_order"):
            cancelled = client_a.cancel_order(res_a["order_id"])
            res_a["cancelled"] = cancelled
            res_a["ok"] = False
            res_a["error"] = "rolled back (other side failed)"

    elif res_b.get("ok") and not res_a.get("ok"):
        client_b = clients.get(platform_b)
        if client_b and hasattr(client_b, "cancel_order"):
            cancelled = client_b.cancel_order(res_b["order_id"])
            res_b["cancelled"] = cancelled
            res_b["ok"] = False
            res_b["error"] = "rolled back (other side failed)"

    return {platform_a: res_a, platform_b: res_b}


class SaveBetRequest(BaseModel):
    """Save a bet to the tracker (after user confirms)."""
    data: dict
    execution: dict = {}


@app.post("/api/bets/execute")
async def bets_execute(request: Request, req: PlaceBetRequest):
    """Try to place orders on both platforms. Does NOT save the bet yet —
    the frontend decides whether to save based on the result."""
    _require_auth(request)
    data = req.model_dump()
    execution = await asyncio.to_thread(_place_orders, data)
    return {"execution": execution}


@app.post("/api/bets")
async def bets_create(request: Request, req: PlaceBetRequest):
    """Save a bet to the tracker (called after user confirms)."""
    _require_auth(request)
    tracker = get_tracker()
    data = req.model_dump()
    data["result"] = "PENDING"
    data["placed_at"] = datetime.now().isoformat(timespec="seconds")
    bet_id = tracker.add_bet(data)
    return {"id": bet_id, "bet": {**data, "id": bet_id}}


@app.patch("/api/bets/{bet_id}/result")
async def bets_toggle_result(request: Request, bet_id: int):
    _require_auth(request)
    tracker = get_tracker()
    bets = tracker.get_all_bets()
    bet = next((b for b in bets if b["id"] == bet_id), None)
    if not bet:
        raise HTTPException(404, "Bet not found")

    cycle = {"PENDING": "PASS", "PASS": "FAIL", "FAIL": "PENDING"}
    new_result = cycle.get(bet["result"], "PENDING")
    tracker.update_bet_result(bet_id, new_result)
    return {"id": bet_id, "result": new_result}


@app.delete("/api/bets/{bet_id}")
async def bets_delete(request: Request, bet_id: int):
    _require_auth(request)
    tracker = get_tracker()
    tracker.delete_bet(bet_id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# API: Balances
# ---------------------------------------------------------------------------

def _fetch_all_balances() -> dict:
    """Blocking: instantiate clients and fetch balances for both platforms."""
    result = {"kalshi": None, "polymarket": None}
    try:
        clients = _get_platform_clients()
        for name, client in clients.items():
            if name in result and hasattr(client, "get_balance"):
                try:
                    result[name] = client.get_balance()
                except Exception:
                    pass
    except Exception:
        pass
    return result


@app.get("/api/balances")
async def balances(request: Request):
    """Fetch platform account balances."""
    _require_auth(request)
    return await asyncio.to_thread(_fetch_all_balances)


# ---------------------------------------------------------------------------
# API: Auto-resolve
# ---------------------------------------------------------------------------

@app.post("/api/resolve")
async def resolve_pending(request: Request):
    """Check platform APIs for settled markets and update PENDING bets."""
    _require_auth(request)
    tracker = get_tracker()
    pending = tracker.get_pending_bets()
    if not pending:
        return {"resolved": 0}

    try:
        clients = await asyncio.to_thread(_get_platform_clients)
    except Exception:
        return {"resolved": 0, "error": "Could not initialize platform clients"}

    kalshi = clients.get("kalshi")
    poly = clients.get("polymarket")
    resolved_count = 0

    for bet in pending:
        result = await asyncio.to_thread(
            _check_bet_resolution, bet, kalshi, poly
        )
        if result:
            tracker.update_bet_result(bet["id"], result)
            resolved_count += 1

    return {"resolved": resolved_count}


def _check_bet_resolution(bet: dict, kalshi, poly) -> Optional[str]:
    """Check if a bet's markets have settled. Returns 'PASS'/'FAIL' or None."""
    rejected = bet["rejected"]  # outcome we didn't bet on

    # Try Kalshi first
    kalshi_ids = bet.get("kalshi_market_id", "")
    if kalshi and kalshi_ids:
        try:
            ids = json.loads(kalshi_ids)
            # Check the rejected outcome's market ticker
            rejected_ticker = ids.get(rejected)
            if rejected_ticker:
                result = kalshi.get_market_result(rejected_ticker)
                if result is not None:
                    # "yes" means the rejected outcome won → we FAIL
                    return "FAIL" if result == "yes" else "PASS"
        except (json.JSONDecodeError, AttributeError):
            pass

    # Try Polymarket
    poly_ids = bet.get("poly_market_id", "")
    if poly and poly_ids:
        try:
            ids = json.loads(poly_ids)
            rejected_id = ids.get(rejected)
            if rejected_id:
                result = poly.get_market_result(rejected_id)
                if result is not None:
                    # "Yes" means the rejected outcome won → we FAIL
                    return "FAIL" if result == "Yes" else "PASS"
        except (json.JSONDecodeError, AttributeError):
            pass

    return None


# ---------------------------------------------------------------------------
# Static files
# ---------------------------------------------------------------------------

@app.get("/")
async def index():
    return FileResponse(BASE_DIR / "static" / "index.html")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    print(f"Starting BetUpset Web {'(DEMO)' if DEMO_MODE else '(LIVE)'}...")
    uvicorn.run(app, host="0.0.0.0", port=8000)
