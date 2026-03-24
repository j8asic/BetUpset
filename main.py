"""
BetUpset Prediction Market Arbitrage Bot — Main Entry Point.

Scans Polymarket and Kalshi for soccer arbitrage opportunities using
the "reject lowest outcome, cover the other two" strategy.

Modes:
  MONITOR (default) — scan, detect, log, alert. No orders placed.
  LIVE — full execution (requires --mode live flag). [Not yet implemented]

Usage:
  python main.py                  # Monitor mode, continuous polling
  python main.py --once           # Single scan and exit
  python main.py --demo           # Demo mode with simulated data
  python main.py --mode live      # Live trading (future)
"""

import argparse
import copy
import random
import time
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

from config import load_config, AppConfig, DEMO_MATCHES
from platform_base import NormalizedMatch, CrossPlatformMatch
from polymarket_client import PolymarketClient
from kalshi_client import KalshiClient
from scanner import Scanner
from detector import detect_all_opportunities, format_opportunity
from risk import RiskManager
from tracker import PortfolioTracker
from alerts import AlertManager
from matching import build_match_key, canonicalize_team, parse_match_title


def initialize_platforms(config: AppConfig) -> list:
    """Create platform clients for all enabled platforms."""
    platforms = []

    poly_cfg = config.platforms.get("polymarket")
    if poly_cfg and poly_cfg.enabled:
        platforms.append(PolymarketClient(credentials=poly_cfg.credentials))

    kalshi_cfg = config.platforms.get("kalshi")
    if kalshi_cfg and kalshi_cfg.enabled:
        platforms.append(KalshiClient(credentials=kalshi_cfg.credentials))

    return platforms


def generate_demo_matches() -> list[CrossPlatformMatch]:
    """
    Generate simulated cross-platform match data for demo mode.

    Simulates price discrepancies between Polymarket and Kalshi
    similar to the legacy/arbitrage_simulator.py approach.
    """
    demo_data = copy.deepcopy(DEMO_MATCHES)
    results = []

    for match in demo_data:
        title = match["title"]
        home_raw, away_raw = parse_match_title(title)
        if not away_raw:
            continue

        home_prob = match["home_prob"]
        draw_prob = match["draw_prob"]
        away_prob = match["away_prob"]

        # Simulate Polymarket prices (tight spreads, slight noise)
        poly_noise = lambda p: max(0.02, min(0.98, p + random.gauss(0, 0.025) + 0.005))
        poly_prices = {
            "home": poly_noise(home_prob),
            "draw": poly_noise(draw_prob),
            "away": poly_noise(away_prob),
        }

        # Simulate Kalshi prices (regulatory margin, different noise)
        kalshi_noise = lambda p: max(0.02, min(0.98, p + random.gauss(0, 0.02) + 0.015))
        kalshi_prices = {
            "home": kalshi_noise(home_prob),
            "draw": kalshi_noise(draw_prob),
            "away": kalshi_noise(away_prob),
        }

        key = build_match_key(home_raw, away_raw)

        poly_match = NormalizedMatch(
            platform="polymarket",
            platform_market_id=f"poly-demo-{key}",
            home_team=home_raw,
            away_team=away_raw,
            kickoff=None,
            league=match.get("league", ""),
            prices=poly_prices,
        )

        kalshi_match = NormalizedMatch(
            platform="kalshi",
            platform_market_id=f"kalshi-demo-{key}",
            home_team=home_raw,
            away_team=away_raw,
            kickoff=None,
            league=match.get("league", ""),
            prices=kalshi_prices,
        )

        cross = CrossPlatformMatch(
            match_key=key,
            home_team=canonicalize_team(home_raw),
            away_team=canonicalize_team(away_raw),
            kickoff=None,
            league=match.get("league", ""),
            platform_data={
                "polymarket": poly_match,
                "kalshi": kalshi_match,
            },
        )
        results.append(cross)

    return results


def run_scan(
    scanner: Scanner,
    config: AppConfig,
    tracker: PortfolioTracker,
    risk_mgr: RiskManager,
    alert_mgr: AlertManager,
    mode: str = "monitor",
    demo: bool = False,
) -> int:
    """
    Run a single scan cycle.

    Returns number of opportunities found.
    """
    # Step 1: Get cross-platform matches
    if demo:
        matches = generate_demo_matches()
        print(f"[Demo] Generated {len(matches)} simulated cross-platform matches")
    else:
        matches = scanner.scan()

    if not matches:
        print("[Main] No cross-platform matches found")
        return 0

    # Step 2: Detect opportunities
    opportunities = detect_all_opportunities(
        matches, config.strategy, bankroll=risk_mgr.current_bankroll
    )

    # Step 3: Process opportunities
    for opp in opportunities:
        # Log opportunity
        tracker.record_opportunity(opp)
        alert_mgr.opportunity_detected(opp)

        if mode == "live":
            # Risk check
            open_positions = tracker.get_open_positions()
            decision = risk_mgr.check_trade(opp, open_positions)

            if not decision.approved:
                alert_mgr.risk_warning(
                    f"Trade rejected for {opp.home_team} vs {opp.away_team}: "
                    f"{decision.reason}"
                )
                continue

            # TODO: Execute trade when executor is implemented
            print(f"[Main] LIVE MODE: Would execute trade for "
                  f"{opp.home_team} vs {opp.away_team} "
                  f"(stake ${opp.stake:.2f})")

    # Summary
    alert_mgr.scan_summary(
        matches_scanned=sum(
            len(m.platform_data) for m in matches
        ),
        opportunities=len(opportunities),
        cross_platform=len(matches),
    )

    return len(opportunities)


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="BetUpset Prediction Market Arbitrage Bot"
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Run with simulated price data",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single scan and exit",
    )
    parser.add_argument(
        "--mode",
        choices=["monitor", "live"],
        default="monitor",
        help="Operating mode (default: monitor)",
    )
    args = parser.parse_args()

    # Load config
    config = load_config("config.yaml")

    mode_label = "DEMO" if args.demo else args.mode.upper()

    print("=" * 60)
    print("  BetUpset Prediction Market Arbitrage Bot")
    print(f"  Mode: {mode_label}")
    print("  Strategy: Two-way cover, reject lowest outcome")
    print("=" * 60)
    print()
    print(f"  Bet fraction:    {config.strategy.bet_fraction:.0%}")
    print(f"  Min gap:         ${config.strategy.min_gap}")
    print(f"  Max reject prob: {config.strategy.max_reject_prob:.0%}")
    print(f"  Poll interval:   {config.scanner.interval_seconds}s")
    print()

    # Initialize components
    platforms = initialize_platforms(config)
    platform_names = [p.name for p in platforms]
    print(f"  Platforms: {', '.join(platform_names) or 'None (demo only)'}")
    print()

    scanner = Scanner(platforms)
    risk_mgr = RiskManager(config.risk)
    tracker = PortfolioTracker(
        db_path=config.output.db_path,
        csv_path=config.output.csv_path,
    )
    alert_mgr = AlertManager(config.alerts)

    if not args.once:
        print("Press Ctrl+C to stop.\n")

    total_opportunities = 0
    scan_count = 0

    try:
        while True:
            scan_count += 1
            print(f"\n{'=' * 40}")
            print(f"SCAN #{scan_count}")
            print(f"{'=' * 40}")

            try:
                # Refresh live balance before each scan
                balances = [p.get_balance() or 0.0 for p in platforms]
                total_balance = sum(balances)
                if total_balance > 0:
                    risk_mgr.update_bankroll(total_balance)
                    print(f"  Balance: ${total_balance:,.2f}")

                found = run_scan(
                    scanner, config, tracker, risk_mgr, alert_mgr,
                    mode=args.mode, demo=args.demo,
                )
                total_opportunities += found
            except Exception as e:
                print(f"[Main] Error during scan: {e}")
                import traceback
                traceback.print_exc()

            print(f"\nTotal opportunities found: {total_opportunities}")

            if args.once:
                break

            print(f"Waiting {config.scanner.interval_seconds}s before next scan...")
            time.sleep(config.scanner.interval_seconds)

    except KeyboardInterrupt:
        print(f"\n\n{'=' * 60}")
        print("  Bot stopped by user")

    # Final summary
    summary = tracker.get_pnl_summary()
    print(f"  Total scans: {scan_count}")
    print(f"  Total opportunities: {total_opportunities}")
    print(f"  Total trades: {summary['total_trades']}")
    if summary['total_trades'] > 0:
        print(f"  P&L: ${summary['total_pnl']:+.2f}")
        print(f"  Win rate: {summary['win_rate']:.1%}")
    print(f"  Results saved to: {config.output.csv_path}")
    print("=" * 60)


if __name__ == "__main__":
    main()
