#!/usr/bin/env python3
"""
Shared scan and row-formatting utilities used by the web app and TUI.
"""

import json
import re
from dataclasses import dataclass


def _sorted_outcomes_by_price(available_prices: dict[str, float]) -> list[str]:
    return sorted(available_prices, key=available_prices.__getitem__)


def _best_platform_for_outcome(
    outcome: str,
    poly_prices: dict,
    kalshi_prices: dict,
) -> str:
    poly_price = poly_prices.get(outcome)
    kalshi_price = kalshi_prices.get(outcome)
    if poly_price and kalshi_price:
        return "polymarket" if poly_price <= kalshi_price else "kalshi"
    return "polymarket" if poly_price else "kalshi"


@dataclass
class MatchRow:
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


def extract_date(match_key: str) -> str:
    match = re.search(r"\d{4}-\d{2}-\d{2}", match_key)
    return match.group(0) if match else "N/A"


def compute_match_rows(cross_matches) -> tuple[list[MatchRow], int]:
    """Convert CrossPlatformMatch objects into display rows."""
    stake = 100
    rows: list[MatchRow] = []

    for match in cross_matches:
        poly = match.platform_data.get("polymarket")
        kalshi = match.platform_data.get("kalshi")
        poly_prices = poly.prices if poly else {}
        kalshi_prices = kalshi.prices if kalshi else {}

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

        best = {}
        for outcome in ("home", "draw", "away"):
            prices = [price for price in [poly_prices.get(outcome), kalshi_prices.get(outcome)] if price]
            best[outcome] = min(prices) if prices else 0.0

        available = {outcome: price for outcome, price in best.items() if price > 0}
        if len(available) < 3:
            continue

        sorted_outcomes = _sorted_outcomes_by_price(available)
        rejected = sorted_outcomes[0]
        covered = sorted_outcomes[1:]

        if available[rejected] >= available[covered[0]]:
            continue

        cost = available[covered[0]] + available[covered[1]]
        gap = 1.0 - cost
        if gap <= 0 or cost <= 0:
            continue

        rejected_price = available[rejected]
        roi = gap / cost
        shares = stake / cost
        profit_if_win = round(shares * gap, 2)
        loss_if_reject = stake
        win_prob = round(1.0 - rejected_price, 4)
        prob_for_score = max(win_prob - 0.6667, 0.0) / 0.3333
        score = round(profit_if_win * prob_for_score * prob_for_score, 2)

        poly_liquidity = poly.liquidity if poly else {}
        kalshi_liquidity = kalshi.liquidity if kalshi else {}

        covered_a, covered_b = covered[0], covered[1]
        platform_a = _best_platform_for_outcome(covered_a, poly_prices, kalshi_prices)
        platform_b = _best_platform_for_outcome(covered_b, poly_prices, kalshi_prices)

        if platform_a == platform_b:
            continue

        price_a = available[covered_a]
        price_b = available[covered_b]

        poly_cost = (
            (price_a if platform_a == "polymarket" else 0.0)
            + (price_b if platform_b == "polymarket" else 0.0)
        )
        kalshi_cost = (
            (price_a if platform_a == "kalshi" else 0.0)
            + (price_b if platform_b == "kalshi" else 0.0)
        )
        poly_stake_fraction = round(poly_cost / cost, 4)
        kalshi_stake_fraction = round(kalshi_cost / cost, 4)

        poly_covered_liq = round(sum(poly_liquidity.get(outcome, 0.0) for outcome in covered), 2)
        kalshi_covered_liq = round(sum(kalshi_liquidity.get(outcome, 0.0) for outcome in covered), 2)

        rows.append(MatchRow(
            match_key=match.match_key,
            date=extract_date(match.match_key),
            home_team=match.home_team,
            away_team=match.away_team,
            best_home=round(best["home"], 3),
            best_draw=round(best["draw"], 3),
            best_away=round(best["away"], 3),
            roi=round(roi, 4),
            win_prob=win_prob,
            score=score,
            rejected=rejected,
            rejected_price=round(rejected_price, 3),
            profit_if_win=profit_if_win,
            loss_if_reject=loss_if_reject,
            polymarket_url=poly_url,
            kalshi_url=kalshi_url,
            poly_market_id=poly.platform_market_id if poly else "",
            kalshi_market_id=kalshi.platform_market_id if kalshi else "",
            poly_volume=round(sum(poly.liquidity.values()), 2) if poly else 0.0,
            kalshi_volume=round(sum(kalshi.liquidity.values()), 2) if kalshi else 0.0,
            covered_a=covered_a,
            covered_b=covered_b,
            platform_a=platform_a,
            platform_b=platform_b,
            price_a=round(price_a, 4),
            price_b=round(price_b, 4),
            poly_stake_fraction=poly_stake_fraction,
            kalshi_stake_fraction=kalshi_stake_fraction,
            poly_covered_liq=poly_covered_liq,
            kalshi_covered_liq=kalshi_covered_liq,
        ))

    rows.sort(key=lambda row: row.score, reverse=True)
    return rows, len(cross_matches)


def run_scan(demo: bool = False) -> tuple[list[MatchRow], int]:
    """Run the shared scanner pipeline."""
    from config import load_config
    from main import initialize_platforms, generate_demo_matches
    from scanner import Scanner

    config = load_config("config.yaml")

    if demo:
        matches = generate_demo_matches()
    else:
        platforms = initialize_platforms(config)
        if not platforms:
            return [], 0
        scanner = Scanner(platforms)
        matches = scanner.scan()

    return compute_match_rows(matches)
