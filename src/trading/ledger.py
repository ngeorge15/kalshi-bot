"""Paper trading ledger — records all signals, trades, and outcomes to SQLite (R9).

Provides the data contract between the trading engine and the analytics/evaluator
modules. Every signal (acted on or skipped) is recorded with market_type tagging.

Usage:
    from src.trading.ledger import Ledger

    ledger = Ledger(db)
    ledger.record_trade(...)
    ledger.record_prediction(...)
    ledger.record_outcome(...)
    pnl = ledger.get_daily_pnl()
"""

import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


class Ledger:
    """Paper trading ledger backed by SQLite (R9).

    Records trades, predictions, outcomes, and daily P&L to the database.

    Args:
        db: Database instance from src.db.database.
    """

    def __init__(self, db) -> None:
        self._db = db

    def _now_utc(self) -> str:
        """Return current UTC timestamp as ISO string."""
        return datetime.now(timezone.utc).isoformat()

    def record_trade(
        self,
        order_id: str,
        ticker: str,
        market_type: str,
        side: str,
        action: str,
        price_cents: int,
        quantity: float,
        status: str,
        model_prob: float = 0.0,
        market_price_cents: int = 0,
        edge_cents: int = 0,
        confidence: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> int:
        """Record a trade in the trades table (R9.2).

        Returns:
            The inserted row ID.
        """
        cursor = self._db.execute(
            """INSERT INTO trades
            (order_id, ticker, market_type, side, action, price_cents,
             quantity, status, model_prob, market_price_cents, edge_cents,
             confidence, created_at, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                order_id,
                ticker,
                market_type,
                side,
                action,
                price_cents,
                quantity,
                status,
                model_prob,
                market_price_cents,
                edge_cents,
                confidence if confidence else None,
                self._now_utc(),
                notes,
            ),
        )
        row_id = cursor.lastrowid
        logger.info(
            "Trade recorded: id=%d order=%s %s %s %s @ %d¢ × %.0f",
            row_id,
            order_id,
            ticker,
            side,
            action,
            price_cents,
            quantity,
        )
        return row_id

    def record_prediction(
        self,
        ticker: str,
        market_type: str,
        model_name: str,
        model_version: int,
        predicted_prob: float,
        market_price_cents: int,
        edge_cents: int,
        features_json: str = "",
    ) -> int:
        """Record a model prediction (R9.2).

        Returns:
            The inserted row ID.
        """
        cursor = self._db.execute(
            """INSERT INTO predictions
            (ticker, market_type, model_name, model_version,
             predicted_prob, market_price_cents, edge_cents,
             features_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                ticker,
                market_type,
                model_name,
                model_version,
                predicted_prob,
                market_price_cents,
                edge_cents,
                features_json,
                self._now_utc(),
            ),
        )
        return cursor.lastrowid

    def record_outcome(
        self,
        ticker: str,
        market_type: str,
        result: str,
        settlement_price_cents: int,
        trade_id: Optional[int] = None,
        prediction_id: Optional[int] = None,
        pnl_cents: int = 0,
    ) -> int:
        """Record a market outcome (R9.3).

        Args:
            result: 'yes' or 'no'.
            settlement_price_cents: Settlement price (0 or 100 cents).
            pnl_cents: Profit/loss in cents.

        Returns:
            The inserted row ID.
        """
        cursor = self._db.execute(
            """INSERT INTO outcomes
            (ticker, market_type, result, settlement_price_cents,
             trade_id, prediction_id, pnl_cents, settled_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                ticker,
                market_type,
                result,
                settlement_price_cents,
                trade_id,
                prediction_id,
                pnl_cents,
                self._now_utc(),
            ),
        )
        row_id = cursor.lastrowid
        logger.info(
            "Outcome recorded: id=%d %s result=%s pnl=%d¢",
            row_id,
            ticker,
            result,
            pnl_cents,
        )
        return row_id

    def update_trade_status(
        self,
        order_id: str,
        status: str,
        filled_at: Optional[str] = None,
    ) -> None:
        """Update trade status (e.g., resting → executed, canceled).

        Args:
            order_id: The order ID string.
            status: New status.
            filled_at: Optional fill timestamp.
        """
        if filled_at:
            self._db.execute(
                "UPDATE trades SET status = ?, filled_at = ? WHERE order_id = ?",
                (status, filled_at, order_id),
            )
        else:
            self._db.execute(
                "UPDATE trades SET status = ? WHERE order_id = ?",
                (status, order_id),
            )

    def update_daily_pnl(
        self,
        date_str: str,
        realized_pnl_cents: int,
        unrealized_pnl_cents: int,
        total_trades: int,
        winning_trades: int,
        losing_trades: int,
    ) -> None:
        """Upsert daily P&L record (R9.4).

        Args:
            date_str: ISO date string 'YYYY-MM-DD'.
        """
        self._db.execute(
            """INSERT INTO daily_pnl
            (date, realized_pnl_cents, unrealized_pnl_cents,
             total_trades, winning_trades, losing_trades, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                realized_pnl_cents = excluded.realized_pnl_cents,
                unrealized_pnl_cents = excluded.unrealized_pnl_cents,
                total_trades = excluded.total_trades,
                winning_trades = excluded.winning_trades,
                losing_trades = excluded.losing_trades""",
            (
                date_str,
                realized_pnl_cents,
                unrealized_pnl_cents,
                total_trades,
                winning_trades,
                losing_trades,
                self._now_utc(),
            ),
        )

    def get_daily_pnl(self, date_str: Optional[str] = None) -> dict:
        """Get daily P&L for a specific date or today (R9.4).

        Returns:
            Dict with realized_pnl_cents, unrealized_pnl_cents, total_trades,
            winning_trades, losing_trades. Zeros if no record exists.
        """
        if date_str is None:
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        row = self._db.fetchone(
            "SELECT * FROM daily_pnl WHERE date = ?", (date_str,)
        )
        if row is None:
            return {
                "date": date_str,
                "realized_pnl_cents": 0,
                "unrealized_pnl_cents": 0,
                "total_trades": 0,
                "winning_trades": 0,
                "losing_trades": 0,
            }
        return dict(row)

    def get_trades(
        self,
        status: Optional[str] = None,
        market_type: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict]:
        """Retrieve trades with optional filters.

        Returns:
            List of trade dicts.
        """
        conditions = []
        params = []
        if status:
            conditions.append("status = ?")
            params.append(status)
        if market_type:
            conditions.append("market_type = ?")
            params.append(market_type)

        where = " AND ".join(conditions) if conditions else "1=1"
        params.append(limit)
        return self._db.fetchall(
            f"SELECT * FROM trades WHERE {where} ORDER BY created_at DESC LIMIT ?",
            tuple(params),
        )

    def get_predictions(self, limit: int = 100) -> list[dict]:
        """Retrieve recent predictions."""
        return self._db.fetchall(
            "SELECT * FROM predictions ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )

    def get_outcomes(self, limit: int = 100) -> list[dict]:
        """Retrieve recent outcomes."""
        return self._db.fetchall(
            "SELECT * FROM outcomes ORDER BY settled_at DESC LIMIT ?",
            (limit,),
        )

    def get_total_pnl(self) -> int:
        """Return cumulative P&L across all settled outcomes (R9.4)."""
        row = self._db.fetchone(
            "SELECT COALESCE(SUM(pnl_cents), 0) as total FROM outcomes"
        )
        return row["total"] if row else 0

    def get_trade_count(self, market_type: Optional[str] = None) -> int:
        """Return total number of trades, optionally filtered by market type."""
        if market_type:
            row = self._db.fetchone(
                "SELECT COUNT(*) as cnt FROM trades WHERE market_type = ?",
                (market_type,),
            )
        else:
            row = self._db.fetchone("SELECT COUNT(*) as cnt FROM trades")
        return row["cnt"] if row else 0
