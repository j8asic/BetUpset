"""
Tests for the team name matching module.
"""

import pytest
from datetime import datetime, timezone

from matching import (
    clean_team_name,
    canonicalize_team,
    team_found_in_text,
    build_match_key,
    parse_match_title,
    group_matches_by_event,
)
from platform_base import NormalizedMatch


class TestCleanTeamName:
    def test_basic_name(self):
        variants = clean_team_name("Liverpool")
        assert "liverpool" in variants

    def test_strips_fc_prefix(self):
        variants = clean_team_name("FC Barcelona")
        assert "barcelona" in variants

    def test_strips_fc_suffix(self):
        variants = clean_team_name("Chelsea FC")
        assert "chelsea" in variants

    def test_alias_expansion(self):
        variants = clean_team_name("Man Utd")
        assert "manchester united" in variants
        assert "man united" in variants

    def test_psg_aliases(self):
        variants = clean_team_name("PSG")
        assert "paris saint-germain" in variants
        assert "psg" in variants

    def test_strips_year(self):
        variants = clean_team_name("Liverpool 2024")
        assert "liverpool" in variants


class TestCanonicalizeTeam:
    def test_direct_match(self):
        assert canonicalize_team("Man Utd") == "manchester united"

    def test_with_prefix(self):
        assert canonicalize_team("FC Bayern Munich") == "bayern munich"

    def test_unknown_team(self):
        assert canonicalize_team("Unknown FC") == "unknown"

    def test_already_canonical(self):
        assert canonicalize_team("manchester united") == "manchester united"

    def test_psg(self):
        assert canonicalize_team("PSG") == "paris saint-germain"

    def test_wolves(self):
        assert canonicalize_team("Wolves") == "wolverhampton wanderers"


class TestTeamFoundInText:
    def test_found(self):
        variants = clean_team_name("Liverpool")
        assert team_found_in_text(variants, "Will Liverpool win?")

    def test_not_found(self):
        variants = clean_team_name("Liverpool")
        assert not team_found_in_text(variants, "Will Chelsea win?")

    def test_alias_found(self):
        variants = clean_team_name("Man City")
        assert team_found_in_text(variants, "Manchester City to win the league")

    def test_short_name_boundary(self):
        """Short names like 'az' need word-boundary matching."""
        variants = clean_team_name("AZ Alkmaar")
        assert team_found_in_text(variants, "AZ Alkmaar vs PSV")
        # Should NOT match "crazy" just because it contains "az"
        assert not team_found_in_text({"az"}, "This is crazy")


class TestBuildMatchKey:
    def test_basic(self):
        key = build_match_key("Liverpool FC", "Chelsea FC")
        assert "liverpool" in key
        assert "chelsea" in key
        assert "_vs_" in key

    def test_with_date(self):
        dt = datetime(2026, 3, 20, 20, 0, tzinfo=timezone.utc)
        key = build_match_key("Liverpool", "Chelsea", dt)
        assert "2026-03-20" in key

    def test_canonical_names(self):
        """Different name variants should produce the same key."""
        key1 = build_match_key("Man Utd", "Spurs")
        key2 = build_match_key("Manchester United", "Tottenham Hotspur")
        assert key1 == key2

    def test_psg_vs_marseille(self):
        key1 = build_match_key("PSG", "OM")
        key2 = build_match_key("Paris Saint-Germain", "Olympique Marseille")
        assert key1 == key2


class TestParseMatchTitle:
    def test_dash_separator(self):
        home, away = parse_match_title("Liverpool FC - Chelsea FC")
        assert home == "Liverpool FC"
        assert away == "Chelsea FC"

    def test_vs_separator(self):
        home, away = parse_match_title("Liverpool vs Chelsea")
        assert home == "Liverpool"
        assert away == "Chelsea"

    def test_en_dash(self):
        home, away = parse_match_title("Liverpool FC – Chelsea FC")
        assert home == "Liverpool FC"
        assert away == "Chelsea FC"

    def test_no_separator(self):
        home, away = parse_match_title("Liverpool")
        assert home == "Liverpool"
        assert away == ""


class TestGroupMatchesByEvent:
    def test_groups_same_match(self):
        """Matches from different platforms with same teams should group."""
        poly = NormalizedMatch(
            platform="polymarket",
            platform_market_id="p1",
            home_team="Liverpool",
            away_team="Chelsea",
            kickoff=None,
            league="Premier League",
            prices={"home": 0.55, "draw": 0.25, "away": 0.22},
        )
        kalshi = NormalizedMatch(
            platform="kalshi",
            platform_market_id="k1",
            home_team="Liverpool FC",
            away_team="Chelsea FC",
            kickoff=None,
            league="EPL",
            prices={"home": 0.52, "draw": 0.27, "away": 0.24},
        )
        groups = group_matches_by_event([poly, kalshi])
        assert len(groups) == 1
        assert "polymarket" in groups[0].platform_data
        assert "kalshi" in groups[0].platform_data

    def test_different_matches_separate(self):
        """Different matches should not be grouped."""
        match1 = NormalizedMatch(
            platform="polymarket",
            platform_market_id="p1",
            home_team="Liverpool", away_team="Chelsea",
            kickoff=None, league="EPL",
            prices={"home": 0.55},
        )
        match2 = NormalizedMatch(
            platform="kalshi",
            platform_market_id="k1",
            home_team="Arsenal", away_team="Tottenham",
            kickoff=None, league="EPL",
            prices={"home": 0.50},
        )
        groups = group_matches_by_event([match1, match2])
        # Single-platform matches are filtered out
        assert len(groups) == 0

    def test_single_platform_excluded(self):
        """A match only on one platform should be excluded."""
        match = NormalizedMatch(
            platform="polymarket",
            platform_market_id="p1",
            home_team="Liverpool", away_team="Chelsea",
            kickoff=None, league="EPL",
            prices={"home": 0.55},
        )
        groups = group_matches_by_event([match])
        assert len(groups) == 0
