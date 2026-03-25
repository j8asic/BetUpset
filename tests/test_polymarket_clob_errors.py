import unittest
from unittest.mock import MagicMock, patch
import sys

def mock_dependencies():
    """Mock missing dependencies that are not installed in the environment."""
    if 'requests' not in sys.modules:
        sys.modules['requests'] = MagicMock()
    if 'py_clob_client' not in sys.modules:
        sys.modules['py_clob_client'] = MagicMock()
        sys.modules['py_clob_client.client'] = MagicMock()
        sys.modules['py_clob_client.clob_types'] = MagicMock()
    if 'redis' not in sys.modules:
        sys.modules['redis'] = MagicMock()

# Ensure dependencies are mocked before importing PolymarketClient
mock_dependencies()
import requests
from polymarket_client import PolymarketClient

class TestPolymarketClobAskPrice(unittest.TestCase):

    def setUp(self):
        # Reset the mock for each test
        # We need to reach into the session created by PolymarketClient
        self.poly = PolymarketClient()
        # Mock the session.get method specifically for each test
        self.poly.session.get = MagicMock()

    def test_get_clob_ask_price_success(self):
        """Test happy path with valid asks."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "asks": [
                {"price": "0.55"},
                {"price": "0.45"},
                {"price": "0.50"}
            ]
        }
        self.poly.session.get.return_value = mock_resp

        result = self.poly.get_clob_ask_price("token_123")
        self.assertEqual(result, 0.45)

    def test_get_clob_ask_price_http_error(self):
        """Test non-200 HTTP status code."""
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        self.poly.session.get.return_value = mock_resp

        result = self.poly.get_clob_ask_price("token_123")
        self.assertIsNone(result)

    def test_get_clob_ask_price_exception(self):
        """Test exception during the request."""
        self.poly.session.get.side_effect = Exception("Connection error")

        result = self.poly.get_clob_ask_price("token_123")
        self.assertIsNone(result)

    def test_get_clob_ask_price_empty_asks(self):
        """Test empty asks list in response."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"asks": []}
        self.poly.session.get.return_value = mock_resp

        result = self.poly.get_clob_ask_price("token_123")
        self.assertIsNone(result)

    def test_get_clob_ask_price_malformed_json(self):
        """Test response with missing 'asks' key."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"something_else": []}
        self.poly.session.get.return_value = mock_resp

        result = self.poly.get_clob_ask_price("token_123")
        self.assertIsNone(result)

    def test_get_clob_ask_price_malformed_ask_item(self):
        """Test asks list with items missing 'price' key."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"asks": [{"not_price": "0.45"}]}
        self.poly.session.get.return_value = mock_resp

        result = self.poly.get_clob_ask_price("token_123")
        self.assertIsNone(result)

    def test_get_clob_ask_price_invalid_price_value(self):
        """Test asks list with invalid price values."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"asks": [{"price": "invalid"}]}
        self.poly.session.get.return_value = mock_resp

        result = self.poly.get_clob_ask_price("token_123")
        self.assertIsNone(result)

if __name__ == '__main__':
    unittest.main()
