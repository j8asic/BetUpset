"""
Multi-Platform Scanner.

Fetches soccer markets from all enabled prediction market platforms,
groups them by match using fuzzy team name matching, and produces
CrossPlatformMatch objects ready for the detector.
"""

import csv

from platform_base import PlatformClient, NormalizedMatch, CrossPlatformMatch
from matching import group_matches_by_event, build_match_key


class Scanner:
    """Scans all enabled platforms and groups matches across platforms."""

    def __init__(self, platforms: list[PlatformClient]):
        self.platforms = platforms

    def scan(self) -> list[CrossPlatformMatch]:
        """
        Fetch soccer markets from all platforms and group by match.

        Returns only matches found on 2+ platforms (cross-platform arb
        requires prices from different sources).
        """
        all_matches: list[NormalizedMatch] = []

        for platform in self.platforms:
            try:
                print(f"[Scanner] Fetching from {platform.name}...")
                matches = platform.fetch_soccer_markets()
                all_matches.extend(matches)
                print(f"[Scanner] {platform.name}: {len(matches)} markets")
            except Exception as e:
                print(f"[Scanner] Error fetching from {platform.name}: {e}")

        if not all_matches:
            print("[Scanner] No matches found from any platform")
            return []

        # Group by real-world match across platforms
        grouped = group_matches_by_event(all_matches)

        print(f"[Scanner] {len(all_matches)} total markets -> "
              f"{len(grouped)} cross-platform matches")

        return grouped

    def scan_with_export(self, path: str = "markets_debug.csv") -> list[CrossPlatformMatch]:
        """Scan all platforms, export raw markets to CSV, and return grouped matches."""
        all_matches: list[NormalizedMatch] = []

        for platform in self.platforms:
            try:
                print(f"[Scanner] Fetching from {platform.name}...")
                matches = platform.fetch_soccer_markets()
                all_matches.extend(matches)
                print(f"[Scanner] {platform.name}: {len(matches)} markets")
            except Exception as e:
                print(f"[Scanner] Error fetching from {platform.name}: {e}")

        if not all_matches:
            print("[Scanner] No matches found from any platform")
            return []

        # Export raw markets
        self._export_markets(all_matches, path)

        # Group by real-world match across platforms
        grouped = group_matches_by_event(all_matches)

        # Export cross-platform comparison
        if grouped:
            self._export_cross_platform(grouped, path.replace(".csv", "_xplatform.csv"))

        print(f"[Scanner] {len(all_matches)} total markets -> "
              f"{len(grouped)} cross-platform matches")

        return grouped

    @staticmethod
    def _export_markets(matches: list[NormalizedMatch], path: str):
        """Export all fetched markets to CSV for debugging."""
        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "platform", "home_team", "away_team", "match_key",
                "league", "kickoff",
                "home_price", "draw_price", "away_price",
                "market_id",
            ])
            for m in matches:
                writer.writerow([
                    m.platform,
                    m.home_team,
                    m.away_team,
                    build_match_key(m.home_team, m.away_team),
                    m.league,
                    m.kickoff.isoformat() if m.kickoff else "",
                    m.prices.get("home", ""),
                    m.prices.get("draw", ""),
                    m.prices.get("away", ""),
                    m.platform_market_id,
                ])
        print(f"[Scanner] Exported {len(matches)} markets to {path}")

    @staticmethod
    def _export_cross_platform(matches: list[CrossPlatformMatch], path: str):
        """Export cross-platform match comparisons to CSV, sorted best to worst."""
        STAKE = 100  # dollars per trade for profit calculation

        def _fmt(v):
            return round(v, 4) if isinstance(v, float) else v

        # Pre-compute rows with gap so we can sort
        rows = []
        for m in matches:
            poly = m.platform_data.get("polymarket")
            kalshi = m.platform_data.get("kalshi")
            pp = poly.prices if poly else {}
            kp = kalshi.prices if kalshi else {}

            # Best (cheapest) price per outcome
            best = {}
            for outcome in ("home", "draw", "away"):
                prices = [p for p in [pp.get(outcome), kp.get(outcome)] if p]
                best[outcome] = min(prices) if prices else ""

            # Sort outcomes by price: reject cheapest, cover other two
            available = {o: v for o, v in best.items() if isinstance(v, float) and v > 0}
            if len(available) >= 3:
                sorted_outcomes = sorted(available, key=lambda o: available[o])
                rejected = sorted_outcomes[0]
                covered = sorted_outcomes[1:]
                cost = round(available[covered[0]] + available[covered[1]], 4)
                gap = round(1.0 - cost, 4)
                rejected_price = round(available[rejected], 4)
                roi = round(gap / cost, 4) if cost > 0 else 0
                shares = round(STAKE / cost, 2) if cost > 0 else 0
                profit_if_win = round(shares * gap, 2)
                loss_if_reject = round(STAKE, 2)

                # Risk metrics
                win_prob = round(1.0 - rejected_price, 4)
                prob_for_score = max(win_prob - 0.6667, 0.0) / 0.3333  # Normalize to [0,1] where 2/3+ is good
                score = round(profit_if_win * prob_for_score * prob_for_score, 1)
            else:
                rejected = ""
                rejected_price = ""
                cost = ""
                gap = ""
                roi = ""
                shares = ""
                profit_if_win = ""
                loss_if_reject = ""
                win_prob = ""
                score = ""

            rows.append((
                score if isinstance(score, float) else -999,
                [
                    m.match_key, m.home_team, m.away_team, m.league,
                    _fmt(pp.get("home", "")), _fmt(pp.get("draw", "")), _fmt(pp.get("away", "")),
                    _fmt(kp.get("home", "")), _fmt(kp.get("draw", "")), _fmt(kp.get("away", "")),
                    _fmt(best.get("home", "")), _fmt(best.get("draw", "")), _fmt(best.get("away", "")),
                    cost, gap, rejected, rejected_price, roi,
                    STAKE, shares, profit_if_win, loss_if_reject,
                    win_prob, score,
                ],
            ))

        # Sort by gap descending (best opportunity first)
        rows.sort(key=lambda r: r[0], reverse=True)

        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "match_key", "home_team", "away_team", "league",
                "poly_home", "poly_draw", "poly_away",
                "kalshi_home", "kalshi_draw", "kalshi_away",
                "best_home", "best_draw", "best_away",
                "best_two_cost", "gap", "rejected", "rejected_price", "roi",
                "stake", "shares", "profit_if_win", "loss_if_reject",
                "win_prob", "score",
            ])
            for _, row in rows:
                writer.writerow(row)

        print(f"[Scanner] Exported {len(matches)} cross-platform matches to {path}")
