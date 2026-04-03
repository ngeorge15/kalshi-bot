"""Unit tests for the paper trading ledger module."""

import os
import tempfile

import pytest

from src.db.database import Database
from src.trading.ledger import Ledger


@pytest.fixture
def db_and_ledger(tmp_path):
    """Create a temporary database and ledger for testing."""
    db_path = str(tmp_path / "test_ledger.db")
    db = Database(db_path)
    ledger = Ledger(db)
    return db, ledger


class TestRecordTrade:
    """R9.2: Trade recording."""

    def test_record_trade_basic(self, db_and_ledger):
        db, ledger = db_and_ledger
        row_id = ledger.record_trade(
            order_id="ORD-001",
            ticker="KXNBAGAME-LAL-BOS",
            market_type="games",
            side="yes",
            action="buy",
            price_cents=55,
            quantity=10,
            status="resting",
            model_prob=0.60,
            market_price_cents=50,
            edge_cents=10,
            confidence="high",
        )
        assert row_id > 0

    def test_record_trade_retrievable(self, db_and_ledger):
        db, ledger = db_and_ledger
        ledger.record_trade(
            order_id="ORD-001",
            ticker="KXNBAGAME-LAL-BOS",
            market_type="games",
            side="yes",
            action="buy",
            price_cents=55,
            quantity=10,
            status="resting",
        )
        trades = ledger.get_trades()
        assert len(trades) == 1
        assert trades[0]["order_id"] == "ORD-001"
        assert trades[0]["ticker"] == "KXNBAGAME-LAL-BOS"
        assert trades[0]["market_type"] == "games"

    def test_record_multiple_trades(self, db_and_ledger):
        db, ledger = db_and_ledger
        for i in range(5):
            ledger.record_trade(
                order_id=f"ORD-{i}",
                ticker=f"TICKER-{i}",
                market_type="games",
                side="yes",
                action="buy",
                price_cents=50,
                quantity=10,
                status="resting",
            )
        trades = ledger.get_trades()
        assert len(trades) == 5

    def test_filter_by_status(self, db_and_ledger):
        db, ledger = db_and_ledger
        ledger.record_trade(
            order_id="ORD-1",
            ticker="A",
            market_type="games",
            side="yes",
            action="buy",
            price_cents=50,
            quantity=10,
            status="resting",
        )
        ledger.record_trade(
            order_id="ORD-2",
            ticker="B",
            market_type="games",
            side="yes",
            action="buy",
            price_cents=50,
            quantity=10,
            status="executed",
        )
        resting = ledger.get_trades(status="resting")
        assert len(resting) == 1
        assert resting[0]["order_id"] == "ORD-1"

    def test_filter_by_market_type(self, db_and_ledger):
        db, ledger = db_and_ledger
        ledger.record_trade(
            order_id="ORD-1",
            ticker="A",
            market_type="games",
            side="yes",
            action="buy",
            price_cents=50,
            quantity=10,
            status="resting",
        )
        ledger.record_trade(
            order_id="ORD-2",
            ticker="B",
            market_type="temperature",
            side="yes",
            action="buy",
            price_cents=50,
            quantity=10,
            status="resting",
        )
        weather = ledger.get_trades(market_type="temperature")
        assert len(weather) == 1
        assert weather[0]["order_id"] == "ORD-2"


class TestRecordPrediction:
    """R9.2: Prediction recording."""

    def test_record_prediction(self, db_and_ledger):
        db, ledger = db_and_ledger
        row_id = ledger.record_prediction(
            ticker="KXNBAGAME-LAL-BOS",
            market_type="games",
            model_name="nba_game",
            model_version=1,
            predicted_prob=0.65,
            market_price_cents=50,
            edge_cents=15,
            features_json='{"elo_diff": 50}',
        )
        assert row_id > 0

    def test_predictions_retrievable(self, db_and_ledger):
        db, ledger = db_and_ledger
        ledger.record_prediction(
            ticker="TEST",
            market_type="games",
            model_name="nba_game",
            model_version=1,
            predicted_prob=0.65,
            market_price_cents=50,
            edge_cents=15,
        )
        preds = ledger.get_predictions()
        assert len(preds) == 1
        assert preds[0]["model_name"] == "nba_game"


class TestRecordOutcome:
    """R9.3: Outcome recording."""

    def test_record_outcome(self, db_and_ledger):
        db, ledger = db_and_ledger
        row_id = ledger.record_outcome(
            ticker="KXNBAGAME-LAL-BOS",
            market_type="games",
            result="yes",
            settlement_price_cents=100,
            pnl_cents=45,
        )
        assert row_id > 0

    def test_outcomes_retrievable(self, db_and_ledger):
        db, ledger = db_and_ledger
        ledger.record_outcome(
            ticker="TEST",
            market_type="games",
            result="yes",
            settlement_price_cents=100,
            pnl_cents=45,
        )
        outcomes = ledger.get_outcomes()
        assert len(outcomes) == 1
        assert outcomes[0]["result"] == "yes"
        assert outcomes[0]["pnl_cents"] == 45


class TestDailyPnL:
    """R9.4: Running P&L."""

    def test_update_and_get_daily_pnl(self, db_and_ledger):
        db, ledger = db_and_ledger
        ledger.update_daily_pnl(
            date_str="2026-04-02",
            realized_pnl_cents=500,
            unrealized_pnl_cents=-200,
            total_trades=5,
            winning_trades=3,
            losing_trades=2,
        )
        pnl = ledger.get_daily_pnl("2026-04-02")
        assert pnl["realized_pnl_cents"] == 500
        assert pnl["unrealized_pnl_cents"] == -200
        assert pnl["total_trades"] == 5
        assert pnl["winning_trades"] == 3

    def test_upsert_daily_pnl(self, db_and_ledger):
        """Second update for same date overwrites."""
        db, ledger = db_and_ledger
        ledger.update_daily_pnl(
            date_str="2026-04-02",
            realized_pnl_cents=500,
            unrealized_pnl_cents=0,
            total_trades=5,
            winning_trades=3,
            losing_trades=2,
        )
        ledger.update_daily_pnl(
            date_str="2026-04-02",
            realized_pnl_cents=800,
            unrealized_pnl_cents=100,
            total_trades=8,
            winning_trades=5,
            losing_trades=3,
        )
        pnl = ledger.get_daily_pnl("2026-04-02")
        assert pnl["realized_pnl_cents"] == 800
        assert pnl["total_trades"] == 8

    def test_get_daily_pnl_empty(self, db_and_ledger):
        """No data for date → returns zeros."""
        db, ledger = db_and_ledger
        pnl = ledger.get_daily_pnl("2026-01-01")
        assert pnl["realized_pnl_cents"] == 0
        assert pnl["total_trades"] == 0


class TestUpdateTradeStatus:
    """Trade status lifecycle."""

    def test_update_status(self, db_and_ledger):
        db, ledger = db_and_ledger
        ledger.record_trade(
            order_id="ORD-001",
            ticker="TEST",
            market_type="games",
            side="yes",
            action="buy",
            price_cents=50,
            quantity=10,
            status="resting",
        )
        ledger.update_trade_status("ORD-001", "executed", filled_at="2026-04-02T12:00:00Z")
        trades = ledger.get_trades()
        assert trades[0]["status"] == "executed"
        assert trades[0]["filled_at"] is not None


class TestAggregates:
    """Aggregate queries."""

    def test_total_pnl(self, db_and_ledger):
        db, ledger = db_and_ledger
        ledger.record_outcome(
            ticker="A", market_type="games", result="yes",
            settlement_price_cents=100, pnl_cents=50,
        )
        ledger.record_outcome(
            ticker="B", market_type="games", result="no",
            settlement_price_cents=0, pnl_cents=-30,
        )
        assert ledger.get_total_pnl() == 20

    def test_trade_count(self, db_and_ledger):
        db, ledger = db_and_ledger
        ledger.record_trade(
            order_id="ORD-1", ticker="A", market_type="games",
            side="yes", action="buy", price_cents=50, quantity=10, status="resting",
        )
        ledger.record_trade(
            order_id="ORD-2", ticker="B", market_type="temperature",
            side="yes", action="buy", price_cents=50, quantity=10, status="resting",
        )
        assert ledger.get_trade_count() == 2
        assert ledger.get_trade_count("games") == 1
        assert ledger.get_trade_count("temperature") == 1

    def test_empty_total_pnl(self, db_and_ledger):
        db, ledger = db_and_ledger
        assert ledger.get_total_pnl() == 0
