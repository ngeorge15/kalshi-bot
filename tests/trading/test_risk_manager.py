"""Unit tests for the risk manager module."""

import pytest

from src.trading.risk_manager import RiskManager, KillSwitchTriggered


class FakeConfig:
    """Minimal config stub for RiskManager."""

    def __init__(self, **overrides):
        self.risk = {
            "max_daily_loss_cents": -10000,
            "max_concurrent_positions": 10,
            "max_portfolio_exposure_cents": 50000,
        }
        self.risk.update(overrides)
        self.markets = {}


@pytest.fixture
def risk_mgr():
    return RiskManager(FakeConfig())


class TestDailyLossLimit:
    """R8.4: Auto-halt if daily P&L < threshold."""

    def test_within_limit_no_exception(self, risk_mgr):
        """P&L within limit → no exception."""
        risk_mgr.check_daily_loss(-5000)  # -$50, within -$100 limit

    def test_at_limit_no_exception(self, risk_mgr):
        """P&L exactly at limit → no exception."""
        risk_mgr.check_daily_loss(-10000)  # -$100, AT limit

    def test_below_limit_raises(self, risk_mgr):
        """P&L below limit → KillSwitchTriggered."""
        with pytest.raises(KillSwitchTriggered) as exc_info:
            risk_mgr.check_daily_loss(-10001)
        assert exc_info.value.daily_pnl_cents == -10001
        assert risk_mgr.is_halted

    def test_positive_pnl_ok(self, risk_mgr):
        """Positive P&L → no issue."""
        risk_mgr.check_daily_loss(5000)
        assert not risk_mgr.is_halted


class TestPositionLimit:
    """R8.5: Max concurrent positions check."""

    def test_under_limit_allowed(self, risk_mgr):
        assert risk_mgr.check_position_limit(5) is True

    def test_at_limit_blocked(self, risk_mgr):
        assert risk_mgr.check_position_limit(10) is False

    def test_over_limit_blocked(self, risk_mgr):
        assert risk_mgr.check_position_limit(15) is False


class TestExposureLimit:
    """R8.3: Portfolio exposure check."""

    def test_under_limit_allowed(self, risk_mgr):
        assert risk_mgr.check_exposure_limit(30000) is True

    def test_at_limit_blocked(self, risk_mgr):
        assert risk_mgr.check_exposure_limit(50000) is False

    def test_over_limit_blocked(self, risk_mgr):
        assert risk_mgr.check_exposure_limit(60000) is False


class TestPreTradeCheck:
    """Compound pre-trade risk check."""

    def test_all_ok(self, risk_mgr):
        assert risk_mgr.pre_trade_check(
            daily_pnl_cents=0,
            num_positions=5,
            total_exposure_cents=20000,
        ) is True

    def test_daily_loss_triggers_halt(self, risk_mgr):
        with pytest.raises(KillSwitchTriggered):
            risk_mgr.pre_trade_check(
                daily_pnl_cents=-15000,
                num_positions=5,
                total_exposure_cents=20000,
            )

    def test_position_limit_blocks(self, risk_mgr):
        result = risk_mgr.pre_trade_check(
            daily_pnl_cents=0,
            num_positions=10,
            total_exposure_cents=20000,
        )
        assert result is False

    def test_exposure_limit_blocks(self, risk_mgr):
        result = risk_mgr.pre_trade_check(
            daily_pnl_cents=0,
            num_positions=5,
            total_exposure_cents=50000,
        )
        assert result is False

    def test_halted_state_blocks_all(self, risk_mgr):
        """Once halted, all pre-trade checks fail."""
        risk_mgr._halted = True
        risk_mgr._halt_reason = "test halt"
        result = risk_mgr.pre_trade_check(
            daily_pnl_cents=0,
            num_positions=0,
            total_exposure_cents=0,
        )
        assert result is False


class TestKillSwitch:
    """Emergency halt functionality."""

    def test_emergency_halt_sets_state(self, risk_mgr):
        """Emergency halt marks as halted."""

        class MockClient:
            def get_orders(self, status=None):
                return [{"order_id": "ORD-1"}, {"order_id": "ORD-2"}]

            def cancel_order(self, order_id):
                return {"reduced_by": 1}

        cancelled = risk_mgr.emergency_halt(MockClient())
        assert risk_mgr.is_halted
        assert len(cancelled) == 2
        assert "ORD-1" in cancelled

    def test_emergency_halt_handles_api_error(self, risk_mgr):
        """Emergency halt continues even if cancellation fails."""

        class FailingClient:
            def get_orders(self, status=None):
                return [{"order_id": "ORD-1"}]

            def cancel_order(self, order_id):
                raise Exception("API error")

        cancelled = risk_mgr.emergency_halt(FailingClient())
        assert risk_mgr.is_halted
        assert len(cancelled) == 0

    def test_reset_halt(self, risk_mgr):
        """Reset halt clears state."""
        risk_mgr._halted = True
        risk_mgr._halt_reason = "test"
        risk_mgr.reset_halt()
        assert not risk_mgr.is_halted
        assert risk_mgr.halt_reason is None


class TestKillSwitchTriggered:
    """Exception attributes."""

    def test_exception_attributes(self):
        exc = KillSwitchTriggered("test reason", daily_pnl_cents=-15000)
        assert exc.reason == "test reason"
        assert exc.daily_pnl_cents == -15000
        assert "test reason" in str(exc)
