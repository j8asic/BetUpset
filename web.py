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
from datetime import date, datetime
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
KICKOFF_INDEX_TTL = 300  # seconds

_tracker = None
_kickoff_index_cache: dict[str, dict[str, str]] = {"polymarket": {}, "kalshi": {}}
_kickoff_index_time: float = 0.0


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


def _resolve_kickoff_from_bet_ids(bet: dict, kickoff_indexes: dict[str, dict[str, str]]) -> str:
    for platform, field, key_name in (
        ("polymarket", "poly_market_id", "_event_slug"),
        ("kalshi", "kalshi_market_id", "_event_ticker"),
    ):
        raw_ids = bet.get(field, "")
        if not raw_ids:
            continue
        try:
            market_ids = json.loads(raw_ids)
        except (json.JSONDecodeError, TypeError):
            continue
        lookup_key = market_ids.get(key_name, "")
        if not lookup_key:
            continue
        kickoff_iso = kickoff_indexes.get(platform, {}).get(lookup_key, "")
        if kickoff_iso:
            return kickoff_iso
    return ""


def _load_kickoff_indexes() -> dict[str, dict[str, str]]:
    global _kickoff_index_cache, _kickoff_index_time

    now = time.monotonic()
    if _kickoff_index_time and now - _kickoff_index_time < KICKOFF_INDEX_TTL:
        return _kickoff_index_cache

    try:
        clients = _get_platform_clients()
    except (OSError, RuntimeError, ValueError):
        return _kickoff_index_cache

    kickoff_indexes: dict[str, dict[str, str]] = {"polymarket": {}, "kalshi": {}}
    for platform, meta_key in (("polymarket", "_event_slug"), ("kalshi", "_event_ticker")):
        client = clients.get(platform)
        if not client or not hasattr(client, "fetch_soccer_markets"):
            continue
        try:
            matches = client.fetch_soccer_markets()
        except (OSError, RuntimeError, ValueError):
            continue
        for match in matches:
            if not match.kickoff:
                continue
            try:
                market_ids = json.loads(match.platform_market_id)
            except (json.JSONDecodeError, TypeError):
                continue
            lookup_key = market_ids.get(meta_key, "")
            if lookup_key:
                kickoff_indexes[platform][lookup_key] = match.kickoff.isoformat()

    _kickoff_index_cache = kickoff_indexes
    _kickoff_index_time = now
    return kickoff_indexes


def _bet_needs_kickoff_enrichment(bet: dict) -> bool:
    if bet.get("kickoff_iso"):
        return False
    if not (bet.get("poly_market_id") or bet.get("kalshi_market_id")):
        return False
    if bet.get("result") != "PENDING":
        return False
    return _bet_is_ready_for_resolution_check(bet)


def _enrich_bets_with_kickoff(bets: list[dict]) -> list[dict]:
    missing = [bet for bet in bets if _bet_needs_kickoff_enrichment(bet)]
    if not missing:
        return bets

    kickoff_indexes = _load_kickoff_indexes()
    if not any(kickoff_indexes.values()):
        return bets

    tracker = get_tracker()
    for bet in missing:
        kickoff_iso = _resolve_kickoff_from_bet_ids(bet, kickoff_indexes)
        if kickoff_iso:
            bet["kickoff_iso"] = kickoff_iso
            tracker.update_bet_kickoff(bet["id"], kickoff_iso)

    return bets


def _get_execution_config():
    from config import ExecutionConfig, load_config

    try:
        return load_config("config.yaml").execution
    except (OSError, ValueError):
        return ExecutionConfig()


def _execution_status_payload() -> dict:
    cfg = _get_execution_config()
    return {
        "dry_run_only": cfg.dry_run_only,
        "max_stake_per_trade": cfg.max_stake_per_trade,
        "max_scan_age_seconds": cfg.max_scan_age_seconds,
        "max_liquidity_fraction": cfg.max_liquidity_fraction,
    }


def _platform_stake_fractions(bet: dict) -> dict[str, float]:
    price_a = float(bet.get("price_a", 0.0) or 0.0)
    price_b = float(bet.get("price_b", 0.0) or 0.0)
    total_price = price_a + price_b
    if total_price <= 0:
        return {}

    fractions = {"polymarket": 0.0, "kalshi": 0.0}
    for suffix in ("a", "b"):
        platform = str(bet.get(f"platform_{suffix}", "") or "")
        price = float(bet.get(f"price_{suffix}", 0.0) or 0.0)
        if platform in fractions:
            fractions[platform] += price / total_price

    return {platform: fraction for platform, fraction in fractions.items() if fraction > 0}


def _platform_stake_amounts(bet: dict) -> dict[str, float]:
    stake = float(bet.get("stake", 0.0) or 0.0)
    return {
        platform: stake * fraction
        for platform, fraction in _platform_stake_fractions(bet).items()
    }


def _validate_execution_request(bet: dict) -> None:
    cfg = _get_execution_config()
    stake = float(bet.get("stake", 0.0) or 0.0)
    if stake <= 0:
        raise HTTPException(400, "Stake must be greater than zero")

    if cfg.dry_run_only:
        raise HTTPException(409, "Execution blocked: dry_run_only is enabled in config.yaml")

    if cfg.max_stake_per_trade > 0 and stake > cfg.max_stake_per_trade:
        raise HTTPException(
            409,
            f"Execution blocked: stake ${stake:.2f} exceeds max_stake_per_trade ${cfg.max_stake_per_trade:.2f}",
        )

    scan_time = float(bet.get("scanned_at", 0.0) or 0.0)
    if scan_time <= 0:
        scan_time = _scan_time
    if scan_time <= 0:
        raise HTTPException(409, "Execution blocked: no scan timestamp is available; refresh opportunities first")
    if scan_time > time.time() + 5:
        raise HTTPException(400, "Execution blocked: invalid scan timestamp")
    if cfg.max_scan_age_seconds > 0:
        age_seconds = time.time() - scan_time
        if age_seconds > cfg.max_scan_age_seconds:
            raise HTTPException(
                409,
                (
                    "Execution blocked: scan data is stale "
                    f"({age_seconds:.0f}s old, limit {cfg.max_scan_age_seconds}s); refresh opportunities first"
                ),
            )

    max_liquidity_fraction = cfg.max_liquidity_fraction
    if max_liquidity_fraction <= 0:
        return

    liquidity_by_platform = {
        "polymarket": float(bet.get("poly_covered_liq", 0.0) or 0.0),
        "kalshi": float(bet.get("kalshi_covered_liq", 0.0) or 0.0),
    }
    for platform, fraction in _platform_stake_fractions(bet).items():
        covered_liquidity = liquidity_by_platform.get(platform, 0.0)
        if covered_liquidity <= 0:
            continue
        max_stake = covered_liquidity * fraction * max_liquidity_fraction
        if stake > max_stake:
            raise HTTPException(
                409,
                (
                    f"Execution blocked: {platform} covered liquidity only supports about ${max_stake:.2f} "
                    f"at the configured {max_liquidity_fraction:.0%} depth cap"
                ),
            )


def _validate_live_balances(bet: dict, live_balances: dict) -> None:
    for platform, required_amount in _platform_stake_amounts(bet).items():
        live_balance = live_balances.get(platform)
        if live_balance is None:
            raise HTTPException(
                409,
                f"Execution blocked: could not verify live {platform} balance before placing the order",
            )
        if required_amount > float(live_balance):
            raise HTTPException(
                409,
                (
                    f"Execution blocked: {platform} needs ${required_amount:.2f} but live balance is only "
                    f"${float(live_balance):.2f}"
                ),
            )


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
        return {
            "matches": _scan_cache,
            "total_matches": _scan_total,
            "scanned_at": _scan_time,
            "cached": True,
            "execution": _execution_status_payload(),
        }

    async with _scan_lock:
        if _scan_cache and (time.time() - _scan_time < SCAN_CACHE_TTL):
            return {
                "matches": _scan_cache,
                "total_matches": _scan_total,
                "scanned_at": _scan_time,
                "cached": True,
                "execution": _execution_status_payload(),
            }

        rows, total = await asyncio.to_thread(run_scan, demo=use_demo)
        _scan_cache = _match_rows_to_dicts(rows)
        _scan_total = total
        _scan_time = time.time()
        return {
            "matches": _scan_cache,
            "total_matches": _scan_total,
            "scanned_at": _scan_time,
            "cached": False,
            "execution": _execution_status_payload(),
        }


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
        return {
            "matches": _scan_cache,
            "total_matches": _scan_total,
            "scanned_at": _scan_time,
            "cached": False,
            "execution": _execution_status_payload(),
        }


# ---------------------------------------------------------------------------
# API: Bets
# ---------------------------------------------------------------------------

class PlaceBetRequest(BaseModel):
    match_key: str
    date: str
    kickoff_iso: str = ""
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
    poly_covered_liq: float = 0.0
    kalshi_covered_liq: float = 0.0
    scanned_at: float = 0.0


@app.get("/api/bets")
async def bets_list(request: Request):
    _require_auth(request)
    tracker = get_tracker()
    bets = tracker.get_all_bets()
    bets = await asyncio.to_thread(_enrich_bets_with_kickoff, bets)
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
        try:
            order_id = client.place_order(ticker, "yes", shares, price_cents)
            return {
                "order_id": order_id, "ticker": ticker,
                "outcome": outcome, "count": shares,
                "price_cents": price_cents, "ok": True,
            }
        except Exception as e:
            return {"error": str(e), "ticker": ticker, "outcome": outcome, "ok": False}

    if platform == "polymarket":
        clob_tokens = poly_ids.get("_clob_tokens", {})
        token_id = clob_tokens.get(outcome)
        if not token_id:
            return {"error": f"no CLOB token for {outcome}", "ok": False}
        try:
            order_id = client.place_order(token_id, "BUY", shares * price, price)
            return {
                "order_id": order_id, "token_id": token_id,
                "outcome": outcome, "ok": True,
            }
        except Exception as e:
            return {"error": str(e), "token_id": token_id, "outcome": outcome, "ok": False}

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

    # Both legs need equal shares for the hedge to work (each share pays $1 on win).
    # Round to nearest integer (both platforms need int), but don't exceed stake.
    cost_per_share = price_a + price_b
    ideal_shares = stake / cost_per_share
    shares = round(ideal_shares)
    if shares * cost_per_share > stake:
        shares = int(ideal_shares)  # fall back to floor if rounding up exceeds budget
    shares = max(5, shares)  # Polymarket minimum order size is 5

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
    _validate_execution_request(data)
    live_balances = await asyncio.to_thread(_fetch_all_balances)
    _validate_live_balances(data, live_balances)
    execution = await asyncio.to_thread(_place_orders, data)
    return {"execution": execution, "balances": live_balances}


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


def _sell_bet_positions(bet: dict) -> dict:
    """Blocking: sell platform positions for a bet, iterating all stored market IDs."""
    results: dict[str, str] = {}

    try:
        clients = _get_platform_clients()
    except Exception as e:
        return {"error": str(e)}

    poly = clients.get("polymarket")
    kalshi = clients.get("kalshi")

    # --- Polymarket: sell any token we hold ---
    poly_raw = bet.get("poly_market_id") or ""
    if poly and poly_raw:
        try:
            poly_ids = json.loads(poly_raw)
            clob_tokens: dict = poly_ids.get("_clob_tokens", {})
            sold_any = False
            for outcome, token_id in clob_tokens.items():
                if not token_id:
                    continue
                shares = poly.get_position(token_id)
                if shares > 0:
                    # Fetch current YES price for this token as sell price
                    market_data = poly._get(f"markets/{poly_ids.get(outcome, '')}")
                    price = 0.5
                    if market_data:
                        try:
                            tokens = market_data.get("tokens", [])
                            for t in tokens:
                                if t.get("token_id") == token_id:
                                    price = float(t.get("price", 0.5))
                        except Exception:
                            pass
                    ok = poly.sell_position(token_id, shares, price)
                    results[f"polymarket_{outcome}"] = "sold" if ok else "sell_failed"
                    sold_any = True
            if not sold_any:
                results["polymarket"] = "no_position"
        except Exception as e:
            results["polymarket"] = f"error: {e}"

    # --- Kalshi: sell any position we hold ---
    kalshi_raw = bet.get("kalshi_market_id") or ""
    if kalshi and kalshi_raw:
        try:
            kalshi_ids = json.loads(kalshi_raw)
            sold_any = False
            for key, ticker in kalshi_ids.items():
                if key.startswith("_") or not ticker:
                    continue
                for side in ("yes", "no"):
                    count = kalshi.get_position(ticker, side)
                    if count > 0:
                        market_data = kalshi._get(f"/markets/{ticker}")
                        price_cents = 50
                        if market_data and "market" in market_data:
                            m = market_data["market"]
                            price_cents = int(m.get("yes_bid", 50) if side == "yes"
                                              else (100 - m.get("yes_ask", 50)))
                            price_cents = max(1, min(99, price_cents))
                        ok = kalshi.sell_position(ticker, side, count, price_cents)
                        results[f"kalshi_{key}_{side}"] = "sold" if ok else "sell_failed"
                        sold_any = True
            if not sold_any:
                results["kalshi"] = "no_position"
        except Exception as e:
            results["kalshi"] = f"error: {e}"

    return results


@app.post("/api/bets/{bet_id}/sell")
async def bets_sell(request: Request, bet_id: int):
    """Sell platform positions for a pending bet, then delete the local record."""
    _require_auth(request)
    tracker = get_tracker()
    all_bets = tracker.get_all_bets()
    bet = next((b for b in all_bets if b["id"] == bet_id), None)
    if not bet:
        raise HTTPException(status_code=404, detail="Bet not found")

    results = await asyncio.to_thread(_sell_bet_positions, bet)
    tracker.delete_bet(bet_id)
    return {"ok": True, "results": results}


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
    pending = [bet for bet in tracker.get_pending_bets() if _bet_is_ready_for_resolution_check(bet)]
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
        resolution = await asyncio.to_thread(
            _check_bet_resolution, bet, kalshi, poly
        )
        if resolution:
            result, winning_outcome = resolution
            tracker.update_bet_result(bet["id"], result, winning_outcome)
            resolved_count += 1

    return {"resolved": resolved_count}


def _check_bet_resolution(bet: dict, kalshi, poly) -> Optional[tuple[str, str]]:
    """Check if a bet's markets have settled.

    Returns (result, winning_outcome) or None if not yet settled.
    result is 'PASS' or 'FAIL', winning_outcome is 'home'/'draw'/'away'.
    """
    rejected = bet["rejected"]
    outcomes = ["home", "draw", "away"]

    # Try Kalshi — check all outcomes to find the winner
    kalshi_ids = bet.get("kalshi_market_id", "")
    if kalshi and kalshi_ids:
        try:
            ids = json.loads(kalshi_ids)
            for outcome in outcomes:
                ticker = ids.get(outcome)
                if not ticker:
                    continue
                result = kalshi.get_market_result(ticker)
                if result == "yes":
                    bet_result = "FAIL" if outcome == rejected else "PASS"
                    return (bet_result, outcome)
        except (json.JSONDecodeError, AttributeError):
            pass

    # Try Polymarket — check all outcomes to find the winner
    poly_ids = bet.get("poly_market_id", "")
    if poly and poly_ids:
        try:
            ids = json.loads(poly_ids)
            for outcome in outcomes:
                market_id = ids.get(outcome)
                if not market_id:
                    continue
                result = poly.get_market_result(market_id)
                if result == "Yes":
                    bet_result = "FAIL" if outcome == rejected else "PASS"
                    return (bet_result, outcome)
        except (json.JSONDecodeError, AttributeError):
            pass

    return None


def _bet_is_ready_for_resolution_check(bet: dict, now: Optional[datetime] = None) -> bool:
    now = now or datetime.now().astimezone()

    kickoff_iso = str(bet.get("kickoff_iso", "") or "").strip()
    if kickoff_iso:
        try:
            kickoff = datetime.fromisoformat(kickoff_iso)
        except ValueError:
            kickoff = None
        if kickoff is not None:
            if kickoff.tzinfo is None:
                kickoff = kickoff.replace(tzinfo=now.tzinfo)
            return kickoff <= now

    raw_date = str(bet.get("date", "") or "").strip()
    if raw_date:
        try:
            return date.fromisoformat(raw_date) <= now.date()
        except ValueError:
            return True

    return True


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
