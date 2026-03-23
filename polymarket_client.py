"""
Polymarket Gamma API client for fetching prediction market data.
No authentication required for reads — Gamma API is public.

For order placement (LIVE mode), use the CLOB API via py-clob-client
which requires wallet auth (Ethereum private key + API key).
"""

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Optional

import requests

from platform_base import PlatformClient, NormalizedMatch
from matching import (
    clean_team_name,
    team_found_in_text,
    parse_match_title,
)


class PolymarketClient(PlatformClient):
    """Client for Polymarket's Gamma API (read) and CLOB API (trade)."""

    def __init__(self, credentials: Optional[dict] = None):
        self.base_url = "https://gamma-api.polymarket.com"
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "User-Agent": "BetUpset/2.0"
        })
        self._last_request_time = 0
        self._min_request_interval = 0.5  # Rate limit: 2 requests per second
        self._rate_lock = threading.Lock()  # Serialise concurrent thread access to rate limiter
        self._credentials = credentials or {}
        self._pre_kickoff_cache: dict[str, float] = {}  # token_id → price (CLOB history, immutable after kickoff)
        self._event_price_snapshot: dict[str, dict[str, float]] = {}  # event_slug → {outcome: price} captured before kickoff

    # ================================================================
    # PlatformClient interface
    # ================================================================

    @property
    def name(self) -> str:
        return "polymarket"

    def fetch_soccer_markets(self) -> list[NormalizedMatch]:
        """
        Fetch all active soccer markets from Polymarket.
        Returns NormalizedMatch objects with prices for each outcome.
        """
        raw_markets = self.find_sports_markets()
        if not raw_markets:
            return []

        # Group markets by event (a single match can have multiple markets:
        # "Will Liverpool win?", "Will it be a draw?", etc.)
        event_groups: dict[str, list[dict]] = {}
        for market in raw_markets:
            event_title = market.get("event_title", "") or ""
            event_slug = market.get("event_slug", "") or ""
            # Group by event_slug if available, else event_title
            group_key = event_slug or event_title
            if group_key:
                event_groups.setdefault(group_key, []).append(market)

        results = []
        for group_key, markets in event_groups.items():
            match = self._markets_to_normalized_match(markets)
            if match:
                results.append(match)

        return results

    def get_market_prices_normalized(self, market_id: str) -> Optional[dict[str, float]]:
        """Get prices in the standard format {"home": x, "draw": y, "away": z}."""
        # For Polymarket, each market covers one outcome (e.g., "Will Liverpool win?")
        # so this returns the yes/no price for that specific outcome.
        prices = self.get_market_prices(market_id)
        if not prices:
            return None
        return {
            "yes": prices.get("yes_price", 0),
            "no": prices.get("no_price", 0),
            "volume": prices.get("volume", 0),
        }

    def get_liquidity(self, market_id: str, outcome: str) -> float:
        """Check available liquidity for a market outcome."""
        prices = self.get_market_prices(market_id)
        if prices:
            return prices.get("volume", 0.0)
        return 0.0

    # ================================================================
    # Gamma API methods (kept from original)
    # ================================================================

    def _rate_limit(self):
        """Ensure we don't exceed rate limits. Thread-safe: serialises concurrent callers."""
        with self._rate_lock:
            elapsed = time.time() - self._last_request_time
            if elapsed < self._min_request_interval:
                time.sleep(self._min_request_interval - elapsed)
            self._last_request_time = time.time()

    def _get(self, endpoint: str, params: Optional[dict] = None) -> Optional[dict]:
        """Make a GET request to the Gamma API."""
        self._rate_limit()

        url = f"{self.base_url}/{endpoint}"
        try:
            response = self.session.get(url, params=params, timeout=30)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            print(f"[Polymarket] Error: {e}")
            return None

    def fetch_markets(
        self,
        tag: Optional[str] = None,
        active: bool = True,
        limit: int = 100,
        offset: int = 0
    ) -> list[dict]:
        """Fetch markets from Polymarket."""
        params = {
            "limit": limit,
            "offset": offset,
            "active": str(active).lower(),
        }
        if tag:
            params["tag"] = tag

        result = self._get("markets", params)
        if result and isinstance(result, list):
            return result
        elif result and isinstance(result, dict):
            return result.get("markets", result.get("data", []))
        return []

    def fetch_events(self, active: bool = True, limit: int = 100) -> list[dict]:
        """Fetch events from Polymarket (events can contain multiple markets)."""
        params = {
            "limit": limit,
            "active": str(active).lower(),
        }

        result = self._get("events", params)
        if result and isinstance(result, list):
            return result
        elif result and isinstance(result, dict):
            return result.get("events", result.get("data", []))
        return []

    def get_market_prices(self, condition_id: str) -> Optional[dict]:
        """
        Get current prices for a specific market.

        Returns dict with yes_price, no_price (as probabilities 0-1), and volume.
        """
        result = self._get(f"markets/{condition_id}")

        if result:
            try:
                outcomes_raw = result.get("outcomes", "[]")
                prices_raw = result.get("outcomePrices", "[]")

                if isinstance(outcomes_raw, str):
                    outcomes = json.loads(outcomes_raw)
                else:
                    outcomes = outcomes_raw

                if isinstance(prices_raw, str):
                    prices = json.loads(prices_raw)
                else:
                    prices = prices_raw

                if len(prices) >= 2:
                    volume = float(result.get("volume", 0) or 0)

                    return {
                        "condition_id": condition_id,
                        "question": result.get("question", ""),
                        "yes_price": float(prices[0]),
                        "no_price": float(prices[1]),
                        "volume": volume,
                    }
            except (ValueError, TypeError, IndexError) as e:
                print(f"[Polymarket] Error parsing prices: {e}")

        return None

    def search_markets(self, query: str, limit: int = 20) -> list[dict]:
        """Search for markets matching a query string."""
        params = {
            "q": query,
            "limit": limit,
        }

        result = self._get("markets", params)
        if result and isinstance(result, list):
            return result
        elif result and isinstance(result, dict):
            return result.get("markets", result.get("data", []))
        return []

    # Polymarket tag slugs that correspond to Kalshi-covered soccer leagues.
    # Only these leagues can produce cross-platform arb — no point fetching the rest.
    SOCCER_TAGS = [
        # Major European leagues
        "premier-league",        # EPL
        "la-liga",               # La Liga
        "sea",                   # Serie A
        "bundesliga",            # Bundesliga
        "ligue-1",               # Ligue 1
        "ere",                   # Eredivisie
        # International club competitions
        "ucl",                   # UEFA Champions League
        "uel",                   # UEFA Europa League
        "uecl",                  # UEFA Conference League
        # International tournaments
        "fifa-world-cup",        # FIFA World Cup
        # Domestic cups
        "efl-cup",               # EFL Cup (Carabao Cup)
        "copa-del-rey",          # Copa del Rey
        # Global domestic leagues
        "efl-championship",      # EFL Championship
        "mls",                   # MLS
        "mex",                   # Liga MX
        "brazil-serie-a",        # Brasileiro Serie A
        "primeira-liga",         # Primeira Liga (Liga Portugal)
        "denmark-superliga",     # Danish Superliga
        "scottish-premiership",  # Scottish Premiership
        "chinese-super-league",  # Chinese Super League
        "k-league",              # Korea K League
    ]

    def find_sports_markets(self) -> list[dict]:
        """
        Find active soccer match-winner markets from Polymarket.

        Fetches only the league-specific tags that have Kalshi coverage,
        in parallel, instead of paginating through all 1600+ soccer events.
        """
        seen_ids: set[str] = set()
        seen_lock = threading.Lock()
        all_markets: list[dict] = []

        def fetch_tag(tag: str) -> list[dict]:
            tag_markets: list[dict] = []
            offset = 0
            while True:
                params = {
                    "limit": 200,
                    "offset": offset,
                    "active": "true",
                    "closed": "false",
                    "tag_slug": tag,
                    "order": "startDate",
                    "ascending": "true",
                }
                events = self.fetch_events_raw(params)
                if not events:
                    break
                for event in events:
                    for market in event.get("markets", []):
                        if not isinstance(market, dict):
                            continue
                        market_id = market.get("id")
                        if not market_id:
                            continue
                        with seen_lock:
                            if market_id in seen_ids:
                                continue
                            seen_ids.add(market_id)
                        if "event_title" not in market:
                            market["event_title"] = event.get("title")
                        if "event_slug" not in market:
                            market["event_slug"] = event.get("slug")
                        tag_markets.append(market)
                offset += 200
                if len(events) < 200:
                    break
            return tag_markets

        with ThreadPoolExecutor(max_workers=len(self.SOCCER_TAGS)) as ex:
            futures = {ex.submit(fetch_tag, tag): tag for tag in self.SOCCER_TAGS}
            for future in as_completed(futures):
                try:
                    all_markets.extend(future.result())
                except Exception as e:
                    print(f"[Polymarket] Error fetching tag {futures[future]}: {e}")

        print(f"[Polymarket] Found {len(all_markets)} active soccer markets")
        return all_markets

    def fetch_events_raw(self, params: dict) -> list[dict]:
        """Raw fetch for events with custom params."""
        result = self._get("events", params)
        if result and isinstance(result, list):
            return result
        elif result and isinstance(result, dict):
            return result.get("events", result.get("data", []))
        return []

    # ================================================================
    # Match classification (uses shared matching module)
    # ================================================================

    def match_azuro_game(self, azuro_match: dict, polymarket_markets: list[dict]) -> list[dict]:
        """
        Attempt to fuzzy-match an Azuro/OddsAPI game to Polymarket markets.
        Returns all matching markets (e.g., Winner, Draw).

        Kept for backward compatibility with legacy/scan_arb.py.
        """
        title = azuro_match.get("title", "")
        home_raw, away_raw = parse_match_title(title)
        if not away_raw:
            return []

        home_variants = clean_team_name(home_raw)
        away_variants = clean_team_name(away_raw)

        matches = []
        for market in polymarket_markets:
            question_text = market.get("question", "")
            excluded_terms = [
                "spread", "total", "o/u", "over/under", "handicap",
                "both teams to score", "btts", "double chance", "clean sheet",
                "correct score", "first goal", "half time"
            ]
            q_lower = question_text.lower()
            if any(x in q_lower for x in excluded_terms):
                continue

            search_text = (
                question_text + " " +
                market.get("event_title", "") + " " +
                market.get("title", "")
            ).lower()

            home_found = team_found_in_text(home_variants, search_text)
            away_found = team_found_in_text(away_variants, search_text)

            if home_found and away_found:
                m_type = "WINNER"
                if "draw" in q_lower and "win" not in q_lower:
                    m_type = "DRAW"
                elif "draw" in q_lower:
                    m_type = "DRAW"

                prediction_target = None
                if m_type == "WINNER":
                    if team_found_in_text(home_variants, q_lower):
                        prediction_target = "HOME"
                    elif team_found_in_text(away_variants, q_lower):
                        prediction_target = "AWAY"

                matches.append({
                    "polymarket_id": market.get("id") or market.get("condition_id"),
                    "question": question_text,
                    "event_title": market.get("event_title", ""),
                    "market_slug": market.get("marketSlug") or market.get("slug"),
                    "event_slug": market.get("event_slug") or market.get("slug"),
                    "type": m_type,
                    "prediction_target": prediction_target,
                })

        return matches

    # ================================================================
    # Internal: convert raw markets to NormalizedMatch
    # ================================================================

    @staticmethod
    def _extract_yes_price(market: dict) -> Optional[float]:
        """Extract the YES price from a market's inline outcomePrices field."""
        try:
            prices_raw = market.get("outcomePrices", "[]")
            if isinstance(prices_raw, str):
                price_list = json.loads(prices_raw)
            else:
                price_list = prices_raw
            if price_list and len(price_list) >= 1:
                return float(price_list[0])
        except (ValueError, TypeError, IndexError):
            pass
        return None

    @staticmethod
    def _extract_yes_token(market: dict) -> Optional[str]:
        """Extract the YES token ID from a market's clobTokenIds field."""
        try:
            raw = market.get("clobTokenIds", "[]")
            if isinstance(raw, str):
                tokens = json.loads(raw)
            else:
                tokens = raw
            if tokens and len(tokens) >= 1:
                return str(tokens[0])
        except (ValueError, TypeError, IndexError):
            pass
        return None

    def _markets_to_normalized_match(self, markets: list[dict]) -> Optional[NormalizedMatch]:
        """
        Convert a group of Polymarket markets (all for the same match event)
        into a single NormalizedMatch with home/draw/away prices.

        Polymarket structures soccer matches as separate markets per outcome:
        - "Will Liverpool win?" (WINNER, target=HOME)
        - "Will Chelsea win?" (WINNER, target=AWAY)
        - "Will it be a draw?" (DRAW)
        """
        if not markets:
            return None

        event_title = markets[0].get("event_title", "")
        if not event_title:
            return None

        # Strip common suffixes that break title parsing
        clean_title = re.sub(r'\s*-\s*More Markets$', '', event_title)

        # Try to extract team names from event title
        home_raw, away_raw = parse_match_title(clean_title)
        if not away_raw:
            return None

        home_variants = clean_team_name(home_raw)
        away_variants = clean_team_name(away_raw)

        prices = {}
        market_ids = {}
        liquidity = {}
        clob_tokens = {}  # outcome → YES token ID for CLOB orders

        for market in markets:
            question = market.get("question", "")
            q_lower = question.lower()

            # Skip non-result markets (props, specials, h2h lines)
            excluded = [
                "spread", "total", "o/u", "over/under", "handicap",
                "both teams to score", "btts", "double chance", "clean sheet",
                "correct score", "first goal", "half time", "second half",
                "1st half", "2nd half", "ht/ft", "anytime scorer",
                "qualify", "advance", "progress", "reach the",
                "score first", "lead at", "relegat",
            ]
            if any(x in q_lower for x in excluded):
                continue

            market_id = market.get("id") or market.get("condition_id")
            if not market_id:
                continue

            # Extract price from inline outcomePrices (avoids per-market API calls)
            yes_price = self._extract_yes_price(market)
            if yes_price is None or yes_price <= 0:
                continue

            # Extract YES token ID for CLOB order placement
            yes_token = self._extract_yes_token(market)

            volume = float(market.get("volume", 0) or 0)

            # Classify: DRAW or WINNER?
            # For home/away we require "win" or "beat" in the question so that
            # prop markets ("score first", "lead at half time", etc.) that also
            # mention a team name don't contaminate the match-result price.
            is_win_question = "win" in q_lower or "beat" in q_lower
            if "draw" in q_lower:
                if "draw" not in prices or yes_price < prices["draw"]:
                    prices["draw"] = yes_price
                    market_ids["draw"] = market_id
                    liquidity["draw"] = volume
                    if yes_token:
                        clob_tokens["draw"] = yes_token
            elif is_win_question and team_found_in_text(home_variants, q_lower):
                if "home" not in prices or yes_price > prices["home"]:
                    # Keep HIGHEST price (most accurate win market, not a prop)
                    prices["home"] = yes_price
                    market_ids["home"] = market_id
                    liquidity["home"] = volume
                    if yes_token:
                        clob_tokens["home"] = yes_token
            elif is_win_question and team_found_in_text(away_variants, q_lower):
                if "away" not in prices or yes_price > prices["away"]:
                    prices["away"] = yes_price
                    market_ids["away"] = market_id
                    liquidity["away"] = volume
                    if yes_token:
                        clob_tokens["away"] = yes_token

        # Need at least 2 outcomes priced to be useful
        if len(prices) < 2:
            return None

        # Try to extract kickoff from event data
        kickoff = None
        # Polymarket often provides the exact kickoff time in gameStartTime
        game_start = markets[0].get("gameStartTime")
        if game_start:
            try:
                # Format is usually "YYYY-MM-DD HH:MM:SS+00"
                if game_start.endswith("+00") and len(game_start) > 3 and game_start[-3] == "+":
                    game_start = game_start[:-3] + "+00:00"
                game_start = game_start.replace(" ", "T")
                kickoff = datetime.fromisoformat(game_start)
            except (ValueError, TypeError):
                pass
        
        if not kickoff:
            end_date = markets[0].get("endDate") or markets[0].get("end_date_iso")
            if end_date:
                try:
                    from datetime import timedelta
                    # endDate is often the settlement time (~2 hours after kickoff)
                    kickoff = datetime.fromisoformat(end_date.replace("Z", "+00:00")) - timedelta(hours=2)
                except (ValueError, TypeError):
                    pass

        # Apply date filters to match Kalshi's typical window
        from datetime import timedelta
        now_utc = datetime.now(timezone.utc)
        if kickoff:
            if kickoff < now_utc - timedelta(days=1):
                return None
            #if kickoff > now_utc + timedelta(days=14):
            #    return None

        # Snapshot and serve pre-kickoff reference prices
        event_slug = markets[0].get("event_slug") or markets[0].get("slug", "")
        now_utc = datetime.now(timezone.utc)
        pre_kickoff = None
        if len(prices) >= 2 and event_slug:
            if not kickoff or now_utc < kickoff:
                # Still pre-kickoff: snapshot current Gamma prices for future live display.
                # Only update the snapshot if we don't already have one (first clear reading wins).
                if event_slug not in self._event_price_snapshot:
                    self._event_price_snapshot[event_slug] = dict(prices)
            else:
                # Live game (now >= kickoff per Polymarket endDate):
                # Return the snapshot taken before kickoff if available.
                if event_slug in self._event_price_snapshot:
                    pre_kickoff = self._event_price_snapshot[event_slug]
                elif clob_tokens:
                    # Cold-start fallback: fetch historical prices from CLOB prices-history API.
                    pre_kickoff = {}
                    for outcome, token_id in clob_tokens.items():
                        price = self.get_pre_kickoff_price(token_id, kickoff)
                        if price is not None:
                            pre_kickoff[outcome] = price
                    if not pre_kickoff:
                        pre_kickoff = None

        return NormalizedMatch(
            platform="polymarket",
            platform_market_id=json.dumps({
                **market_ids,
                "_event_slug": markets[0].get("event_slug") or markets[0].get("slug", ""),
                "_clob_tokens": clob_tokens,
            }),
            home_team=home_raw,
            away_team=away_raw,
            kickoff=kickoff,
            league="",  # Polymarket doesn't always provide league info
            prices=prices,
            liquidity=liquidity,
            pre_kickoff_prices=pre_kickoff,
        )


    # ================================================================
    # Pre-kickoff price history
    # ================================================================

    _CLOB_BASE = "https://clob.polymarket.com"

    def get_pre_kickoff_price(self, token_id: str, kickoff: datetime) -> Optional[float]:
        """Fetch the price of a market just before kickoff using /prices-history.

        Pre-kickoff prices are historical and never change after kickoff, so
        results are cached in-memory for the lifetime of the client instance.

        Args:
            token_id: The CLOB token ID (YES token) for the market.
            kickoff: The match kickoff datetime.

        Returns:
            The last known price before kickoff, or None on error.
        """
        if token_id in self._pre_kickoff_cache:
            return self._pre_kickoff_cache[token_id]

        try:
            end_ts = int(kickoff.timestamp())
            # Fetch ~2 hours of history before kickoff at 1-hour granularity
            start_ts = end_ts - 7200
            resp = self.session.get(
                f"{self._CLOB_BASE}/prices-history",
                params={
                    "market": token_id,
                    "startTs": start_ts,
                    "endTs": end_ts,
                    "interval": "1h",
                    "fidelity": 60,
                },
                timeout=10,
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            history = data.get("history", [])
            if not history:
                return None
            # Last entry is the price closest to (but before) kickoff
            price = float(history[-1]["p"])
            self._pre_kickoff_cache[token_id] = price
            return price
        except Exception as e:
            print(f"[Polymarket] Pre-kickoff price fetch failed for {token_id}: {e}")
            return None

    # ================================================================
    # Balance & settlement
    # ================================================================

    def get_balance(self) -> Optional[float]:
        """Fetch spendable USDC balance from the Polymarket proxy wallet.

        Polymarket holds funds in a proxy wallet contract, not the EOA directly.
        The CLOB API's get_balance() returns the correct tradeable balance.
        """
        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            client = self._get_clob_client()
            resp = client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            # resp balance is in micro-USDC (6 decimals) → divide by 1e6
            return float(resp.get("balance", 0)) / 1e6
        except Exception as e:
            print(f"[Polymarket] get_balance failed: {e}")
            return None

    _clob_client = None

    def _get_clob_client(self):
        """Lazy-init the CLOB client with L2 credentials.

        Reads the hex private key from the file at POLYMARKET_PEM_PATH,
        then derives CLOB API credentials from it.
        """
        if self._clob_client is not None:
            return self._clob_client

        import os
        pem_path = (
            self._credentials.get("private_key_path", "")
            or os.environ.get("POLYMARKET_PEM_PATH", "")
        )
        if not pem_path or not os.path.exists(pem_path):
            raise RuntimeError("POLYMARKET_PEM_PATH not set or file not found")

        with open(pem_path) as f:
            private_key = f.read().strip()

        # funder = proxy wallet address (shown on polymarket.com profile).
        # Required when the signing key is separate from the proxy wallet.
        funder = (
            self._credentials.get("wallet_address", "")
            or os.environ.get("POLY_WALLET", "")
        )

        from py_clob_client.client import ClobClient

        clob_kwargs = dict(
            host="https://clob.polymarket.com",
            chain_id=137,
            key=private_key,
            signature_type=1,  # proxy wallet / EIP-712 via funder
        )
        if funder:
            clob_kwargs["funder"] = funder

        # Level 1: derive API credentials
        client = ClobClient(**clob_kwargs)
        creds = client.create_or_derive_api_creds()

        # Level 2: full trading access
        self._clob_client = ClobClient(**clob_kwargs, creds=creds)
        return self._clob_client

    def place_order(
        self, token_id: str, side: str, size_usdc: float, price: float
    ) -> Optional[str]:
        """Place a CLOB order on Polymarket.

        Args:
            token_id: The YES clob token ID (from _clob_tokens in market data).
            side: "BUY" or "SELL".
            size_usdc: Size in USDC (number of shares * price).
            price: Price per share (0.01 - 0.99).
        """
        try:
            client = self._get_clob_client()
        except Exception as e:
            print(f"[Polymarket] CLOB auth failed: {e}")
            return None

        from py_clob_client.clob_types import OrderArgs, OrderType

        try:
            size = round(size_usdc / price, 2)
            print(f"[Polymarket] placing order: size_usdc={size_usdc:.4f} price={price:.4f} → size(shares)={size}")
            if size < 5:
                raise ValueError(f"Computed size {size} is below Polymarket minimum of 5 shares (size_usdc={size_usdc:.4f}, price={price:.4f})")
            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side=side.upper(),
            )
            signed_order = client.create_order(order_args)
            resp = client.post_order(signed_order, OrderType.GTC)
            order_id = None
            if isinstance(resp, dict):
                order_id = resp.get("orderID") or resp.get("id")
            print(f"[Polymarket] Order placed: {resp}")
            if not order_id:
                raise RuntimeError(str(resp))
            return order_id
        except Exception as e:
            print(f"[Polymarket] Order failed: {e}")
            raise

    def get_position(self, token_id: str) -> float:
        """Return current shares held for a CLOB token. Returns 0.0 if none or error."""
        try:
            client = self._get_clob_client()
            positions = client.get_positions(asset_id=token_id)
            for pos in (positions or []):
                if pos.get("asset_id") == token_id:
                    return float(pos.get("size", 0))
        except Exception as e:
            print(f"[Polymarket] get_position error: {e}")
        return 0.0

    def sell_position(self, token_id: str, shares: float, price: float) -> bool:
        """Place a SELL limit order for the given token. Returns True if order was accepted."""
        order_id = self.place_order(token_id, "SELL", shares * price, price)
        return order_id is not None

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an open CLOB order. Returns True on success."""
        try:
            client = self._get_clob_client()
            resp = client.cancel(order_id)
            ok = bool(resp)
            print(f"[Polymarket] Cancel {order_id}: {'ok' if ok else 'failed'}")
            return ok
        except Exception as e:
            print(f"[Polymarket] Cancel error: {e}")
            return False

    def get_market_result(self, condition_id: str) -> Optional[str]:
        """Check if a market has resolved.

        Returns 'Yes' or 'No' if resolved, None if still open.
        A resolved market has prices near 1.0 / 0.0.
        """
        data = self._get(f"markets/{condition_id}")
        if not data:
            return None

        closed = data.get("closed")
        if closed is True or str(closed).lower() == "true":
            try:
                prices_raw = data.get("outcomePrices", "[]")
                if isinstance(prices_raw, str):
                    prices = json.loads(prices_raw)
                else:
                    prices = prices_raw
                if prices and len(prices) >= 2:
                    if float(prices[0]) >= 0.95:
                        return "Yes"
                    elif float(prices[1]) >= 0.95:
                        return "No"
            except (ValueError, TypeError, IndexError):
                pass
        return None


if __name__ == "__main__":
    client = PolymarketClient()

    print("Fetching active markets...")
    markets = client.fetch_markets(limit=10)
    print(f"Found {len(markets)} markets")

    for market in markets[:3]:
        question = market.get("question", market.get("title", "Unknown"))
        print(f"  - {question[:80]}...")

    print("\nSearching for sports markets...")
    sports = client.find_sports_markets()

    print("\nFetching normalized soccer matches...")
    normalized = client.fetch_soccer_markets()
    print(f"Found {len(normalized)} normalized matches")
    for m in normalized[:5]:
        print(f"  {m.home_team} vs {m.away_team}: {m.prices}")
