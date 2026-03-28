import unittest
from unittest.mock import MagicMock, patch
import sys
sys.modules['redis'] = MagicMock()

from polymarket_client import PolymarketClient
from kalshi_client import KalshiClient
from py_clob_client.clob_types import OrderType

class TestOrderExecution(unittest.TestCase):
    @patch('polymarket_client.PolymarketClient._get_clob_client')
    def test_polymarket_buy_place_order_uses_market_fok(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.post_order.return_value = {"orderID": "123"}
        mock_get_client.return_value = mock_client

        poly = PolymarketClient()
        order_id = poly.place_order("token123", "BUY", 10.0, 0.45, price_bump=0.01)

        self.assertEqual(order_id, "123")
        # should have posted FOK order
        mock_client.post_order.assert_called_once()
        args = mock_client.post_order.call_args[0]
        self.assertEqual(args[1], OrderType.FOK)

        order_args = mock_client.create_market_order.call_args[0][0]
        self.assertAlmostEqual(order_args.price, 0.46)
        self.assertAlmostEqual(order_args.amount, 10.0)
        self.assertEqual(order_args.order_type, OrderType.FOK)
        mock_client.create_order.assert_not_called()

    @patch('polymarket_client.PolymarketClient._get_clob_client')
    def test_polymarket_buy_place_order_rounds_budget_to_cents(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.post_order.return_value = {"orderID": "123"}
        mock_get_client.return_value = mock_client

        poly = PolymarketClient()
        poly.place_order("token123", "BUY", 10.019, 0.45, price_bump=0.01)

        order_args = mock_client.create_market_order.call_args[0][0]
        self.assertAlmostEqual(order_args.amount, 10.01)

    @patch('polymarket_client.PolymarketClient._get_clob_client')
    def test_polymarket_sell_place_order_uses_share_size(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.post_order.return_value = {"orderID": "sell-123"}
        mock_get_client.return_value = mock_client

        poly = PolymarketClient()
        ok = poly.sell_position("token123", 41, 0.10)

        self.assertTrue(ok)
        order_args = mock_client.create_order.call_args[0][0]
        self.assertAlmostEqual(order_args.size, 41.0)
        self.assertAlmostEqual(order_args.price, 0.11)

    @patch('kalshi_client.KalshiClient._rate_limit')
    @patch('kalshi_client.KalshiClient._make_auth_headers')
    def test_kalshi_place_order_uses_current_v2_schema(self, mock_auth, _mock_rate_limit):
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
        self.assertEqual(body["buy_max_cost"], 470)
        self.assertNotIn("time_in_force", body)

    @patch('polymarket_client.requests.Session.get')
    def test_get_clob_ask_price(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"asks": [{"price": "0.48"}, {"price": "0.45"}, {"price": "0.50"}]}
        mock_get.return_value = mock_resp

        poly = PolymarketClient()
        lowest_ask = poly.get_clob_ask_price("token_abc")
        self.assertEqual(lowest_ask, 0.45)
