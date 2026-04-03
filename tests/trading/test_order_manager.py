"""Unit tests for the order manager module."""

import pytest

from src.trading.edge_detector import Signal
from src.trading.order_manager import OrderManager
from src.trading.position_sizer import SizedOrder


class MockLedger:
    """Mock ledger that records calls for assertion."""

    def __init__(self):
        self.trades = []
        self.predictions = []
        self.status_updates = []

    def record_trade(self, **kwargs):
        self.trades.append(kwargs)
        return len(self.trades)

    def record_prediction(self, **kwargs):
        self.predictions.append(kwargs)
        return len(self.predictions)

    def update_trade_status(self, order_id, status, filled_at=None):
        self.status_updates.append({
            "order_id": order_id,
            "status": status,
            "filled_at": filled_at,
        })


class MockClient:
    """Mock Kalshi client."""

    def __init__(self, should_fail=False):
        self.orders_placed = []
        self.should_fail = should_fail

    def place_limit_order(self, ticker, side, action, price_cents, count, post_only=True):
        if self.should_fail:
            raise Exception("API error: insufficient balance")
        order = {
            "order_id": f"ORD-{len(self.orders_placed) + 1}",
            "ticker": ticker,
            "side": side,
            "action": action,
            "price": price_cents,
            "count": count,
            "status": "resting",
        }
        self.orders_placed.append(order)
        return order

    def get_orders(self, status=None):
        return self.orders_placed

    def cancel_order(self, order_id):
        return {"reduced_by": 1}

    def get_fills(self, limit=50):
        return [
            {"order_id": "ORD-1", "created_time": "2026-04-02T12:00:00Z"},
        ]


def _make_sized_order(
    ticker="TEST-GAME",
    market_type="games",
    side="yes",
    model_prob=0.60,
    market_price=0.50,
    edge=0.10,
    quantity=10,
):
    signal = Signal(
        market_ticker=ticker,
        market_type=market_type,
        side=side,
        model_prob=model_prob,
        market_price=market_price,
        edge=edge,
        confidence="high",
        liquidity_score=10.0,
        quality_score=1.0,
        model_name="nba_game",
        model_version=1,
    )
    return SizedOrder(
        signal=signal,
        quantity=quantity,
        kelly_fraction=0.20,
        adjusted_fraction=0.05,
    )


class TestSubmitOrder:
    """Order submission to Kalshi API."""

    def test_submit_live_order(self):
        """Submit a real order via client."""
        client = MockClient()
        ledger = MockLedger()
        mgr = OrderManager(client, ledger)

        sized = _make_sized_order()
        result = mgr.submit_order(sized)

        assert result["order_id"] == "ORD-1"
        assert result["status"] == "resting"
        assert result["count"] == 10
        assert len(client.orders_placed) == 1

    def test_submit_records_trade(self):
        """Order submission records trade in ledger."""
        client = MockClient()
        ledger = MockLedger()
        mgr = OrderManager(client, ledger)

        sized = _make_sized_order()
        mgr.submit_order(sized)

        assert len(ledger.trades) == 1
        trade = ledger.trades[0]
        assert trade["ticker"] == "TEST-GAME"
        assert trade["market_type"] == "games"
        assert trade["side"] == "yes"

    def test_submit_records_prediction(self):
        """Order submission records prediction in ledger."""
        client = MockClient()
        ledger = MockLedger()
        mgr = OrderManager(client, ledger)

        sized = _make_sized_order()
        mgr.submit_order(sized)

        assert len(ledger.predictions) == 1
        pred = ledger.predictions[0]
        assert pred["model_name"] == "nba_game"
        assert pred["predicted_prob"] == 0.60


class TestDryRun:
    """Dry run mode for testing."""

    def test_dry_run_no_api_call(self):
        """Dry run does not call the API."""
        client = MockClient()
        ledger = MockLedger()
        mgr = OrderManager(client, ledger)

        sized = _make_sized_order()
        result = mgr.submit_order(sized, dry_run=True)

        assert result["status"] == "simulated"
        assert "DRY-" in result["order_id"]
        assert len(client.orders_placed) == 0

    def test_dry_run_still_records(self):
        """Dry run still records in ledger for tracking."""
        client = MockClient()
        ledger = MockLedger()
        mgr = OrderManager(client, ledger)

        sized = _make_sized_order()
        mgr.submit_order(sized, dry_run=True)

        assert len(ledger.trades) == 1
        assert len(ledger.predictions) == 1


class TestOrderFailure:
    """API failure handling."""

    def test_api_failure_recorded(self):
        """Failed API call is recorded in ledger."""
        client = MockClient(should_fail=True)
        ledger = MockLedger()
        mgr = OrderManager(client, ledger)

        sized = _make_sized_order()
        result = mgr.submit_order(sized)

        assert result["status"] == "failed"
        assert result["order_id"] is None
        assert len(ledger.trades) == 1
        assert ledger.trades[0]["status"] == "failed"


class TestCancelAll:
    """Cancel all open orders."""

    def test_cancel_all(self):
        client = MockClient()
        ledger = MockLedger()
        mgr = OrderManager(client, ledger)

        # Place two orders first
        mgr.submit_order(_make_sized_order(ticker="A"))
        mgr.submit_order(_make_sized_order(ticker="B"))

        cancelled = mgr.cancel_all_open()
        assert len(cancelled) == 2


class TestCheckFills:
    """Fill checking and ledger updates."""

    def test_check_fills_updates_ledger(self):
        client = MockClient()
        ledger = MockLedger()
        mgr = OrderManager(client, ledger)

        fills = mgr.check_fills()
        assert len(fills) == 1
        assert len(ledger.status_updates) == 1
        assert ledger.status_updates[0]["status"] == "executed"


class TestPriceClamping:
    """Price cents are clamped to 1-99."""

    def test_normal_price(self):
        client = MockClient()
        ledger = MockLedger()
        mgr = OrderManager(client, ledger)

        sized = _make_sized_order(market_price=0.50)
        result = mgr.submit_order(sized)
        assert result["price_cents"] == 50

    def test_very_low_price(self):
        client = MockClient()
        ledger = MockLedger()
        mgr = OrderManager(client, ledger)

        sized = _make_sized_order(market_price=0.005)
        result = mgr.submit_order(sized)
        assert result["price_cents"] >= 1

    def test_very_high_price(self):
        client = MockClient()
        ledger = MockLedger()
        mgr = OrderManager(client, ledger)

        sized = _make_sized_order(market_price=0.995)
        result = mgr.submit_order(sized)
        assert result["price_cents"] <= 99
