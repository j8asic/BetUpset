"""
Cross-platform team name matching and normalization.

Provides fuzzy matching infrastructure for linking the same soccer match
across different prediction market platforms (Polymarket, Kalshi, BettorEdge).
"""

import re
import unicodedata
from difflib import SequenceMatcher
from datetime import datetime
from typing import Optional

from platform_base import NormalizedMatch, CrossPlatformMatch


# ============================================================
# TEAM NAME ALIASES
# ============================================================
# Each group lists all known variants for one team.
# The FIRST entry is the canonical name used in match keys.

ALIAS_GROUPS = [
    # England - Premier League
    ["manchester united", "man utd", "man united"],
    ["manchester city", "man city"],
    ["tottenham hotspur", "tottenham", "spurs"],
    ["west ham united", "west ham"],
    ["newcastle united", "newcastle"],
    ["sheffield united", "sheffield utd"],
    ["nottingham forest", "nott'm forest", "nottm forest"],
    ["brighton and hove albion", "brighton & hove albion", "brighton"],
    ["wolverhampton wanderers", "wolves", "wolverhampton"],
    ["crystal palace", "palace"],
    ["leicester city", "leicester"],
    ["leeds united", "leeds"],
    ["aston villa", "villa"],

    # Spain - La Liga
    ["real madrid", "real"],
    ["atletico madrid", "atletico de madrid", "atletico"],
    ["real sociedad", "sociedad"],
    ["real betis", "betis"],
    ["athletic bilbao", "athletic club", "athletic"],
    ["celta vigo", "celta de vigo", "celta"],
    ["deportivo alaves", "alaves"],
    ["rayo vallecano", "rayo"],
    ["villarreal", "villarreal cf"],

    # Germany - Bundesliga
    ["borussia dortmund", "dortmund", "bvb"],
    ["borussia monchengladbach", "borussia m'gladbach", "monchengladbach", "gladbach"],
    ["bayern munich", "bayern munchen", "bayern münchen", "bayern"],
    ["rb leipzig", "rasenballsport leipzig", "leipzig"],
    ["bayer leverkusen", "leverkusen", "bayer 04 leverkusen"],
    ["eintracht frankfurt", "frankfurt", "eintracht"],
    ["vfb stuttgart", "stuttgart"],
    ["werder bremen", "bremen"],
    ["sc freiburg", "freiburg"],
    ["1. fc union berlin", "union berlin"],
    ["1. fc koln", "fc koln", "cologne", "köln"],
    ["fc augsburg", "augsburg"],
    ["tsg hoffenheim", "hoffenheim"],
    ["vfl wolfsburg", "wolfsburg"],
    ["vfl bochum", "bochum"],
    ["sv darmstadt 98", "darmstadt"],
    ["1. fc heidenheim", "heidenheim"],

    # Italy - Serie A
    ["inter milan", "internazionale", "internazionale milano", "inter"],
    ["ac milan", "milan"],
    ["napoli", "ssc napoli"],
    ["lazio", "ss lazio"],
    ["roma", "as roma"],
    ["fiorentina", "acf fiorentina", "ac fiorentina"],
    ["atalanta", "atalanta bc", "atalanta bergamo"],
    ["torino", "torino fc"],
    ["genoa", "genoa cfc"],
    ["bologna", "bologna fc"],
    ["udinese", "udinese calcio"],
    ["sassuolo", "us sassuolo"],
    ["cagliari", "cagliari calcio"],
    ["empoli", "empoli fc"],
    ["hellas verona", "verona"],
    ["salernitana", "us salernitana"],
    ["lecce", "us lecce"],
    ["frosinone", "frosinone calcio"],
    ["monza", "ac monza"],
    ["juventus", "juve"],

    # France - Ligue 1
    ["paris saint-germain", "paris saint germain", "paris sg", "psg", "paris"],
    ["olympique lyonnais", "olympique lyon", "lyon", "ol"],
    ["olympique marseille", "olympique de marseille", "marseille", "om"],
    ["as monaco", "monaco"],
    ["lille", "losc lille", "losc"],
    ["stade rennais", "rennes"],
    ["rc strasbourg", "strasbourg"],
    ["fc nantes", "nantes"],
    ["toulouse", "toulouse fc"],
    ["rc lens", "lens"],
    ["stade brestois", "brest"],
    ["le havre", "le havre ac"],
    ["clermont foot", "clermont"],
    ["fc lorient", "lorient"],
    ["stade de reims", "reims"],
    ["montpellier", "montpellier hsc"],
    ["ogc nice", "nice"],
    ["metz", "fc metz"],

    # Netherlands
    ["ajax", "afc ajax", "ajax amsterdam"],
    ["psv", "psv eindhoven"],
    ["feyenoord", "feyenoord rotterdam"],
    ["az alkmaar", "az"],

    # Portugal
    ["sporting lisbon", "sporting cp", "sporting"],
    ["benfica", "sl benfica"],
    ["porto", "fc porto"],

    # Turkey - Super Lig
    ["galatasaray", "galatasaray sk"],
    ["fenerbahce", "fenerbahçe", "fenerbahce sk"],
    ["besiktas", "beşiktaş", "besiktas jk"],
    ["trabzonspor", "trabzonspor fk"],
    ["basaksehir", "istanbul basaksehir", "başakşehir"],

    # Scotland
    ["celtic", "celtic fc", "celtic glasgow"],
    ["rangers", "rangers fc", "glasgow rangers"],

    # South Korea - K League
    ["jeonbuk hyundai motors", "jeonbuk", "jeonbuk motors"],
    ["ulsan hd", "ulsan hyundai", "ulsan"],
    ["pohang steelers", "pohang"],
    ["fc seoul", "seoul"],

    # China - Chinese Super League
    ["shanghai port", "shanghai port fc"],
    ["shanghai shenhua", "shenhua"],
    ["shandong taishan", "shandong luneng", "shandong"],
    ["beijing guoan", "beijing"],

    # International
    ["united states", "usa", "usmnt"],
    ["south korea", "korea republic", "korea"],
]

# Build lookup: any variant → canonical name (first in group)
_ALIAS_TO_CANONICAL: dict[str, str] = {}
_ALIAS_TO_GROUP: dict[str, set[str]] = {}

for group in ALIAS_GROUPS:
    canonical = group[0]
    group_set = set(group)
    for alias in group:
        _ALIAS_TO_CANONICAL[alias] = canonical
        _ALIAS_TO_GROUP[alias] = group_set


# Prefixes and suffixes commonly attached to team names
_AFFIXES = [
    "fc ", "ac ", "acf ", "as ", "afc ", "ssc ", "us ", "rcd ",
    "cd ", "rc ", "og ", "sc ", "fk ", "sk ", "bsc ", "vfb ",
    " fc", " sc", " cf", " ac", " as", " cd", " sv", " fk",
    " sk", " jk", " bk", " if", " afc",
]

# Noise tokens stripped before fuzzy comparison
_NOISE_TOKENS: set[str] = {
    # Club type abbreviations
    "fc", "ac", "sc", "rc", "us", "as", "cd", "cf", "sv", "fk",
    "sk", "jk", "bk", "if", "afc", "ssc", "rcd", "og", "bsc",
    "vfb", "acf", "hsc", "mk", "nk", "lok", "din", "hb", "kb",
    "bv", "vv", "hv", "ik", "pk", "rk", "gk", "ok",
    "de", "el", "la", "le", "les", "du", "des", "al",
}


def _ascii_lower(name: str) -> str:
    """Lowercase + strip accents → plain ASCII."""
    normalized = unicodedata.normalize("NFKD", name)
    return normalized.encode("ascii", "ignore").decode("ascii").strip().lower()


def _core_name(name: str) -> str:
    """
    Reduce a team name to its core tokens for fuzzy comparison:
    - strip accents (unicode NFKD → ASCII)
    - lowercase
    - remove noise words (FC, AC, City, Town, …)
    - remove standalone numbers
    """
    # Strip accents
    normalized = unicodedata.normalize("NFKD", name)
    ascii_name = normalized.encode("ascii", "ignore").decode("ascii")
    ascii_name = ascii_name.strip().lower()
    # Strip year suffixes
    ascii_name = re.sub(r'\s+\d{4}$', '', ascii_name)
    # Replace punctuation with spaces
    ascii_name = re.sub(r"[^\w\s]", " ", ascii_name)
    # Filter noise tokens and pure numbers
    tokens = [
        t for t in ascii_name.split()
        if t not in _NOISE_TOKENS and not t.isdigit()
    ]
    return " ".join(tokens)


def _fuzzy_cores_match(ca: str, cb: str, threshold: float = 0.82) -> bool:
    """True if two pre-computed core names refer to the same team."""
    if not ca or not cb:
        return False
    # One contains the other (e.g. "rangers" in "glasgow rangers")
    if ca in cb or cb in ca:
        return True
    return SequenceMatcher(None, ca, cb).ratio() >= threshold


def clean_team_name(name: str) -> set[str]:
    """
    Generate multiple normalized name variants for fuzzy matching.
    Returns a set of possible names for this team.
    """
    name = _ascii_lower(name)
    # Strip year suffixes like "1909", "2018"
    name = re.sub(r'\s+\d{4}$', '', name)

    variants = {name}

    # Generate stripped variants
    for affix in _AFFIXES:
        for v in list(variants):
            if v.startswith(affix):
                variants.add(v[len(affix):].strip())
            if v.endswith(affix):
                variants.add(v[:-len(affix)].strip())

    # Expand variants through aliases
    expanded = set()
    for v in variants:
        expanded.add(v)
        if v in _ALIAS_TO_GROUP:
            expanded.update(_ALIAS_TO_GROUP[v])

    expanded.discard("")
    return expanded


def canonicalize_team(name: str) -> str:
    """
    Return the canonical name for a team.
    If no alias match found, returns the cleaned lowercase ASCII name.
    """
    cleaned = _ascii_lower(name)
    cleaned = re.sub(r'\s+\d{4}$', '', cleaned)

    # Direct lookup
    if cleaned in _ALIAS_TO_CANONICAL:
        return _ALIAS_TO_CANONICAL[cleaned]

    # Try stripping affixes
    best_stripped = cleaned
    for affix in _AFFIXES:
        stripped = cleaned
        if stripped.startswith(affix):
            stripped = stripped[len(affix):].strip()
        if stripped.endswith(affix):
            stripped = stripped[:-len(affix)].strip()
        if stripped and stripped != cleaned:
            if stripped in _ALIAS_TO_CANONICAL:
                return _ALIAS_TO_CANONICAL[stripped]
            # Keep the shortest stripped version as fallback
            if len(stripped) < len(best_stripped):
                best_stripped = stripped

    return best_stripped


def team_found_in_text(team_variants: set[str], text: str) -> bool:
    """Check if any variant of a team name appears in the text."""
    text_lower = text.lower()
    for variant in team_variants:
        if len(variant) < 3:
            # Very short names need word-boundary matching to avoid false positives
            if re.search(r'\b' + re.escape(variant) + r'\b', text_lower):
                return True
        elif variant in text_lower:
            return True
    return False


def build_match_key(home: str, away: str, kickoff: Optional[datetime] = None) -> str:
    """
    Build a canonical key for cross-platform match deduplication.

    Uses canonical team names + kickoff date to ensure the same match
    produces the same key regardless of which platform it came from.
    """
    canonical_home = canonicalize_team(home)
    canonical_away = canonicalize_team(away)

    if kickoff:
        date_str = kickoff.strftime("%Y-%m-%d")
        return f"{canonical_home}_vs_{canonical_away}_{date_str}"
    return f"{canonical_home}_vs_{canonical_away}"


def parse_match_title(title: str) -> tuple[str, str]:
    """
    Parse a match title like "Liverpool FC - Chelsea FC" into (home, away).
    Handles various separator styles: " - ", " vs ", " vs. ", " – ", " — ".
    """
    normalized = title.replace(" vs. ", " - ").replace(" vs ", " - ") \
                      .replace(" v ", " - ").replace(" – ", " - ") \
                      .replace(" — ", " - ")
    parts = [t.strip() for t in normalized.split(" - ", 1)]
    if len(parts) >= 2:
        return parts[0], parts[1]
    return title.strip(), ""


def group_matches_by_event(
    all_matches: list[NormalizedMatch],
    kickoff_tolerance_hours: float = 48.0,
) -> list[CrossPlatformMatch]:
    """
    Group NormalizedMatch objects from different platforms into
    CrossPlatformMatch objects representing the same real-world match.

    Three-step matching (most to least precise):
      1. Exact canonical key (same teams + date) — no kickoff check needed
      2. Keyless canonical lookup (both orderings) + kickoff proximity
      3. Fuzzy core-name comparison + kickoff proximity
         (handles accent differences, noise words, city/town suffixes)

    Uses 48h tolerance because Polymarket stores market endDate while Kalshi
    stores game start time — these can differ by 24h+.
    """
    groups: dict[str, CrossPlatformMatch] = {}
    # keyless (no date) → list of full keys
    keyless_index: dict[str, list[str]] = {}
    # key → (core_home, core_away) for fuzzy fallback
    groups_cores: dict[str, tuple[str, str]] = {}

    for match in all_matches:
        key         = build_match_key(match.home_team, match.away_team, match.kickoff)
        keyless     = build_match_key(match.home_team, match.away_team)
        keyless_rev = build_match_key(match.away_team, match.home_team)

        # Step 1: exact key match
        if key in groups:
            groups[key].platform_data[match.platform] = match
            continue

        # Step 2: keyless canonical lookup (both home/away orderings)
        found_key = None
        for kl in (keyless, keyless_rev):
            for existing_key in keyless_index.get(kl, []):
                if _kickoffs_close(
                    match.kickoff, groups[existing_key].kickoff, kickoff_tolerance_hours
                ):
                    found_key = existing_key
                    break
            if found_key:
                break

        # Step 3: fuzzy core-name comparison
        if not found_key:
            mch = _core_name(match.home_team)
            mca = _core_name(match.away_team)
            for gkey, (gh, ga) in groups_cores.items():
                if not _kickoffs_close(
                    match.kickoff, groups[gkey].kickoff, kickoff_tolerance_hours
                ):
                    continue
                if (
                    (_fuzzy_cores_match(mch, gh) and _fuzzy_cores_match(mca, ga))
                    or (_fuzzy_cores_match(mch, ga) and _fuzzy_cores_match(mca, gh))
                ):
                    found_key = gkey
                    break

        if found_key:
            groups[found_key].platform_data[match.platform] = match
            # Prefer Polymarket kickoff since it natively provides precise gameStartTime,
            # whereas Kalshi often guesses by subtracting 2 hours from expected expiration.
            if match.platform == "polymarket" and match.kickoff:
                groups[found_key].kickoff = match.kickoff
            # Fallback to kalshi only if we don't already have a valid kickoff
            elif match.platform == "kalshi" and match.kickoff and not groups[found_key].kickoff:
                groups[found_key].kickoff = match.kickoff
            continue

        # New group
        groups[key] = CrossPlatformMatch(
            match_key=key,
            home_team=canonicalize_team(match.home_team),
            away_team=canonicalize_team(match.away_team),
            kickoff=match.kickoff,
            league=match.league,
        )
        groups[key].platform_data[match.platform] = match
        for kl in (keyless, keyless_rev):
            keyless_index.setdefault(kl, []).append(key)
        groups_cores[key] = (_core_name(match.home_team), _core_name(match.away_team))

    # Return all grouped matches
    return list(groups.values())


def _kickoffs_close(
    dt1: Optional[datetime],
    dt2: Optional[datetime],
    tolerance_hours: float,
) -> bool:
    """Check if two kickoff times are within tolerance of each other."""
    if dt1 is None or dt2 is None:
        return True  # If either is unknown, assume they could match
    diff = abs((dt1 - dt2).total_seconds())
    return diff <= tolerance_hours * 3600
