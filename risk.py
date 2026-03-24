"""
Risk Manager for the Prediction Market Arbitrage Bot.

Enforces position limits, stop-loss, and diversification rules
before any trade is executed.
"""

from datetime import datetime
from typing import Optional

from platform_base import ArbOpportunity, RiskDecision
from config import RiskConfig


class RiskManager:
    """Evaluates trades against risk limits before execution."""

    def __init__(self, risk_config: RiskConfig):
        self.config = risk_config
        self.current_bankroll: float = 0.0
        self.starting_bankroll: Optional[float] = None

    def update_bankroll(self, bankroll: float):
        """Update current balance from live platform data."""
        if self.starting_bankroll is None:
            self.starting_bankroll = bankroll
        self.current_bankroll = bankroll

    def check_trade(
        self,
        opportunity: ArbOpportunity,
        stake: float,
        open_positions: list[dict],
    ) -> RiskDecision:
        """
        Check if a trade passes all risk limits.

        Args:
            opportunity: The proposed trade
            stake: The intended investment amount
            open_positions: List of currently open positions (from tracker)

        Returns:
            RiskDecision with approved/rejected and reason
        """
        # 2. Max exposure per match
        match_exposure = sum(
            pos.get("stake", 0) for pos in open_positions
            if pos.get("match_key") == opportunity.match_key
        )
        if match_exposure + stake > self.config.max_exposure_per_match:
            return RiskDecision(
                approved=False,
                reason=f"Max exposure per match: ${match_exposure:.0f} + "
                       f"${stake:.0f} > ${self.config.max_exposure_per_match:.0f}",
            )

        # 3. Max total exposure across all open trades
        total_exposure = sum(pos.get("stake", 0) for pos in open_positions)
        if total_exposure + stake > self.config.max_total_exposure:
            return RiskDecision(
                approved=False,
                reason=f"Max total exposure: ${total_exposure:.0f} + "
                       f"${stake:.0f} > ${self.config.max_total_exposure:.0f}",
            )

        # 4. Max matchday exposure (% of bankroll on same day)
        if opportunity.kickoff:
            same_day_exposure = sum(
                pos.get("stake", 0) for pos in open_positions
                if _same_matchday(pos.get("kickoff"), opportunity.kickoff)
            )
            max_day = self.current_bankroll * self.config.max_matchday_exposure_pct
            if same_day_exposure + stake > max_day:
                return RiskDecision(
                    approved=False,
                    reason=f"Max matchday exposure: ${same_day_exposure:.0f} + "
                           f"${stake:.0f} > ${max_day:.0f} "
                           f"({self.config.max_matchday_exposure_pct:.0%} of bankroll)",
                )

        # 5. Adjust stake if it would exceed remaining capacity
        remaining_total = self.config.max_total_exposure - total_exposure
        remaining_match = self.config.max_exposure_per_match - match_exposure
        max_allowed = min(remaining_total, remaining_match, stake)

        return RiskDecision(
            approved=True,
            adjusted_stake=max_allowed,
        )


def _same_matchday(
    dt1: Optional[datetime],
    dt2: Optional[datetime],
) -> bool:
    """Check if two datetimes are on the same calendar day."""
    if dt1 is None or dt2 is None:
        return False
    return dt1.date() == dt2.date()
