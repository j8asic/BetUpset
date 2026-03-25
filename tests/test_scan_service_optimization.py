
import json
import unittest
import sys
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass
from unittest.mock import MagicMock

# Mock all dependencies
sys.modules['dotenv'] = MagicMock()
sys.modules['py_clob_client'] = MagicMock()
sys.modules['py_clob_client.clob_types'] = MagicMock()
sys.modules['py_clob_client.client'] = MagicMock()
sys.modules['requests'] = MagicMock()

# Mocking modules that might be imported and cause issues due to missing dependencies
sys.modules['polymarket_client'] = MagicMock()
sys.modules['kalshi_client'] = MagicMock()
sys.modules['main'] = MagicMock()
sys.modules['scanner'] = MagicMock()
sys.modules['config'] = MagicMock()
sys.modules['detector'] = MagicMock()

import scan_service
from scan_service import run_scan
from platform_base import NormalizedMatch, CrossPlatformMatch

@dataclass
class MockPlatformClient:
    name: str
    _event_price_snapshot: dict = None

    def get_pre_kickoff_price(self, token_id, kickoff):
        print(f"DEBUG: get_pre_kickoff_price({token_id}, {kickoff}) called")
        return 0.55

class TestScanServiceOptimization(unittest.TestCase):
    def test_run_scan_pre_kickoff_logic(self):
        # Setup mock platforms
        poly_client = MockPlatformClient("polymarket", _event_price_snapshot={})
        kalshi_client = MockPlatformClient("kalshi")

        # Setup mock matches
        now = datetime.now(timezone.utc)
        kickoff = now - timedelta(hours=1)

        # Match that needs CLOB fallback
        poly_match = NormalizedMatch(
            platform="polymarket",
            platform_market_id=json.dumps({
                "_event_slug": "test-event",
                "_clob_tokens": {"home": "token-h", "away": "token-a"}
            }),
            home_team="Team A",
            away_team="Team B",
            kickoff=kickoff,
            league="",
            prices={"home": 0.6, "away": 0.4},
            liquidity={"home": 100, "away": 100},
            pre_kickoff_prices=None
        )

        kalshi_match = NormalizedMatch(
            platform="kalshi",
            platform_market_id="{}",
            home_team="Team A",
            away_team="Team B",
            kickoff=kickoff,
            league="",
            prices={"home": 0.7, "away": 0.3},
            liquidity={"home": 100, "away": 100}
        )

        match = CrossPlatformMatch(
            match_key="2024-01-01:team-a-vs-team-b",
            home_team="Team A",
            away_team="Team B",
            kickoff=kickoff,
            league="Premier League",
            platform_data={"polymarket": poly_match, "kalshi": kalshi_match}
        )

        # Add dummy attributes to scan_service to satisfy mock.patch
        scan_service.Scanner = MagicMock()
        scan_service.load_config = MagicMock()
        scan_service.detect_opportunity = MagicMock()
        scan_service.initialize_platforms = MagicMock()
        scan_service.generate_demo_matches = MagicMock()

        # Mock dependencies in run_scan
        scan_service._platforms = [poly_client, kalshi_client]

        with unittest.mock.patch("scan_service.detect_opportunity") as mock_detect:
            mock_detect.return_value = None
            with unittest.mock.patch("scan_service.Scanner") as mock_scanner_cls:
                mock_scanner = mock_scanner_cls.return_value
                mock_scanner.scan.return_value = [match]
                with unittest.mock.patch("scan_service.load_config") as mock_load_config:
                    mock_load_config.return_value = MagicMock()

                    print(f"DEBUG: Before run_scan. poly_match.pre_kickoff_prices={poly_match.pre_kickoff_prices}")
                    # In scan_service.py:
                    # poly_match = match.platform_data.get("polymarket")
                    # In my test, poly_match is match.platform_data["polymarket"]

                    rows, count = run_scan(demo=False)
                    print(f"DEBUG: After run_scan. poly_match.pre_kickoff_prices={poly_match.pre_kickoff_prices}")

                    # Verify pre_kickoff_prices was populated correctly
                    self.assertIsNotNone(poly_match.pre_kickoff_prices)
                    self.assertEqual(poly_match.pre_kickoff_prices.get("home"), 0.55)
                    self.assertEqual(poly_match.pre_kickoff_prices.get("away"), 0.55)

if __name__ == "__main__":
    unittest.main()
