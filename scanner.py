"""
Multi-Platform Scanner.

Fetches soccer markets from all enabled prediction market platforms,
groups them by match using fuzzy team name matching, and produces
CrossPlatformMatch objects ready for the detector.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed

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

        def fetch(platform: PlatformClient) -> list[NormalizedMatch]:
            print(f"[Scanner] Fetching from {platform.name}...")
            result = platform.fetch_soccer_markets()
            print(f"[Scanner] {platform.name}: {len(result)} markets")
            return result

        with ThreadPoolExecutor(max_workers=len(self.platforms)) as ex:
            futures = {ex.submit(fetch, p): p for p in self.platforms}
            for future in as_completed(futures):
                try:
                    all_matches.extend(future.result())
                except Exception as e:
                    print(f"[Scanner] Error fetching from {futures[future].name}: {e}")

        if not all_matches:
            print("[Scanner] No matches found from any platform")
            return []

        # Group by real-world match across platforms
        grouped = group_matches_by_event(all_matches)

        print(f"[Scanner] {len(all_matches)} total markets -> "
              f"{len(grouped)} cross-platform matches")

        return grouped
