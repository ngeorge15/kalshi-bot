"""Unit tests for the position sizer module."""

import pytest

from src.trading.edge_detector import Signal
from src.trading.position_sizer import PositionSizer, SizedOrder, PortfolioState


class FakeConfig:
    """Minimal config stub for PositionSizer."""

    def __init__(self, **overrides):
        self.risk = {
            "kelly_fraction": 0.25,
            "max_portfolio_exposure_cents": 50000,
            "max_daily_loss_cents": -10000,
            "max_concurrent_positions": 10,
            "max_same_game_positions": 3,
            "max_same_city_weather_positions": 2,
        }
        self.risk.update(overrides)
        self.markets = {
            "nba": {
                "max_position_per_trade": {
                    "games": 50,
                    "props": 25,
                    "futures": 10,
                },
            },
            "weather": {
                "max_position_per_trade": {
                    "temperature": 100,
                    "precipitation": 50,
                },
            },
        }


def _make_signal(edge=0.10, market_price=0.50, market_type="games", ticker="TEST"):
    return Signal(
        market_ticker=ticker,
        market_type=market_type,
        side="yes",
        model_prob=market_price + edge,
        market_price=market_price,
        edge=edge,
        confidence="high",
        liquidity_score=10.0,
        quality_score=1.0,
    )


def _make_portfolio(
    balance=100_00,  # $100 in cents
    positions=None,
    daily_pnl=0,
    exposure=0,
):
    return PortfolioState(
        balance_cents=balance,
        open_positions=positions or [],
        daily_pnl_cents=daily_pnl,
        total_exposure_cents=exposure,
    )


@pytest.fixture
def sizer():
    return PositionSizer(FakeConfig())


class TestKellyCalculation:
    """R8.1: Fractional Kelly sizing."""

    def test_basic_kelly(self, sizer):
        """Kelly = edge / (1 - market_price)."""
        raw = sizer.calculate_kelly(edge=0.10, market_price=0.50)
        assert abs(raw - 0.20) < 1e-6  # 0.10 / 0.50 = 0.20

    def test_kelly_high_price(self, sizer):
        """High market price → higher Kelly fraction."""
        raw = sizer.calculate_kelly(edge=0.10, market_price=0.80)
        assert abs(raw - 0.50) < 1e-6  # 0.10 / 0.20 = 0.50

    def test_kelly_low_price(self, sizer):
        """Low market price → lower Kelly fraction."""
        raw = sizer.calculate_kelly(edge=0.10, market_price=0.20)
        assert abs(raw - 0.125) < 1e-6  # 0.10 / 0.80 = 0.125

    def test_kelly_boundary_price_zero(self, sizer):
        """Market price of 0 → Kelly = 0 (avoid divide by zero)."""
        raw = sizer.calculate_kelly(edge=0.10, market_price=0.0)
        assert raw == 0.0

    def test_kelly_boundary_price_one(self, sizer):
        """Market price of 1.0 → Kelly = 0 (no edge possible)."""
        raw = sizer.calculate_kelly(edge=0.10, market_price=1.0)
        assert raw == 0.0


class TestFractionalKellyScaling:
    """R8.1: 0.25x Kelly multiplier applied."""

    def test_quarter_kelly(self, sizer):
        """Position size uses 0.25x raw Kelly."""
        signal = _make_signal(edge=0.10, market_price=0.50)
        portfolio = _make_portfolio(balance=100_00)  # $100

        sized = sizer.size_position(signal, portfolio)
        assert sized is not None
        assert sized.kelly_fraction == pytest.approx(0.20, abs=1e-4)
        assert sized.adjusted_fraction == pytest.approx(0.05, abs=1e-4)
        # 0.05 * 10000 cents / 50 cents = 10 contracts
        assert sized.quantity == 10


class TestPerTypeCaps:
    """R8.2: Per-trade position caps by market type."""

    def test_games_cap(self, sizer):
        assert sizer._get_per_type_cap("games") == 50

    def test_props_cap(self, sizer):
        assert sizer._get_per_type_cap("props") == 25

    def test_temperature_cap(self, sizer):
        assert sizer._get_per_type_cap("temperature") == 100

    def test_cap_applied(self):
        """Large position gets capped to per-type limit."""
        sizer = PositionSizer(FakeConfig())
        signal = _make_signal(edge=0.40, market_price=0.10, market_type="futures")
        # Kelly = 0.40/0.90 ≈ 0.44, adjusted = 0.11
        # 0.11 * 1_000_000 / 10 = 11000, cap at 10
        portfolio = _make_portfolio(balance=1_000_000)

        sized = sizer.size_position(signal, portfolio)
        assert sized is not None
        assert sized.quantity <= 10  # futures cap
        assert "per_type_cap" in sized.reason_capped


class TestPortfolioExposureCap:
    """R8.3: Max total open position value."""

    def test_at_exposure_limit_blocked(self, sizer):
        """No new positions when at exposure limit."""
        signal = _make_signal()
        portfolio = _make_portfolio(exposure=50000)  # at limit

        sized = sizer.size_position(signal, portfolio)
        assert sized is None

    def test_partial_exposure_remaining(self, sizer):
        """Position sized to fit remaining exposure."""
        signal = _make_signal(edge=0.10, market_price=0.50)
        portfolio = _make_portfolio(balance=100_00, exposure=49900)
        # Only 100 cents remaining for exposure
        # 100 / 50 cents = 2 contracts max

        sized = sizer.size_position(signal, portfolio)
        if sized is not None:
            assert sized.quantity <= 2


class TestMaxConcurrentPositions:
    """R8.5: Max concurrent positions."""

    def test_at_position_limit_blocked(self, sizer):
        """No new positions when at concurrent limit."""
        signal = _make_signal()
        positions = [
            {"ticker": f"GAME-{i}", "market_type": "games", "side": "yes", "quantity": 5, "price_cents": 50}
            for i in range(10)
        ]
        portfolio = _make_portfolio(positions=positions)

        sized = sizer.size_position(signal, portfolio)
        assert sized is None

    def test_under_position_limit_allowed(self, sizer):
        """Under concurrent limit → allowed."""
        signal = _make_signal()
        positions = [
            {"ticker": f"GAME-{i}", "market_type": "games", "side": "yes", "quantity": 5, "price_cents": 50}
            for i in range(5)
        ]
        portfolio = _make_portfolio(positions=positions)

        sized = sizer.size_position(signal, portfolio)
        assert sized is not None


class TestCorrelationCaps:
    """R8.6: Correlation limits for same-game and same-city."""

    def test_same_game_correlation_cap(self, sizer):
        """Max 3 positions in same NBA game."""
        signal = _make_signal(ticker="KXNBA-LAL-BOS-ML")
        positions = [
            {"ticker": "KXNBA-LAL-BOS-SPREAD", "market_type": "spreads", "side": "yes", "quantity": 5, "price_cents": 50},
            {"ticker": "KXNBA-LAL-BOS-TOTAL", "market_type": "totals", "side": "yes", "quantity": 5, "price_cents": 50},
            {"ticker": "KXNBA-LAL-BOS-PROP1", "market_type": "props", "side": "yes", "quantity": 5, "price_cents": 50},
        ]
        portfolio = _make_portfolio(positions=positions)

        sized = sizer.size_position(
            signal, portfolio, game_id="KXNBA-LAL-BOS"
        )
        assert sized is None

    def test_same_city_weather_cap(self, sizer):
        """Max 2 weather positions for same city on same day."""
        signal = _make_signal(market_type="temperature", ticker="KXNYC-TEMP")
        positions = [
            {"ticker": "KXNYC-PRECIP", "market_type": "precipitation", "side": "yes", "quantity": 5, "price_cents": 50},
            {"ticker": "KXNYC-TEMP2", "market_type": "temperature", "side": "yes", "quantity": 5, "price_cents": 50},
        ]
        portfolio = _make_portfolio(positions=positions)

        sized = sizer.size_position(
            signal, portfolio, city="NYC", date_str="2026-04-02"
        )
        assert sized is None


class TestPortfolioState:
    """Test PortfolioState helpers."""

    def test_num_positions(self):
        ps = _make_portfolio(
            positions=[{"ticker": "A"}, {"ticker": "B"}]
        )
        assert ps.num_positions == 2

    def test_count_same_game(self):
        ps = _make_portfolio(
            positions=[
                {"ticker": "GAME-1-ML"},
                {"ticker": "GAME-1-SPREAD"},
                {"ticker": "GAME-2-ML"},
            ]
        )
        assert ps.count_same_game_positions("GAME-1") == 2
        assert ps.count_same_game_positions("GAME-2") == 1

    def test_count_same_city_weather(self):
        ps = _make_portfolio(
            positions=[
                {"ticker": "KXNYC-TEMP", "market_type": "temperature"},
                {"ticker": "KXNYC-PRECIP", "market_type": "precipitation"},
                {"ticker": "KXCHI-TEMP", "market_type": "temperature"},
            ]
        )
        assert ps.count_same_city_weather("NYC", "2026-04-02") == 2
        assert ps.count_same_city_weather("CHI", "2026-04-02") == 1


class TestSizedOrderDataclass:
    def test_to_dict(self, sizer):
        signal = _make_signal()
        portfolio = _make_portfolio()
        sized = sizer.size_position(signal, portfolio)
        assert sized is not None
        d = sized.to_dict()
        assert "quantity" in d
        assert "kelly_fraction" in d
        assert "market_ticker" in d


def test_does_not_force_contract_above_kelly_budget(sizer):
    assert sizer.size_position(_make_signal(), _make_portfolio(balance=40)) is None


def test_quantity_never_exceeds_cash_with_large_kelly_multiplier():
    sizer = PositionSizer(FakeConfig(kelly_fraction=100))
    sized = sizer.size_position(_make_signal(), _make_portfolio(balance=100))
    assert sized is not None
    assert sized.quantity * 50 <= 100
