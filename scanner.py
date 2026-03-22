"""
Multi-Platform Scanner.

Fetches soccer markets from all enabled prediction market platforms,
groups them by match using fuzzy team name matching, and produces
CrossPlatformMatch objects ready for the detector.
"""

from platform_base import PlatformClient, NormalizedMatch, CrossPlatformMatch
from matching import group_matches_by_event


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
