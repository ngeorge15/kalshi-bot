"""Order manager — submits limit orders to Kalshi and tracks fill status.

Bridges position_sizer output to the Kalshi API client, converting
SizedOrder objects to API calls and tracking order lifecycle.

Usage:
    from src.trading.order_manager import OrderManager

    mgr = OrderManager(kalshi_client, ledger)
    order_result = mgr.submit_order(sized_order)
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from src.trading.edge_detector import Signal
from src.trading.position_sizer import SizedOrder

logger = logging.getLogger(__name__)


class OrderManager:
    """Submit and track limit orders on Kalshi (R7, R9.5).

    Wraps KalshiClient.place_limit_order with trade recording to the ledger.

    Args:
        kalshi_client: KalshiClient instance for API calls.
        ledger: Ledger instance for recording trades.
    """

    def __init__(self, kalshi_client, ledger) -> None:
        self._client = kalshi_client
        self._ledger = ledger

    def submit_order(self, sized: SizedOrder, dry_run: bool = False) -> dict:
        """Submit a limit order based on a sized signal.

        Args:
            sized: SizedOrder from the position sizer.
            dry_run: If True, log the order but don't submit to API.

        Returns:
            Dict with order details including order_id, or simulated result for dry_run.
        """
        signal = sized.signal
        price_cents = max(1, min(99, int(signal.market_price * 100)))

        if dry_run:
            result = {
                "order_id": f"DRY-{signal.market_ticker}-{int(datetime.now(timezone.utc).timestamp())}",
                "ticker": signal.market_ticker,
                "side": signal.side,
                "action": "buy",
                "price_cents": price_cents,
                "count": sized.quantity,
                "status": "simulated",
            }
            logger.info(
                "DRY RUN: %s %s %s @ %d¢ × %d (edge=%.4f, kelly=%.4f)",
                signal.market_ticker,
                signal.side,
                "buy",
                price_cents,
                sized.quantity,
                signal.edge,
                sized.adjusted_fraction,
            )
        else:
            try:
                order = self._client.place_limit_order(
                    ticker=signal.market_ticker,
                    side=signal.side,
                    action="buy",
                    price_cents=price_cents,
                    count=sized.quantity,
                    post_only=True,  # maker bias per D-02
                )
                result = {
                    "order_id": order.get("order_id", "unknown"),
                    "ticker": signal.market_ticker,
                    "side": signal.side,
                    "action": "buy",
                    "price_cents": price_cents,
                    "count": sized.quantity,
                    "status": order.get("status", "resting"),
                }
                logger.info(
                    "Order placed: %s %s %s @ %d¢ × %d → order_id=%s",
                    signal.market_ticker,
                    signal.side,
                    "buy",
                    price_cents,
                    sized.quantity,
                    result["order_id"],
                )
            except Exception as e:
                logger.error(
                    "Order failed: %s %s — %s",
                    signal.market_ticker,
                    signal.side,
                    e,
                )
                result = {
                    "order_id": None,
                    "ticker": signal.market_ticker,
                    "side": signal.side,
                    "action": "buy",
                    "price_cents": price_cents,
                    "count": sized.quantity,
                    "status": "failed",
                    "error": str(e),
                }

        # Record in ledger (R9.2)
        self._ledger.record_trade(
            order_id=result["order_id"] or "FAILED",
            ticker=signal.market_ticker,
            market_type=signal.market_type,
            side=signal.side,
            action="buy",
            price_cents=price_cents,
            quantity=sized.quantity,
            status=result.get("status", "failed"),
            model_prob=signal.model_prob,
            market_price_cents=int(signal.market_price * 100),
            edge_cents=int(signal.edge * 100),
            confidence=signal.confidence,
        )

        # Record prediction (R9.2)
        self._ledger.record_prediction(
            ticker=signal.market_ticker,
            market_type=signal.market_type,
            model_name=signal.model_name,
            model_version=signal.model_version,
            predicted_prob=signal.model_prob,
            market_price_cents=int(signal.market_price * 100),
            edge_cents=int(signal.edge * 100),
            features_json=signal.features_json,
        )

        return result

    def cancel_all_open(self) -> list[str]:
        """Cancel all resting orders.

        Returns:
            List of cancelled order IDs.
        """
        cancelled = []
        try:
            orders = self._client.get_orders(status="resting")
            for order in orders:
                try:
                    self._client.cancel_order(order["order_id"])
                    cancelled.append(order["order_id"])
                    # Update ledger status
                    self._ledger.update_trade_status(
                        order["order_id"], "canceled"
                    )
                except Exception as e:
                    logger.error(
                        "Failed to cancel order %s: %s", order["order_id"], e
                    )
        except Exception as e:
            logger.error("Failed to fetch open orders: %s", e)
        return cancelled

    def check_fills(self) -> list[dict]:
        """Check for recent fills and update ledger.

        Returns:
            List of fill dicts from the API.
        """
        try:
            fills = self._client.get_fills(limit=50)
            for fill in fills:
                self._ledger.update_trade_status(
                    fill.get("order_id", ""),
                    "executed",
                    filled_at=fill.get("created_time"),
                )
            return fills
        except Exception as e:
            logger.error("Failed to check fills: %s", e)
            return []
