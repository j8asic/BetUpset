"""
Tests for team order prioritization and harmonization in matching.py.
"""

import pytest
from datetime import datetime, timezone
from matching import group_matches_by_event
from platform_base import NormalizedMatch

def test_polymarket_prioritization_same_order():
    """If Polymarket and Kalshi have the same order, the group should keep it."""
    poly = NormalizedMatch(
        platform="polymarket",
        platform_market_id="p1",
        home_team="Egypt",
        away_team="Spain",
        kickoff=datetime(2026, 3, 25, 20, 0, tzinfo=timezone.utc),
        league="International",
        prices={"home": 0.4, "draw": 0.3, "away": 0.3},
    )
    kalshi = NormalizedMatch(
        platform="kalshi",
        platform_market_id="k1",
        home_team="Egypt",
        away_team="Spain",
        kickoff=datetime(2026, 3, 25, 20, 0, tzinfo=timezone.utc),
        league="International",
        prices={"home": 0.42, "draw": 0.28, "away": 0.30},
    )
    
    # Process Kalshi first, then Poly
    groups = group_matches_by_event([kalshi, poly])
    assert len(groups) == 1
    assert groups[0].home_team == "egypt"
    assert groups[0].away_team == "spain"
    assert groups[0].platform_data["polymarket"].prices["home"] == 0.4
    assert groups[0].platform_data["kalshi"].prices["home"] == 0.42

def test_polymarket_prioritization_reversed_order():
    """If Kalshi is reversed but Poly has correct order, Poly should win."""
    # Egypt vs Spain (Correct)
    poly = NormalizedMatch(
        platform="polymarket",
        platform_market_id="p1",
        home_team="Egypt",
        away_team="Spain",
        kickoff=datetime(2026, 3, 25, 20, 0, tzinfo=timezone.utc),
        league="International",
        prices={"home": 0.4, "draw": 0.3, "away": 0.3},
    )
    # Spain vs Egypt (Reversed)
    kalshi = NormalizedMatch(
        platform="kalshi",
        platform_market_id="k1",
        home_team="Spain",
        away_team="Egypt",
        kickoff=datetime(2026, 3, 25, 20, 0, tzinfo=timezone.utc),
        league="International",
        prices={"home": 0.30, "draw": 0.28, "away": 0.42}, # Home is Spain (0.3)
    )
    
    # Process Kalshi first (group created as Spain vs Egypt)
    # Then Poly (group should REORIENT to Egypt vs Spain)
    groups = group_matches_by_event([kalshi, poly])
    
    assert len(groups) == 1
    assert groups[0].home_team == "egypt"
    assert groups[0].away_team == "spain"
    
    # Poly prices should be unchanged
    assert groups[0].platform_data["polymarket"].prices["home"] == 0.4
    assert groups[0].platform_data["polymarket"].prices["away"] == 0.3
    
    # Kalshi prices should be FLIPPED to match group order (Egypt = Home)
    # Original Kalshi: Egypt was Away (0.42), so new Home price should be 0.42
    assert groups[0].platform_data["kalshi"].prices["home"] == 0.42
    assert groups[0].platform_data["kalshi"].prices["away"] == 0.30

def test_kalshi_only_keeps_order():
    """If only Kalshi is present, it defines the order even if later we'd prefer something else."""
    kalshi = NormalizedMatch(
        platform="kalshi",
        platform_market_id="k1",
        home_team="Spain",
        away_team="Egypt",
        kickoff=datetime(2026, 3, 25, 20, 0, tzinfo=timezone.utc),
        league="International",
        prices={"home": 0.30, "draw": 0.28, "away": 0.42},
    )
    groups = group_matches_by_event([kalshi])
    assert len(groups) == 1
    assert groups[0].home_team == "spain"
    assert groups[0].away_team == "egypt"

def test_alias_matching_international():
    """Verify that EGY/ESP aliases work."""
    poly = NormalizedMatch(
        platform="polymarket",
        platform_market_id="p1",
        home_team="Egypt",
        away_team="Spain",
        kickoff=datetime(2026, 3, 25, 20, 0, tzinfo=timezone.utc),
        league="International",
        prices={"home": 0.5},
    )
    kalshi = NormalizedMatch(
        platform="kalshi",
        platform_market_id="k1",
        home_team="ESP",
        away_team="EGY",
        kickoff=datetime(2026, 3, 25, 20, 0, tzinfo=timezone.utc),
        league="International",
        prices={"home": 0.4}, # ESP win
    )
    
    groups = group_matches_by_event([poly, kalshi])
    assert len(groups) == 1
    assert groups[0].home_team == "egypt"
    assert groups[0].away_team == "spain"
    # Kalshi was ESP vs EGY, now harmonized to EGY vs ESP
    # So Kalshi "home" (EGY) should be 0.6 if we only had binary, but here we have ESP=0.4
    # Wait, Kalshi home was ESP (0.4). Now Home is EGY. So new Away should be 0.4.
    assert groups[0].platform_data["kalshi"].prices["away"] == 0.4
