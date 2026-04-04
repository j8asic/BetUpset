import time
import json
from unittest.mock import patch, MagicMock

import web
from platform_base import PlatformClient
from kalshi_client import KalshiClient
from polymarket_client import PolymarketClient

def run_benchmark():
    # We will mock _get_platform_clients and KalshiClient methods
    mock_kalshi = MagicMock(spec=KalshiClient)
    mock_poly = MagicMock(spec=PolymarketClient)

    def fake_get_platform_clients():
        return {"kalshi": mock_kalshi, "polymarket": mock_poly}

    web._get_platform_clients = fake_get_platform_clients

    # Set up dummy bet with multiple kalshi positions
    kalshi_ids = {
        "home": "TICKER-HOME",
        "draw": "TICKER-DRAW",
        "away": "TICKER-AWAY",
        "some_other": "TICKER-OTHER1",
        "another": "TICKER-OTHER2",
    }

    bet = {
        "kalshi_market_id": json.dumps(kalshi_ids),
        "poly_market_id": "",
    }

    def fake_get_position(ticker, side):
        # Always return 1 to simulate we hold a position, triggering the _get call
        return 1

    mock_kalshi.get_position.side_effect = fake_get_position

    def fake_get(endpoint, **kwargs):
        # Simulate network delay of 50ms
        time.sleep(0.05)
        return {
            "market": {
                "yes_bid": 40,
                "yes_ask": 60,
            }
        }

    mock_kalshi._get.side_effect = fake_get

    def fake_sell_position(ticker, side, count, price_cents):
        return True

    mock_kalshi.sell_position.side_effect = fake_sell_position

    print("Running baseline benchmark...")
    start_time = time.perf_counter()
    results = web._sell_bet_positions(bet)
    end_time = time.perf_counter()

    elapsed = (end_time - start_time) * 1000
    print(f"Time taken: {elapsed:.2f} ms")
    print("Results:", results)

    # Assert expected calls
    # 5 tickers * 2 sides = 10 positions -> 10 _get calls (each taking 50ms = 500ms total minimum)
    print(f"kalshi._get called {mock_kalshi._get.call_count} times")

if __name__ == "__main__":
    run_benchmark()
