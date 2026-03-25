#!/usr/bin/env python3
"""
Shared scan and row-formatting utilities used by the web app.

Uses detector.py as the single source of truth for arbitrage detection.
"""

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone

from config import StrategyConfig
from detector import detect_opportunity
from platform_base import ArbOpportunity, CrossPlatformMatch


@dataclass
class MatchRow:
    match_key: str
    date: str
    kickoff_iso: str
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
    polymarket_url: str = ""
    kalshi_url: str = ""
    poly_market_id: str = ""
    kalshi_market_id: str = ""
    poly_volume: float = 0.0
    kalshi_volume: float = 0.0
    covered_a: str = ""
    covered_b: str = ""
    platform_a: str = ""
    platform_b: str = ""
    price_a: float = 0.0
    price_b: float = 0.0
    poly_stake_fraction: float = 0.0
    kalshi_stake_fraction: float = 0.0
    poly_covered_liq: float = 0.0
    kalshi_covered_liq: float = 0.0
    pre_kickoff_home: float = 0.0
    pre_kickoff_draw: float = 0.0
    pre_kickoff_away: float = 0.0
    best_home_platform: str = ""
    best_draw_platform: str = ""
    best_away_platform: str = ""


def extract_date(match_key: str) -> str:
    match = re.search(r"\d{4}-\d{2}-\d{2}", match_key)
    return match.group(0) if match else "N/A"


def _kickoff_iso(kickoff: datetime | None) -> str:
    if not kickoff:
        return ""
    return kickoff.isoformat()


def _extract_urls(match: CrossPlatformMatch) -> tuple[str, str]:
    """Extract Polymarket and Kalshi URLs from platform data."""
    poly = match.platform_data.get("polymarket")
    kalshi = match.platform_data.get("kalshi")

    poly_url = ""
    if poly:
        try:
            ids = json.loads(poly.platform_market_id)
            slug = ids.get("_event_slug", "")
            if slug:
                poly_url = f"https://polymarket.com/event/{slug}"
        except (json.JSONDecodeError, AttributeError):
            pass

    kalshi_url = ""
    if kalshi:
        try:
            ids = json.loads(kalshi.platform_market_id)
            event_ticker = ids.get("_event_ticker", "")
            series_ticker = ids.get("_series_ticker", "")
            series_slug = ids.get("_series_slug", "")
            if event_ticker and series_ticker and series_slug:
                kalshi_url = (
                    f"https://kalshi.com/markets/{series_ticker.lower()}"
                    f"/{series_slug}/{event_ticker.lower()}"
                )
        except (json.JSONDecodeError, AttributeError):
            pass

    return poly_url, kalshi_url


def _opp_to_row(opp: ArbOpportunity, match: CrossPlatformMatch) -> MatchRow:
    """Convert a detected ArbOpportunity + its source match into a MatchRow."""
    poly = match.platform_data.get("polymarket")
    kalshi = match.platform_data.get("kalshi")
    poly_url, kalshi_url = _extract_urls(match)

    stake = 100
    cost = opp.price_a + opp.price_b
    shares = stake / cost if cost > 0 else 0
    profit_if_win = round(shares * opp.gap, 2)

    # Platform allocation fractions
    poly_cost = (
        (opp.price_a if opp.platform_a == "polymarket" else 0.0)
        + (opp.price_b if opp.platform_b == "polymarket" else 0.0)
    )
    kalshi_cost = (
        (opp.price_a if opp.platform_a == "kalshi" else 0.0)
        + (opp.price_b if opp.platform_b == "kalshi" else 0.0)
    )
    poly_stake_fraction = round(poly_cost / cost, 4) if cost > 0 else 0.0
    kalshi_stake_fraction = round(kalshi_cost / cost, 4) if cost > 0 else 0.0

    # Liquidity for covered outcomes
    poly_liq = poly.liquidity if poly else {}
    kalshi_liq = kalshi.liquidity if kalshi else {}
    covered = [opp.outcome_a, opp.outcome_b]
    poly_covered_liq = round(sum(poly_liq.get(o, 0.0) for o in covered), 2)
    kalshi_covered_liq = round(sum(kalshi_liq.get(o, 0.0) for o in covered), 2)

    # Best price per outcome — use the actual arb leg prices and platforms.
    # The rejected outcome shows cheapest available but gets no platform link.
    poly_prices = poly.prices if poly else {}
    kalshi_prices = kalshi.prices if kalshi else {}
    best = {}
    best_platform = {}
    for outcome in ("home", "draw", "away"):
        if outcome == opp.outcome_a:
            best[outcome] = opp.price_a
            best_platform[outcome] = opp.platform_a
        elif outcome == opp.outcome_b:
            best[outcome] = opp.price_b
            best_platform[outcome] = opp.platform_b
        else:
            # rejected outcome — show cheapest price, link to that platform
            p_p = poly_prices.get(outcome)
            k_p = kalshi_prices.get(outcome)
            if p_p and k_p:
                if p_p <= k_p:
                    best[outcome], best_platform[outcome] = p_p, "polymarket"
                else:
                    best[outcome], best_platform[outcome] = k_p, "kalshi"
            elif p_p:
                best[outcome], best_platform[outcome] = p_p, "polymarket"
            elif k_p:
                best[outcome], best_platform[outcome] = k_p, "kalshi"
            else:
                best[outcome], best_platform[outcome] = 0.0, ""

    # Pre-kickoff prices from Polymarket history API
    pre = poly.pre_kickoff_prices if poly and poly.pre_kickoff_prices else {}

    # my metrics
    win_prob = round(1.0 - opp.rejected_price, 4)    
    prob_for_score = max(win_prob - 0.6667, 0.0) / 0.3333
    score = min(10, round(opp.roi_if_win * prob_for_score * prob_for_score * 100, 0))

    return MatchRow(
        match_key=opp.match_key,
        date=extract_date(opp.match_key),
        kickoff_iso=_kickoff_iso(opp.kickoff),
        home_team=opp.home_team,
        away_team=opp.away_team,
        best_home=round(best["home"], 3),
        best_draw=round(best["draw"], 3),
        best_away=round(best["away"], 3),
        roi=round(opp.roi_if_win, 4),
        win_prob=win_prob,
        score=score,
        rejected=opp.rejected_outcome,
        rejected_price=round(opp.rejected_price, 3),
        profit_if_win=profit_if_win,
        loss_if_reject=stake,
        polymarket_url=poly_url,
        kalshi_url=kalshi_url,
        poly_market_id=poly.platform_market_id if poly else "",
        kalshi_market_id=kalshi.platform_market_id if kalshi else "",
        poly_volume=round(sum(poly.liquidity.values()), 2) if poly else 0.0,
        kalshi_volume=round(sum(kalshi.liquidity.values()), 2) if kalshi else 0.0,
        covered_a=opp.outcome_a,
        covered_b=opp.outcome_b,
        platform_a=opp.platform_a,
        platform_b=opp.platform_b,
        price_a=round(opp.price_a, 4),
        price_b=round(opp.price_b, 4),
        poly_stake_fraction=poly_stake_fraction,
        kalshi_stake_fraction=kalshi_stake_fraction,
        poly_covered_liq=poly_covered_liq,
        kalshi_covered_liq=kalshi_covered_liq,
        pre_kickoff_home=round(pre.get("home", 0.0), 3),
        pre_kickoff_draw=round(pre.get("draw", 0.0), 3),
        pre_kickoff_away=round(pre.get("away", 0.0), 3),
        best_home_platform=best_platform.get("home", ""),
        best_draw_platform=best_platform.get("draw", ""),
        best_away_platform=best_platform.get("away", ""),
    )


def compute_match_rows(
    cross_matches: list[CrossPlatformMatch],
    config: StrategyConfig | None = None,
) -> tuple[list[MatchRow], int]:
    """Convert CrossPlatformMatch objects into display rows using the detector."""
    if config is None:
        config = StrategyConfig()

    rows: list[MatchRow] = []
    for match in cross_matches:
        opp = detect_opportunity(match, config)
        if opp is None:
            continue
        # Require cross-platform (different platforms for each leg)
        if opp.platform_a == opp.platform_b:
            continue
        rows.append(_opp_to_row(opp, match))

    rows.sort(key=lambda row: row.score, reverse=True)
    return rows, len(cross_matches)


_platforms: list | None = None  # singleton — reused across scans so caches persist
_last_raw_matches: list[CrossPlatformMatch] = []


def get_last_raw_matches() -> list[CrossPlatformMatch]:
    return _last_raw_matches


def run_scan(demo: bool = False) -> tuple[list[MatchRow], int]:
    """Run the shared scanner pipeline."""
    global _platforms

    from config import load_config
    from main import initialize_platforms, generate_demo_matches
    from scanner import Scanner

    config = load_config("config.yaml")

    if demo:
        matches = generate_demo_matches()
    else:
        if _platforms is None:
            _platforms = initialize_platforms(config)
        if not _platforms:
            return [], 0
        scanner = Scanner(_platforms)
        matches = scanner.scan()

        # After matching we have the corrected Kalshi kickoff. For live games where
        # Polymarket's extended endDate caused the pre-kickoff fetch to be skipped,
        # retry here using the snapshot cache (fast) or CLOB fallback (cold start).
        now_utc = datetime.now(timezone.utc)
        poly_client = next((p for p in _platforms if p.name == "polymarket"), None)
        if poly_client:
            tasks = []
            for match in matches:
                if not match.kickoff or now_utc <= match.kickoff:
                    continue
                poly_match = match.platform_data.get("polymarket")
                if not poly_match or poly_match.pre_kickoff_prices:
                    continue
                try:
                    ids = json.loads(poly_match.platform_market_id)
                    event_slug = ids.get("_event_slug", "")
                    # 1. Snapshot cache (Gamma prices captured before kickoff — no API call)
                    if event_slug and event_slug in poly_client._event_price_snapshot:
                        poly_match.pre_kickoff_prices = poly_client._event_price_snapshot[event_slug]
                        continue

                    # 2. CLOB prices-history fallback (cold start — server restarted mid-game)
                    clob_tokens = ids.get("_clob_tokens", {})
                    if clob_tokens:
                        for outcome, token_id in clob_tokens.items():
                            tasks.append((poly_match, outcome, token_id, match.kickoff))
                except Exception:
                    pass

            if tasks:
                def fetch_one(task):
                    pm, outcome, tid, ko = task
                    return pm, outcome, poly_client.get_pre_kickoff_price(tid, ko)

                with ThreadPoolExecutor(max_workers=20) as executor:
                    futures = [executor.submit(fetch_one, t) for t in tasks]
                    for f in as_completed(futures):
                        try:
                            pm, outcome, price = f.result()
                            if price is not None:
                                if pm.pre_kickoff_prices is None:
                                    pm.pre_kickoff_prices = {}
                                pm.pre_kickoff_prices[outcome] = price
                        except Exception:
                            pass

    global _last_raw_matches
    _last_raw_matches = matches
    return compute_match_rows(matches, config.strategy)
