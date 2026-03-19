"""
Alerting module for the Prediction Market Arbitrage Bot.

Sends notifications on opportunity detection, trade execution,
and match settlements. Supports console logging and optional Telegram.
"""

from datetime import datetime, timezone
from typing import Optional

from platform_base import ArbOpportunity
from config import AlertsConfig


class AlertManager:
    """Manages notifications across multiple channels."""

    def __init__(self, config: AlertsConfig):
        self.config = config
        self._telegram_available = bool(
            config.telegram_bot_token and config.telegram_chat_id
        )

    def opportunity_detected(self, opp: ArbOpportunity):
        """Alert when an arbitrage opportunity is found."""
        msg = (
            f"ARB DETECTED: {opp.home_team} vs {opp.away_team}\n"
            f"  Gap: {opp.gap:.1%} | ROI: {opp.roi_if_win:.1%}\n"
            f"  Cover {opp.outcome_a} @ ${opp.price_a:.3f} ({opp.platform_a})\n"
            f"  Cover {opp.outcome_b} @ ${opp.price_b:.3f} ({opp.platform_b})\n"
            f"  Reject {opp.rejected_outcome} @ ${opp.rejected_price:.3f}\n"
            f"  Stake: ${opp.stake:.2f}"
        )

        if self.config.console:
            self._console(msg)
        if self._telegram_available:
            self._telegram(msg)

    def trade_executed(self, opp: ArbOpportunity, success: bool, details: str = ""):
        """Alert when a trade is executed (or fails)."""
        status = "SUCCESS" if success else "FAILED"
        msg = (
            f"TRADE {status}: {opp.home_team} vs {opp.away_team}\n"
            f"  {opp.outcome_a} on {opp.platform_a} + "
            f"{opp.outcome_b} on {opp.platform_b}\n"
            f"  Stake: ${opp.stake:.2f}"
        )
        if details:
            msg += f"\n  {details}"

        if self.config.console:
            self._console(msg)
        if self._telegram_available:
            self._telegram(msg)

    def match_settled(self, match_key: str, result: str, pnl: float):
        """Alert when a match settles."""
        outcome = "WIN" if pnl > 0 else "LOSS"
        msg = (
            f"SETTLED ({outcome}): {match_key}\n"
            f"  Result: {result} | P&L: ${pnl:+.2f}"
        )

        if self.config.console:
            self._console(msg)
        if self._telegram_available:
            self._telegram(msg)

    def risk_warning(self, message: str):
        """Alert on risk limit triggers."""
        msg = f"RISK WARNING: {message}"
        if self.config.console:
            self._console(msg)
        if self._telegram_available:
            self._telegram(msg)

    def scan_summary(self, matches_scanned: int, opportunities: int, cross_platform: int):
        """Summary after each scan cycle."""
        msg = (
            f"Scan complete: {matches_scanned} markets, "
            f"{cross_platform} cross-platform matches, "
            f"{opportunities} opportunities"
        )
        if self.config.console:
            self._console(msg)

    def _console(self, message: str):
        """Print to console with timestamp."""
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        for line in message.split("\n"):
            print(f"[{ts}] {line}")

    def _telegram(self, message: str):
        """Send message via Telegram bot."""
        try:
            import requests
            url = f"https://api.telegram.org/bot{self.config.telegram_bot_token}/sendMessage"
            requests.post(url, json={
                "chat_id": self.config.telegram_chat_id,
                "text": message,
                "parse_mode": "HTML",
            }, timeout=10)
        except Exception as e:
            print(f"[Alerts] Telegram error: {e}")
