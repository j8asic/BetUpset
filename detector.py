"""
Arbitrage Detector for Prediction Markets.

Ports the detection algorithm from legacy/arbitrage_simulator.py to work with
live data from multiple platforms. Core strategy: for each soccer match,
find the cheapest price per outcome across all platforms, reject the
lowest-priced outcome, and cover the other two.
"""

from typing import Optional

from platform_base import ArbOpportunity, CrossPlatformMatch
from config import StrategyConfig


def detect_opportunity(
    match: CrossPlatformMatch,
    config: StrategyConfig,
    bankroll: float = 10000.0,
) -> Optional[ArbOpportunity]:
    """
    Detect an arbitrage opportunity in a cross-platform match.

    Algorithm (from legacy/arbitrage_simulator.py find_arb):
    1. Find cheapest price for each outcome across all platforms
    2. Sort by price, reject the lowest (least likely outcome)
    3. Calculate gap = 1 - P_a - P_b
    4. Apply filters: MIN_GAP, MAX_REJECT_PROB, SAFETY_FACTOR
    5. Calculate ROI and stake

    Args:
        match: Cross-platform match with prices from multiple platforms
        config: Strategy configuration (thresholds and parameters)
        bankroll: Current bankroll for stake calculation

    Returns:
        ArbOpportunity if filters pass, None otherwise
    """
    outcomes = ["home", "draw", "away"]

    # Step 1: Find cheapest price for each outcome across all platforms
    best: dict[str, dict] = {}
    for outcome in outcomes:
        cheapest_price = 999.0
        cheapest_platform = ""
        cheapest_market_id = ""

        for platform_name, norm_match in match.platform_data.items():
            price = norm_match.prices.get(outcome, 0)
            if 0 < price < cheapest_price:
                cheapest_price = price
                cheapest_platform = platform_name
                # Extract the specific market ID for this outcome
                import json
                try:
                    market_ids = json.loads(norm_match.platform_market_id)
                    cheapest_market_id = market_ids.get(outcome, norm_match.platform_market_id)
                except (json.JSONDecodeError, TypeError):
                    cheapest_market_id = norm_match.platform_market_id

        if cheapest_price < 999.0:
            best[outcome] = {
                "price": cheapest_price,
                "platform": cheapest_platform,
                "market_id": cheapest_market_id,
            }

    # Need at least 3 outcomes priced for the strategy to work
    if len(best) < 3:
        # With only 2 outcomes, we can still try if both are cheap enough
        if len(best) < 2:
            return None

    # Step 2: Sort by price, reject the lowest
    available_outcomes = sorted(best.keys(), key=lambda o: best[o]["price"])
    rejected = available_outcomes[0]
    covered = available_outcomes[1:]  # The two (or one) we cover

    if len(covered) < 2:
        return None

    p_reject = best[rejected]["price"]
    p_a = best[covered[0]]["price"]
    p_b = best[covered[1]]["price"]

    # Step 3: Calculate gap
    gap = 1.0 - p_a - p_b

    # Step 4: Apply filters
    if gap <= config.min_gap:
        return None
    if p_reject > config.max_reject_prob:
        return None
    if p_reject >= gap * config.safety_factor:
        return None

    # Step 5: Calculate metrics
    total_cost = p_a + p_b
    roi = gap / total_cost if total_cost > 0 else 0

    # Stake calculation
    stake = bankroll * config.bet_fraction
    shares = stake / total_cost if total_cost > 0 else 0

    return ArbOpportunity(
        match_key=match.match_key,
        home_team=match.home_team,
        away_team=match.away_team,
        kickoff=match.kickoff,
        league=match.league,
        outcome_a=covered[0],
        platform_a=best[covered[0]]["platform"],
        market_id_a=str(best[covered[0]]["market_id"]),
        price_a=p_a,
        outcome_b=covered[1],
        platform_b=best[covered[1]]["platform"],
        market_id_b=str(best[covered[1]]["market_id"]),
        price_b=p_b,
        rejected_outcome=rejected,
        rejected_price=p_reject,
        rejected_platform=best[rejected]["platform"],
        gap=gap,
        roi_if_win=roi,
        shares=shares,
        stake=stake,
    )


def detect_all_opportunities(
    matches: list[CrossPlatformMatch],
    config: StrategyConfig,
    bankroll: float = 10000.0,
) -> list[ArbOpportunity]:
    """
    Scan all cross-platform matches for arbitrage opportunities.

    Returns list of opportunities sorted by ROI (best first).
    """
    opportunities = []
    for match in matches:
        opp = detect_opportunity(match, config, bankroll)
        if opp:
            opportunities.append(opp)

    # Sort by ROI descending
    opportunities.sort(key=lambda o: o.roi_if_win, reverse=True)
    return opportunities


def format_opportunity(opp: ArbOpportunity) -> str:
    """Format an opportunity for console display."""
    lines = [
        f"  {opp.home_team} vs {opp.away_team}",
        f"  Gap: {opp.gap:.1%} | ROI: {opp.roi_if_win:.1%}",
        f"  Cover {opp.outcome_a} @ ${opp.price_a:.3f} on {opp.platform_a}",
        f"  Cover {opp.outcome_b} @ ${opp.price_b:.3f} on {opp.platform_b}",
        f"  Reject {opp.rejected_outcome} @ ${opp.rejected_price:.3f} ({opp.rejected_platform})",
        f"  Stake: ${opp.stake:.2f} ({opp.shares:.1f} shares)",
    ]
    if opp.kickoff:
        lines.insert(1, f"  Kickoff: {opp.kickoff.strftime('%Y-%m-%d %H:%M UTC')}")
    return "\n".join(lines)
