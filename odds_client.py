"""
Third-party bookmaker odds client using The Odds API (https://the-odds-api.com/).

Provides vig-adjusted "true probabilities" for soccer match outcomes, used as a
ground-truth signal in BetUpset's SCORE formula in place of the circular
market-implied probabilities from Polymarket/Kalshi.

Usage:
    client = OddsApiClient(api_key="your_key")
    client.prefetch_all_sports()   # warm cache once per scan cycle
    probs = client.get_true_probs("Liverpool", "Chelsea", kickoff)
    # probs = {"home": 0.52, "draw": 0.26, "away": 0.22, "bookmaker_count": 8, "odds_source": ...}
"""

import logging
import threading
import time
from datetime import datetime
from typing import Optional

import requests

from matching import _core_name, _fuzzy_cores_match, canonicalize_team

logger = logging.getLogger(__name__)


class OddsApiClient:
    """
    Fetches and caches bookmaker h2h odds from The Odds API.

    Cache design (two levels):
    - Per-sport raw cache: one entry per sport key, invalidated by TTL.
    - Flattened match index: (canonical_home, canonical_away) → probs dict.
      Rebuilt after each fresh sport fetch; enables O(1) lookup per opportunity.

    Both structures are protected by threading.Lock() since web.py runs
    the scanner in threads.
    """

    BASE_URL = "https://api.the-odds-api.com/v4/sports"

    # Soccer sport keys covering the leagues traded on Polymarket/Kalshi.
    # One API call per key returns ALL upcoming games for that competition.
    SOCCER_SPORT_KEYS = [
        "soccer_epl",
        "soccer_spain_la_liga",
        "soccer_germany_bundesliga",
        "soccer_italy_serie_a",
        "soccer_france_ligue_one",
        "soccer_uefa_champs_league",
        "soccer_uefa_europa_league",
        "soccer_netherlands_eredivisie",
        "soccer_portugal_primeira_liga",
        "soccer_usa_mls",
        "soccer_fifa_world_cup_qualifiers_europe",
    ]

    def __init__(
        self,
        api_key: str,
        regions: str = "eu",
        cache_ttl_seconds: int = 300,
    ):
        self._api_key = api_key
        self._regions = regions
        self._ttl = cache_ttl_seconds

        self._session = requests.Session()
        self._session.headers.update({
            "Accept": "application/json",
            "User-Agent": "BetUpset/2.0",
        })

        # Per-sport raw cache: sport_key → {"data": list[dict], "fetched_at": float}
        self._cache: dict[str, dict] = {}
        self._cache_lock = threading.Lock()

        # Flattened match index for fast lookup.
        # Key: (canonical_home, canonical_away)
        # Value: {home, draw, away, bookmaker_count, kickoff, raw_home, raw_away, odds_source}
        self._match_index: dict[tuple[str, str], dict] = {}
        self._index_lock = threading.Lock()

    # ------------------------------------------------------------------
    # HTTP layer
    # ------------------------------------------------------------------

    def fetch_odds(self, sport_key: str) -> list[dict]:
        """
        Return the h2h odds list for a sport key, refreshing if stale.
        On network/HTTP error, returns previously cached data (or [] on cold start).
        """
        with self._cache_lock:
            entry = self._cache.get(sport_key)
            if entry and (time.monotonic() - entry["fetched_at"]) < self._ttl:
                return entry["data"]

        # Network call — outside lock to avoid blocking other threads during I/O
        try:
            resp = self._session.get(
                f"{self.BASE_URL}/{sport_key}/odds/",
                params={
                    "apiKey": self._api_key,
                    "regions": self._regions,
                    "markets": "h2h",
                    "oddsFormat": "decimal",
                },
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
            elif resp.status_code == 422:
                # Sport not active — cache empty list to stop retrying until TTL expires
                data = []
            else:
                logger.warning(
                    "[OddsClient] HTTP %s for sport %s", resp.status_code, sport_key
                )
                with self._cache_lock:
                    stale = self._cache.get(sport_key)
                return stale["data"] if stale else []
        except Exception as exc:
            logger.warning("[OddsClient] Error fetching %s: %s", sport_key, exc)
            with self._cache_lock:
                stale = self._cache.get(sport_key)
            return stale["data"] if stale else []

        with self._cache_lock:
            self._cache[sport_key] = {"data": data, "fetched_at": time.monotonic()}

        self._rebuild_index_for_sport(data)
        return data

    def prefetch_all_sports(self) -> int:
        """
        Pre-warm the cache for all tracked soccer sport keys.
        Call once at the start of each scan cycle; subsequent calls within TTL are no-ops.
        Returns total number of events indexed.
        """
        total = 0
        for sport_key in self.SOCCER_SPORT_KEYS:
            games = self.fetch_odds(sport_key)
            total += len(games)
        logger.debug("[OddsClient] Prefetched %d events across %d sports", total, len(self.SOCCER_SPORT_KEYS))
        return total

    # ------------------------------------------------------------------
    # Probability computation
    # ------------------------------------------------------------------

    @staticmethod
    def _vig_remove(d_home: float, d_draw: float, d_away: float) -> dict[str, float]:
        """
        Convert decimal odds to vig-adjusted fair probabilities using the
        Multiplicative method (correct for unequal-probability events like soccer).

          raw_x   = 1 / d_x
          fair_x  = raw_x / (raw_home + raw_draw + raw_away)

        Result sums to exactly 1.0.
        Returns empty dict if any price is invalid.
        """
        if d_home <= 1.0 or d_draw <= 1.0 or d_away <= 1.0:
            return {}
        r_h = 1.0 / d_home
        r_d = 1.0 / d_draw
        r_a = 1.0 / d_away
        total = r_h + r_d + r_a
        if total <= 0:
            return {}
        return {
            "home": round(r_h / total, 6),
            "draw": round(r_d / total, 6),
            "away": round(r_a / total, 6),
        }

    def _consensus_probs(self, game: dict) -> Optional[dict]:
        """
        Average vig-removed probabilities across all bookmakers in one game event.

        The Odds API response for one event:
          {
            "home_team": "Liverpool", "away_team": "Chelsea",
            "commence_time": "2026-03-30T15:00:00Z",
            "bookmakers": [
              {"key": "pinnacle", "markets": [
                {"key": "h2h", "outcomes": [
                  {"name": "Liverpool", "price": 2.10},
                  {"name": "Chelsea", "price": 3.50},
                  {"name": "Draw", "price": 3.20}
                ]}
              ]}
            ]
          }
        """
        home_name = game.get("home_team", "")
        away_name = game.get("away_team", "")

        samples: list[dict[str, float]] = []
        for bk in game.get("bookmakers", []):
            for market in bk.get("markets", []):
                if market.get("key") != "h2h":
                    continue
                d_home = d_draw = d_away = None
                for o in market.get("outcomes", []):
                    name = o.get("name", "")
                    price = float(o.get("price", 0))
                    if name.lower() == "draw":
                        d_draw = price
                    elif name == home_name:
                        d_home = price
                    elif name == away_name:
                        d_away = price
                if d_home and d_draw and d_away:
                    fp = self._vig_remove(d_home, d_draw, d_away)
                    if fp:
                        samples.append(fp)

        if not samples:
            return None

        n = len(samples)
        return {
            "home": round(sum(s["home"] for s in samples) / n, 6),
            "draw": round(sum(s["draw"] for s in samples) / n, 6),
            "away": round(sum(s["away"] for s in samples) / n, 6),
            "bookmaker_count": n,
        }

    # ------------------------------------------------------------------
    # Index management
    # ------------------------------------------------------------------

    def _rebuild_index_for_sport(self, events: list[dict]) -> None:
        """
        Parse a list of game events into the match index.
        Keyed by (canonical_home, canonical_away) for O(1) lookup.
        """
        new_entries: dict[tuple[str, str], dict] = {}
        for game in events:
            raw_home = game.get("home_team", "")
            raw_away = game.get("away_team", "")
            if not raw_home or not raw_away:
                continue

            probs = self._consensus_probs(game)
            if not probs:
                continue

            kickoff: Optional[datetime] = None
            try:
                kickoff = datetime.fromisoformat(
                    game["commence_time"].replace("Z", "+00:00")
                )
            except (KeyError, ValueError):
                pass

            key = (canonicalize_team(raw_home), canonicalize_team(raw_away))
            new_entries[key] = {
                "home": probs["home"],
                "draw": probs["draw"],
                "away": probs["away"],
                "bookmaker_count": probs.get("bookmaker_count", 0),
                "kickoff": kickoff,
                "raw_home": raw_home,
                "raw_away": raw_away,
                "odds_source": "bookmaker_consensus",
            }

        with self._index_lock:
            self._match_index.update(new_entries)

    # ------------------------------------------------------------------
    # Public lookup
    # ------------------------------------------------------------------

    def get_true_probs(
        self,
        home_team: str,
        away_team: str,
        kickoff: Optional[datetime] = None,
        kickoff_tolerance_hours: float = 24.0,
    ) -> Optional[dict]:
        """
        Return consensus fair probabilities for a match.

        Matching strategy (mirrors matching.py's three-step approach):
          1. Exact canonical key lookup.
          2. Fuzzy core-name scan across entire index (covers name variations
             like "Man Utd" vs "Manchester United").
        Both steps include a kickoff proximity check (±24h by default — generous
        enough to handle Polymarket's endDate vs actual kickoff offset).

        Returns dict with keys:
          home, draw, away (float, vig-removed fair probs summing to ~1.0)
          bookmaker_count (int)
          odds_source ("bookmaker_consensus")
        or None if no match found.
        """
        canonical_home = canonicalize_team(home_team)
        canonical_away = canonicalize_team(away_team)

        with self._index_lock:
            # Step 1: exact canonical key
            entry = self._match_index.get((canonical_home, canonical_away))
            if entry and _kickoff_close(kickoff, entry.get("kickoff"), kickoff_tolerance_hours):
                return dict(entry)

            # Step 2: fuzzy core-name scan
            core_home = _core_name(home_team)
            core_away = _core_name(away_team)
            for (idx_home, idx_away), idx_entry in self._match_index.items():
                if not _kickoff_close(kickoff, idx_entry.get("kickoff"), kickoff_tolerance_hours):
                    continue
                if (
                    _fuzzy_cores_match(core_home, _core_name(idx_entry["raw_home"]))
                    and _fuzzy_cores_match(core_away, _core_name(idx_entry["raw_away"]))
                ):
                    return dict(idx_entry)

        return None


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------

def _kickoff_close(
    dt1: Optional[datetime],
    dt2: Optional[datetime],
    tolerance_hours: float,
) -> bool:
    """True if two datetimes are within tolerance_hours of each other, or either is None."""
    if dt1 is None or dt2 is None:
        return True
    # Make both timezone-aware or both naive for comparison
    if dt1.tzinfo is not None and dt2.tzinfo is None:
        dt2 = dt2.replace(tzinfo=dt1.tzinfo)
    elif dt2.tzinfo is not None and dt1.tzinfo is None:
        dt1 = dt1.replace(tzinfo=dt2.tzinfo)
    diff = abs((dt1 - dt2).total_seconds())
    return diff <= tolerance_hours * 3600
