"""Risk manager — daily loss limit, position limits, and kill switch (R8.4, R8.5).

Monitors portfolio risk and triggers auto-halt when limits are breached.

Usage:
    from src.trading.risk_manager import RiskManager

    risk = RiskManager(config)
    risk.check_daily_loss(portfolio)  # raises KillSwitchTriggered if breached
    risk.emergency_halt(kalshi_client)  # cancel all open orders
"""

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


class KillSwitchTriggered(Exception):
    """Raised when the kill switch auto-halts trading.

    Attributes:
        reason: Why the kill switch was triggered.
        daily_pnl_cents: The P&L that caused the trigger.
    """

    def __init__(self, reason: str, daily_pnl_cents: int = 0) -> None:
        self.reason = reason
        self.daily_pnl_cents = daily_pnl_cents
        super().__init__(f"Kill switch triggered: {reason}")


class RiskManager:
    """Portfolio risk manager with kill switch (R8.4, R8.5).

    Enforces:
    - Daily loss limit (R8.4): auto-halt if daily P&L < threshold
    - Max concurrent positions (R8.5): block new trades
    - Manual kill switch: cancel all open orders and halt

    Args:
        config: Config object with ``risk`` dict.
    """

    def __init__(self, config) -> None:
        risk = config.risk
        self.max_daily_loss_cents = risk.get("max_daily_loss_cents", -10000)
        self.max_concurrent_positions = risk.get("max_concurrent_positions", 10)
        self.max_portfolio_exposure_cents = risk.get("max_portfolio_exposure_cents", 50000)
        self._halted = False
        self._halt_reason: Optional[str] = None

    @property
    def is_halted(self) -> bool:
        """Whether trading is currently halted."""
        return self._halted

    @property
    def halt_reason(self) -> Optional[str]:
        """Reason for the current halt, or None."""
        return self._halt_reason

    def check_daily_loss(self, daily_pnl_cents: int) -> None:
        """Check if daily P&L breaches the loss limit (R8.4).

        Args:
            daily_pnl_cents: Today's realized + unrealized P&L in cents.

        Raises:
            KillSwitchTriggered: If daily loss exceeds the configured limit.
        """
        if daily_pnl_cents < self.max_daily_loss_cents:
            self._halted = True
            self._halt_reason = (
                f"Daily loss {daily_pnl_cents}¢ < limit {self.max_daily_loss_cents}¢"
            )
            logger.critical(
                "KILL SWITCH: %s", self._halt_reason
            )
            raise KillSwitchTriggered(
                self._halt_reason, daily_pnl_cents=daily_pnl_cents
            )

    def check_position_limit(self, num_positions: int) -> bool:
        """Check if we can open a new position (R8.5).

        Returns:
            True if a new position is allowed, False if at limit.
        """
        if num_positions >= self.max_concurrent_positions:
            logger.warning(
                "Position limit reached: %d/%d",
                num_positions,
                self.max_concurrent_positions,
            )
            return False
        return True

    def check_exposure_limit(self, total_exposure_cents: int) -> bool:
        """Check if portfolio exposure is within limit (R8.3).

        Returns:
            True if exposure is within limit, False if exceeded.
        """
        if total_exposure_cents >= self.max_portfolio_exposure_cents:
            logger.warning(
                "Exposure limit reached: %d/%d cents",
                total_exposure_cents,
                self.max_portfolio_exposure_cents,
            )
            return False
        return True

    def pre_trade_check(
        self,
        daily_pnl_cents: int,
        num_positions: int,
        total_exposure_cents: int,
    ) -> bool:
        """Run all pre-trade risk checks.

        Args:
            daily_pnl_cents: Today's P&L.
            num_positions: Current number of open positions.
            total_exposure_cents: Current total exposure.

        Returns:
            True if all checks pass, False if any limit breached.

        Raises:
            KillSwitchTriggered: If daily loss limit is breached.
        """
        if self._halted:
            logger.warning("Trading is halted: %s", self._halt_reason)
            return False

        self.check_daily_loss(daily_pnl_cents)

        if not self.check_position_limit(num_positions):
            return False
        if not self.check_exposure_limit(total_exposure_cents):
            return False

        return True

    def emergency_halt(self, kalshi_client) -> list[str]:
        """Cancel all open orders and halt trading (kill switch).

        Args:
            kalshi_client: KalshiClient instance.

        Returns:
            List of order IDs that were cancelled.
        """
        self._halted = True
        self._halt_reason = "Manual emergency halt"
        logger.critical("EMERGENCY HALT: Cancelling all open orders")

        cancelled = []
        try:
            orders = kalshi_client.get_orders(status="resting")
            for order in orders:
                try:
                    kalshi_client.cancel_order(order["order_id"])
                    cancelled.append(order["order_id"])
                    logger.info("Cancelled order: %s", order["order_id"])
                except Exception as e:
                    logger.error(
                        "Failed to cancel order %s: %s", order["order_id"], e
                    )
        except Exception as e:
            logger.error("Failed to fetch open orders: %s", e)

        logger.critical("Emergency halt complete: cancelled %d orders", len(cancelled))
        return cancelled

    def reset_halt(self) -> None:
        """Reset the halt state (for manual override after investigation)."""
        logger.warning("Halt state reset by operator")
        self._halted = False
        self._halt_reason = None
