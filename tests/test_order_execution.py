import unittest
from unittest.mock import MagicMock, patch
import json
import pytest
import sys
sys.modules['redis'] = MagicMock()

from polymarket_client import PolymarketClient
from kalshi_client import KalshiClient
from py_clob_client.clob_types import OrderType

class TestOrderExecution(unittest.TestCase):
    @patch('polymarket_client.PolymarketClient._get_clob_client')
    def test_polymarket_place_order_uses_fok(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.post_order.return_value = {"orderID": "123"}
        mock_get_client.return_value = mock_client

        poly = PolymarketClient()
        order_id = poly.place_order("token123", "BUY", 10.0, 0.45, price_bump=0.01)

        self.assertEqual(order_id, "123")
        # should have posted FOK order
        mock_client.post_order.assert_called_once()
        args, kwargs = mock_client.post_order.call_args
        self.assertEqual(args[1], OrderType.FOK)
        
        # passed price is 0.45 + 0.01 = 0.46
        order_args = mock_client.create_order.call_args[0][0]
        self.assertAlmostEqual(order_args.price, 0.46)
        
        # size is 10.0 / 0.46 = 21.74
        self.assertAlmostEqual(order_args.size, 21.74)

    @patch('kalshi_client.KalshiClient._rate_limit')
    @patch('kalshi_client.KalshiClient._make_auth_headers')
    def test_kalshi_place_order_uses_ioc(self, mock_auth, mock_rate_limit):
        mock_auth.return_value = {}
        kalshi = KalshiClient({"api_key_id": "test", "private_key_path": "fake.pem"})
        
        # mock private key so it passes the None check
        kalshi._private_key = MagicMock()
        kalshi.session = MagicMock()
        
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"order": {"order_id": "k123"}}
        kalshi.session.post.return_value = mock_resp
        
        order_id = kalshi.place_order("TICKER", "yes", 10, 45, price_bump_cents=2)
        
        self.assertEqual(order_id, "k123")
        kalshi.session.post.assert_called_once()
        call_kwargs = kalshi.session.post.call_args[1]
        
        body = call_kwargs["json"]
        self.assertEqual(body["yes_price"], 47) # 45 + 2
        self.assertEqual(body["type"], "limit")
        self.assertEqual(body["time_in_force"], "ioc")

    @patch('polymarket_client.requests.Session.get')
    def test_get_clob_ask_price(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"asks": [{"price": "0.48"}, {"price": "0.45"}, {"price": "0.50"}]}
        mock_get.return_value = mock_resp

        poly = PolymarketClient()
        lowest_ask = poly.get_clob_ask_price("token_abc")
        self.assertEqual(lowest_ask, 0.45)
