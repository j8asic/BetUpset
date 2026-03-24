"""
Kalshi API client for fetching prediction market data.

Kalshi is a CFTC-regulated exchange with strong soccer coverage across
EPL, La Liga, Serie A, Bundesliga, Ligue 1, and UCL.

Prices come from the orderbook (yes_dollars / no_dollars fields).
The market summary fields (yes_bid, last_price) are often None for
newer markets, so we always fall back to the orderbook.

API docs: https://docs.kalshi.com
Auth: API key ID + RSA private key for request signing.
Base URL: https://api.elections.kalshi.com/trade-api/v2
"""

import json
import os
import re
import threading
import time
from base64 import b64encode
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Optional

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding

from platform_base import PlatformClient, NormalizedMatch
from matching import clean_team_name, team_found_in_text, parse_match_title


class KalshiClient(PlatformClient):
    """Client for the Kalshi prediction market exchange."""

    BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

    def __init__(self, credentials: Optional[dict] = None):
        self._credentials = credentials or {}
        self._api_key_id = (
            self._credentials.get("api_key_id", "")
            or os.environ.get("KALSHI_API_KEY", "")
        )
        self._private_key_path = (
            self._credentials.get("private_key_path", "")
            or os.environ.get("KALSHI_PEM_PATH", "")
        )

        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "BetUpset/2.0",
        })

        self._last_request_time = 0
        self._min_request_interval = 0.25  # Rate limit (stay under Kalshi's ~10 req/s)
        self._rate_lock = threading.Lock()  # Serialise concurrent thread access to rate limiter
        self._token: Optional[str] = None
        self._token_expiry: float = 0
        self._private_key = None

        # Auth on init if credentials available
        if self._api_key_id:
            self._authenticate()

    # ================================================================
    # PlatformClient interface
    # ================================================================

    @property
    def name(self) -> str:
        return "kalshi"

    def fetch_soccer_markets(self) -> list[NormalizedMatch]:
        """
        Fetch all available soccer markets from Kalshi.

        Strategy: batch-fetch markets per series (includes dollar prices),
        group by event_ticker, and normalize into NormalizedMatch objects.
        All 19 series are fetched concurrently to minimise wall-clock time.
        """
        all_markets: dict[str, list[dict]] = {}  # event_ticker -> [markets]

        def fetch_series(series: str) -> dict[str, list[dict]]:
            series_markets: dict[str, list[dict]] = {}
            cursor = None
            while True:
                params: dict = {
                    "series_ticker": series,
                    "status": "open",
                    "limit": 200,
                }
                if cursor:
                    params["cursor"] = cursor
                data = self._get("/markets", params=params)
                if not data or "markets" not in data:
                    break
                for m in data["markets"]:
                    event_ticker = m.get("event_ticker", "")
                    if event_ticker:
                        m["_series_ticker"] = series
                        series_markets.setdefault(event_ticker, []).append(m)
                cursor = data.get("cursor")
                if not cursor or len(data["markets"]) < 200:
                    break
            return series_markets

        with ThreadPoolExecutor(max_workers=len(self.SOCCER_SERIES)) as ex:
            futures = {ex.submit(fetch_series, s): s for s in self.SOCCER_SERIES}
            for future in as_completed(futures):
                try:
                    for event_ticker, markets in future.result().items():
                        all_markets.setdefault(event_ticker, []).extend(markets)
                except Exception as e:
                    print(f"[Kalshi] Error fetching series {futures[future]}: {e}")

        print(f"[Kalshi] Found {len(all_markets)} soccer events")

        results = []
        for event_ticker, markets in all_markets.items():
            match = self._markets_to_normalized_match(event_ticker, markets)
            if match:
                results.append(match)

        print(f"[Kalshi] Found {len(results)} normalized soccer matches")
        return results

    def get_market_prices(self, market_id: str) -> Optional[dict[str, float]]:
        """Get current prices for a Kalshi market (ticker)."""
        data = self._get(f"/markets/{market_id}")
        if not data or "market" not in data:
            return None

        market = data["market"]
        yes_price = self._extract_dollar_price(market)
        if not yes_price:
            return None

        return {
            "yes": yes_price,
            "no": 1.0 - yes_price,
            "volume": float(market.get("volume_fp") or 0),
        }

    def get_liquidity(self, market_id: str, outcome: str) -> float:
        """Check order book depth for a market outcome."""
        data = self._get(f"/markets/{market_id}/orderbook")
        if not data:
            return 0.0

        side = "yes" if outcome in ("home", "yes") else "no"
        orders = data.get(side, [])
        total_liquidity = sum(
            order.get("quantity", 0) * order.get("price", 0) / 100.0
            for order in orders
        )
        return total_liquidity

    # ================================================================
    # Authentication
    # ================================================================

    def _authenticate(self):
        """
        Authenticate with Kalshi using API key + RSA private key signing.
        Kalshi uses a login endpoint that returns a session token.
        """
        if not self._api_key_id:
            return

        try:
            # Try RSA-signed authentication
            if self._private_key_path and os.path.exists(self._private_key_path):
                self._authenticate_rsa()
            else:
                # Fallback: API key in header (for read-only access)
                self.session.headers["Authorization"] = f"Bearer {self._api_key_id}"
                print("[Kalshi] Using API key for read-only access")
        except Exception as e:
            print(f"[Kalshi] Auth failed: {e}")

    def _authenticate_rsa(self):
        """Load RSA private key for per-request PSS signing (per official Kalshi SDK)."""
        try:
            with open(self._private_key_path, "rb") as f:
                self._private_key = serialization.load_pem_private_key(f.read(), password=None)
            print("[Kalshi] RSA key loaded")
        except Exception as e:
            print(f"[Kalshi] RSA key load error: {e}")
            self._private_key = None

    # ================================================================
    # HTTP methods
    # ================================================================

    def _rate_limit(self):
        """Ensure we don't exceed rate limits. Thread-safe: serialises concurrent callers."""
        with self._rate_lock:
            elapsed = time.time() - self._last_request_time
            if elapsed < self._min_request_interval:
                time.sleep(self._min_request_interval - elapsed)
            self._last_request_time = time.time()

    def _get(self, endpoint: str, params: Optional[dict] = None) -> Optional[dict]:
        """Make a GET request to the Kalshi API."""
        self._rate_limit()

        url = f"{self.BASE_URL}{endpoint}"
        try:
            response = self.session.get(url, params=params, timeout=30)
            if response.status_code == 401:
                print("[Kalshi] Unauthorized - check API credentials")
                return None
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            print(f"[Kalshi] API error: {e}")
            return None

    # ================================================================
    # Soccer market discovery
    # ================================================================

    # Known Kalshi series tickers for soccer match-winner markets.
    # Each series contains 3-way events (Home / Tie / Away).
    SOCCER_SERIES = [
        # Major European leagues
        "KXEPLGAME",              # English Premier League
        "KXLALIGAGAME",           # La Liga
        "KXSERIEAGAME",           # Serie A
        "KXBUNDESLIGAGAME",       # Bundesliga
        "KXLIGUE1GAME",           # Ligue 1
        "KXEREDIVISIEGAME",       # Eredivisie
        # International club competitions
        "KXUCLGAME",              # UEFA Champions League
        "KXUELGAME",              # UEFA Europa League
        "KXUECLGAME",             # UEFA Conference League
        "KXUEFAGAME",             # Generic UEFA games
        "KXAFCCLGAME",            # AFC Champions League
        "KXCONCACAFCCUPGAME",     # CONCACAF Champions Cup
        # International tournaments
        "KXWCGAME",               # FIFA World Cup
        "KXINTLFRIENDLYGAME",     # International Friendlies
        # Domestic cups
        "KXEFLCUPGAME",           # EFL Cup (Carabao Cup)
        "KXCOPADELREYGAME",       # Copa del Rey
        # Global domestic leagues
        "KXMLSGAME",              # MLS
        "KXLIGAMXGAME",           # Liga MX
        "KXBRASILEIROGAME",       # Brasileiro Serie A
        "KXLIGAPORTUGALGAME",     # Liga Portugal
        "KXSCOTTISHPREMGAME",     # Scottish Premiership
        "KXEFLCHAMPIONSHIPGAME",  # EFL Championship
        "KXDANISHSUPERLIGAGAME",  # Danish Superliga
        "KXSUPERLIGGAME",         # Turkish Super Lig
        "KXCHNSLGAME",            # Chinese Super League
        "KXDIMAYORGAME",          # Colombian Liga DIMAYOR
        "KXCHLLDPGAME",           # Chile Liga de Primera
        "KXPERLIGA1GAME",         # Peru Liga 1
        "KXKLEAGUEGAME",          # Korea K League
    ]

    # Map series ticker prefix to league name for display
    SERIES_TO_LEAGUE = {
        "KXEPLGAME": "EPL",
        "KXLALIGAGAME": "La Liga",
        "KXSERIEAGAME": "Serie A",
        "KXBUNDESLIGAGAME": "Bundesliga",
        "KXLIGUE1GAME": "Ligue 1",
        "KXEREDIVISIEGAME": "Eredivisie",
        "KXUCLGAME": "UCL",
        "KXUELGAME": "Europa League",
        "KXUECLGAME": "Conference League",
        "KXUEFAGAME": "UEFA",
        "KXAFCCLGAME": "AFC CL",
        "KXCONCACAFCCUPGAME": "CONCACAF",
        "KXWCGAME": "World Cup",
        "KXINTLFRIENDLYGAME": "Intl Friendlies",
        "KXEFLCUPGAME": "EFL Cup",
        "KXCOPADELREYGAME": "Copa del Rey",
        "KXMLSGAME": "MLS",
        "KXLIGAMXGAME": "Liga MX",
        "KXBRASILEIROGAME": "Brasileiro",
        "KXLIGAPORTUGALGAME": "Liga Portugal",
        "KXSCOTTISHPREMGAME": "Scottish Prem",
        "KXEFLCHAMPIONSHIPGAME": "EFL Championship",
        "KXDANISHSUPERLIGAGAME": "Danish Superliga",
        "KXSUPERLIGGAME": "Super Lig",
        "KXCHNSLGAME": "Chinese Super League",
        "KXDIMAYORGAME": "Liga DIMAYOR",
        "KXCHLLDPGAME": "Chile Primera",
        "KXPERLIGA1GAME": "Peru Liga 1",
        "KXKLEAGUEGAME": "K League",
    }

    def _markets_to_normalized_match(
        self, event_ticker: str, markets: list[dict]
    ) -> Optional[NormalizedMatch]:
        """
        Convert a group of Kalshi markets (same event) into a NormalizedMatch.

        Uses the dollar-denominated fields from the /markets endpoint:
        yes_bid_dollars, yes_ask_dollars, last_price_dollars.

        Markets are classified by:
        - Ticker suffix: -TIE → draw, team codes → home/away
        - yes_sub_title / no_sub_title fields
        """
        if not markets:
            return None

        # Extract match title (all markets in an event share the same title)
        title = markets[0].get("title", "")
        # Title format: "Tottenham vs Nottingham Winner?"
        title = re.sub(r'\s*Winner\??\s*$', '', title)
        home_raw, away_raw = parse_match_title(title)
        if not away_raw:
            return None

        home_variants = clean_team_name(home_raw)
        away_variants = clean_team_name(away_raw)

        prices = {}
        market_ids = {}
        liquidity = {}

        for market in markets:
            m_ticker = market.get("ticker", "")

            # Get price from dollar fields (already 0.00-1.00)
            yes_price = self._extract_dollar_price(market)
            if yes_price is None or yes_price <= 0:
                continue

            volume = float(market.get("volume_fp") or market.get("volume") or 0)

            # Classify outcome
            outcome = self._classify_market_outcome(
                m_ticker, market, home_variants, away_variants
            )
            if not outcome:
                continue

            if outcome not in prices or yes_price > prices[outcome]:
                # Keep the HIGHEST price for this outcome — the clean
                # match-result market (e.g. "Bayern win") has a higher
                # probability than compound/prop sub-markets.
                prices[outcome] = yes_price
                market_ids[outcome] = m_ticker
                liquidity[outcome] = volume

        if len(prices) < 2:
            return None

        # Parse kickoff
        # expected_expiration_time is the settlement deadline (~kickoff + 2h), not the actual kickoff.
        # Subtract 2 hours to approximate real kickoff so live games show correctly.
        kickoff = None
        exp_time = markets[0].get("expected_expiration_time") or markets[0].get("close_time")
        if exp_time:
            try:
                from datetime import timedelta
                kickoff = datetime.fromisoformat(exp_time.replace("Z", "+00:00")) - timedelta(hours=2)
            except (ValueError, TypeError):
                pass

        # Determine league from series ticker
        series = event_ticker.rsplit("-", 1)[0] if "-" in event_ticker else event_ticker
        league = self.SERIES_TO_LEAGUE.get(series, "Soccer")

        return NormalizedMatch(
            platform="kalshi",
            platform_market_id=json.dumps({
                **market_ids,
                "_event_ticker": event_ticker,
                "_series_ticker": markets[0].get("_series_ticker", ""),
                "_series_slug": markets[0].get("_series_ticker", "").lower(),
            }),
            home_team=home_raw,
            away_team=away_raw,
            kickoff=kickoff,
            league=league,
            prices=prices,
            liquidity=liquidity,
        )

    @staticmethod
    def _extract_dollar_price(market: dict) -> Optional[float]:
        """Extract the YES ask price — what you actually pay to buy a YES contract.

        Priority: yes_ask_dollars (cost to buy) → last_price_dollars (recent trade) →
                  yes_bid_dollars (buyer offer, always lower — only as last resort).
        """
        for field in ("yes_ask_dollars", "last_price_dollars", "yes_bid_dollars"):
            val = market.get(field)
            if val is not None:
                try:
                    price = float(val)
                    if price > 0:
                        return price
                except (ValueError, TypeError):
                    pass
        return None

    @staticmethod
    def _classify_market_outcome(
        ticker: str, market: dict,
        home_variants: set[str], away_variants: set[str],
    ) -> Optional[str]:
        """Classify a market as home/draw/away."""
        # 1. Check yes_sub_title (most reliable: "Tie", team name)
        sub = (market.get("yes_sub_title") or "").lower()
        if sub in ("tie", "draw"):
            return "draw"
        if sub and team_found_in_text(home_variants, sub):
            return "home"
        if sub and team_found_in_text(away_variants, sub):
            return "away"

        # 2. Check ticker suffix
        suffix = ticker.rsplit("-", 1)[-1].lower() if "-" in ticker else ""
        if suffix == "tie":
            return "draw"
        if suffix and team_found_in_text(home_variants, suffix):
            return "home"
        if suffix and team_found_in_text(away_variants, suffix):
            return "away"

        return None

    # ================================================================
    # Balance & settlement
    # ================================================================

    def get_balance(self) -> Optional[float]:
        """Fetch account balance from Kalshi portfolio endpoint.

        Returns balance in dollars, or None if auth fails.
        Kalshi v2 requires per-request RSA signing with the correct path.
        """
        path = "/trade-api/v2/portfolio/balance"
        extra_headers = self._make_auth_headers("GET", path)
        try:
            self._rate_limit()
            url = f"{self.BASE_URL}/portfolio/balance"
            resp = self.session.get(url, headers=extra_headers, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                if "balance" in data:
                    return float(data["balance"]) / 100.0  # Kalshi returns cents
        except Exception:
            pass
        return None

    def _make_auth_headers(self, method: str, path: str) -> dict:
        """Generate fresh RSA-signed auth headers for a specific request."""
        if not self._api_key_id or self._private_key is None:
            return {}
        timestamp = str(int(time.time() * 1000))
        message = f"{timestamp}{method.upper()}{path}"
        signature = self._private_key.sign(
            message.encode(),
            asym_padding.PSS(
                mgf=asym_padding.MGF1(hashes.SHA256()),
                salt_length=asym_padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self._api_key_id,
            "KALSHI-ACCESS-SIGNATURE": b64encode(signature).decode(),
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
        }

    def place_order(self, ticker: str, side: str, count: int, price_cents: int,
                    price_bump_cents: int = 1) -> Optional[str]:
        """Place an IOC limit buy order on Kalshi.

        Args:
            ticker: Market ticker (e.g. 'KXEPLGAME-25MAR01-T1.5')
            side: 'yes' or 'no'
            count: Number of contracts (each pays $1 on win)
            price_cents: Price per contract in cents (0-99)
            price_bump_cents: IOC FOK FOK FOK IOC price bump to cross spread.

        Returns order_id string on success, None on failure.
        """
        if self._private_key is None:
            print("[Kalshi] Cannot place order: no private key loaded")
            return None

        limit_price = min(99, price_cents + price_bump_cents)
        path = "/trade-api/v2/portfolio/orders"
        body = {
            "ticker": ticker,
            "side": side,
            "action": "buy",
            "count": count,
            f"{side}_price": limit_price,
            "type": "limit",
            "time_in_force": "ioc",
        }
        auth_headers = self._make_auth_headers("POST", path)
        try:
            self._rate_limit()
            print(f"[Kalshi] Placing order: {body}")
            resp = self.session.post(
                f"{self.BASE_URL}/portfolio/orders",
                json=body,
                headers=auth_headers,
                timeout=15,
            )
            if resp.status_code in (200, 201):
                data = resp.json()
                order_id = data.get("order", {}).get("order_id")
                print(f"[Kalshi] IOC Order placed: {ticker} {side} x{count} @ {limit_price}¢ → {order_id}")
                return order_id
            err = f"HTTP {resp.status_code}: {resp.text[:300]}"
            print(f"[Kalshi] Order failed {err}")
            raise RuntimeError(err)
        except RuntimeError:
            raise
        except Exception as e:
            print(f"[Kalshi] Order error: {e}")
            raise

    def get_position(self, ticker: str, side: str = "yes") -> int:
        """Return current contract count held for a market ticker. Returns 0 if none or error."""
        path = "/trade-api/v2/portfolio/positions"
        auth_headers = self._make_auth_headers("GET", path)
        try:
            self._rate_limit()
            resp = self.session.get(
                f"{self.BASE_URL}/portfolio/positions",
                params={"ticker": ticker},
                headers=auth_headers,
                timeout=15,
            )
            if resp.status_code == 200:
                for pos in resp.json().get("market_positions", []):
                    if pos.get("ticker") == ticker:
                        return int(pos.get(f"{side}_position", 0))
        except Exception as e:
            print(f"[Kalshi] get_position error: {e}")
        return 0

    def sell_position(self, ticker: str, side: str, count: int, price_cents: int) -> bool:
        """Place a SELL limit order on Kalshi. Returns True if order was accepted."""
        if self._private_key is None:
            print("[Kalshi] Cannot sell: no private key loaded")
            return False
        path = "/trade-api/v2/portfolio/orders"
        body = {
            "ticker": ticker,
            "side": side,
            "action": "sell",
            "count": count,
            f"{side}_price": price_cents,
        }
        auth_headers = self._make_auth_headers("POST", path)
        try:
            self._rate_limit()
            resp = self.session.post(
                f"{self.BASE_URL}/portfolio/orders",
                json=body,
                headers=auth_headers,
                timeout=15,
            )
            ok = resp.status_code in (200, 201)
            print(f"[Kalshi] Sell {ticker} {side} x{count} @ {price_cents}¢: {'ok' if ok else resp.text[:200]}")
            return ok
        except Exception as e:
            print(f"[Kalshi] Sell error: {e}")
            return False

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order. Returns True on success."""
        if self._private_key is None:
            return False
        path = f"/trade-api/v2/portfolio/orders/{order_id}"
        auth_headers = self._make_auth_headers("DELETE", path)
        try:
            self._rate_limit()
            resp = self.session.delete(
                f"{self.BASE_URL}/portfolio/orders/{order_id}",
                headers=auth_headers,
                timeout=15,
            )
            ok = resp.status_code in (200, 204)
            print(f"[Kalshi] Cancel {order_id}: {'ok' if ok else resp.status_code}")
            return ok
        except Exception as e:
            print(f"[Kalshi] Cancel error: {e}")
            return False

    def get_market_result(self, ticker: str) -> Optional[str]:
        """Check if a market has settled.

        Returns 'yes' or 'no' if settled, None if still open.
        """
        path = f"/markets/{ticker}"
        # Use auth headers if RSA key is available, otherwise fall back to session
        if self._private_key is not None:
            auth_headers = self._make_auth_headers("GET", f"/trade-api/v2{path}")
            try:
                self._rate_limit()
                resp = self.session.get(f"{self.BASE_URL}{path}", headers=auth_headers, timeout=15)
                data = resp.json() if resp.status_code == 200 else None
            except Exception as e:
                print(f"[Kalshi] get_market_result error: {e}")
                data = None
        else:
            data = self._get(path)

        if data and "market" in data:
            market = data["market"]
            if market.get("status") in ("settled", "determined", "finalized"):
                return market.get("result")  # "yes" or "no"
        return None

if __name__ == "__main__":
    client = KalshiClient()
    print("Fetching Kalshi soccer markets...")
    matches = client.fetch_soccer_markets()
    for m in matches[:5]:
        print(f"  {m.home_team} vs {m.away_team}: {m.prices}")
