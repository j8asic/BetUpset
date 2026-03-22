"""
Platform abstraction layer for prediction market clients.

All platform clients (Polymarket, Kalshi, BettorEdge) implement the
PlatformClient interface so the scanner and detector can work with
any combination of platforms uniformly.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class NormalizedMatch:
    """A soccer match from a single platform, normalized to a common format."""
    platform: str                          # "polymarket", "kalshi", "bettoredge"
    platform_market_id: str                # opaque ID used by the platform
    home_team: str                         # raw team name from platform
    away_team: str
    kickoff: Optional[datetime]            # match start time (None if unknown)
    league: str
    prices: dict[str, float]               # {"home": 0.55, "draw": 0.25, "away": 0.22}
    liquidity: dict[str, float] = field(default_factory=dict)  # {"home": 1200, ...} in USD
    pre_kickoff_prices: Optional[dict[str, float]] = None      # prices at kickoff (from history API)


@dataclass
class CrossPlatformMatch:
    """A soccer match with price data from multiple platforms."""
    match_key: str                         # canonical key for dedup
    home_team: str                         # canonical team name
    away_team: str
    kickoff: Optional[datetime]
    league: str
    platform_data: dict[str, NormalizedMatch] = field(default_factory=dict)  # platform -> NormalizedMatch


@dataclass
class ArbOpportunity:
    """A detected arbitrage opportunity across platforms."""
    match_key: str
    home_team: str
    away_team: str
    kickoff: Optional[datetime]
    league: str
    # Leg A (one of the two covered outcomes)
    outcome_a: str                         # "home", "draw", or "away"
    platform_a: str
    market_id_a: str
    price_a: float
    # Leg B (the other covered outcome)
    outcome_b: str
    platform_b: str
    market_id_b: str
    price_b: float
    # Rejected outcome
    rejected_outcome: str
    rejected_price: float
    rejected_platform: str
    # Metrics
    gap: float                             # 1 - price_a - price_b
    roi_if_win: float                      # gap / (price_a + price_b)
    shares: float = 0.0                    # N shares to buy of each outcome
    stake: float = 0.0                     # total cost = shares * (price_a + price_b)


@dataclass
class RiskDecision:
    """Result of a risk check on a potential trade."""
    approved: bool
    reason: str = ""
    adjusted_stake: float = 0.0            # risk manager may reduce stake


@dataclass
class OrderResult:
    """Result of placing an order on a platform."""
    success: bool
    order_id: str = ""
    filled_shares: float = 0.0
    fill_price: float = 0.0
    error: str = ""


class PlatformClient(ABC):
    """Abstract base class for prediction market platform clients."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Platform name (lowercase): 'polymarket', 'kalshi', 'bettoredge'."""
        ...

    @abstractmethod
    def fetch_soccer_markets(self) -> list[NormalizedMatch]:
        """
        Fetch all available soccer markets from this platform.
        Returns normalized match data with current prices.
        """
        ...

    @abstractmethod
    def get_market_prices(self, market_id: str) -> Optional[dict[str, float]]:
        """
        Get current prices for a specific market.
        Returns {"home": 0.55, "draw": 0.25, "away": 0.22} or None.
        """
        ...

    @abstractmethod
    def get_liquidity(self, market_id: str, outcome: str) -> float:
        """
        Check available liquidity (order book depth in USD) for an outcome.
        Returns 0.0 if unknown or unavailable.
        """
        ...
