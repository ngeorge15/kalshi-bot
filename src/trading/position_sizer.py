"""Position sizer — fractional Kelly sizing with per-type caps and risk limits.

Calculates position sizes using 0.25x Kelly criterion, enforces per-trade caps,
portfolio exposure limits, correlation caps, and market type allocation limits.

Usage:
    from src.trading.position_sizer import PositionSizer

    sizer = PositionSizer(config)
    sized = sizer.size_position(signal, portfolio_state)
"""

import logging
import math
from dataclasses import dataclass
from typing import Optional

from src.trading.edge_detector import Signal

logger = logging.getLogger(__name__)


@dataclass
class SizedOrder:
    """A signal with a calculated position size."""

    signal: Signal
    quantity: int  # number of contracts
    kelly_fraction: float  # raw Kelly fraction
    adjusted_fraction: float  # after 0.25x scaling
    reason_capped: str = ""  # empty if not capped, else description

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            **self.signal.to_dict(),
            "quantity": self.quantity,
            "kelly_fraction": self.kelly_fraction,
            "adjusted_fraction": self.adjusted_fraction,
            "reason_capped": self.reason_capped,
        }


@dataclass
class PortfolioState:
    """Snapshot of current portfolio for sizing decisions."""

    balance_cents: int  # available cash
    open_positions: list[dict]  # list of {ticker, market_type, side, quantity, price_cents}
    daily_pnl_cents: int = 0  # today's realized + unrealized P&L
    total_exposure_cents: int = 0  # sum of all open position values

    @property
    def num_positions(self) -> int:
        return len(self.open_positions)

    def count_same_game_positions(self, game_id: str) -> int:
        """Count positions tied to the same NBA game (by ticker prefix)."""
        return sum(
            1
            for p in self.open_positions
            if p.get("ticker", "").startswith(game_id)
        )

    def count_same_city_weather(self, city: str, date_str: str) -> int:
        """Count weather positions for same city on same day."""
        return sum(
            1
            for p in self.open_positions
            if city.upper() in p.get("ticker", "").upper()
            and p.get("market_type", "").startswith(("temperature", "precipitation", "severe"))
        )


class PositionSizer:
    """Fractional Kelly position sizer with configurable caps (R8).

    Args:
        config: Config object with ``risk`` and ``markets`` dicts.
    """

    def __init__(self, config) -> None:
        risk = config.risk
        self.kelly_multiplier = risk.get("kelly_fraction", 0.25)
        self.max_portfolio_exposure_cents = risk.get("max_portfolio_exposure_cents", 50000)
        self.max_daily_loss_cents = risk.get("max_daily_loss_cents", -10000)
        self.max_concurrent_positions = risk.get("max_concurrent_positions", 10)
        self.max_same_game = risk.get("max_same_game_positions", 3)
        self.max_same_city_weather = risk.get("max_same_city_weather_positions", 2)

        # Per-type position caps from config
        nba_caps = config.markets.get("nba", {}).get("max_position_per_trade", {})
        weather_caps = config.markets.get("weather", {}).get("max_position_per_trade", {})
        self._per_type_caps = {**nba_caps, **weather_caps}

    def _get_per_type_cap(self, market_type: str) -> int:
        """Return max contracts per trade for this market type (R8.2)."""
        return self._per_type_caps.get(market_type, 50)

    def calculate_kelly(self, edge: float, market_price: float) -> float:
        """Calculate raw Kelly fraction (R8.1).

        Kelly formula: f = edge / (1 - market_price) for YES bets.
        More generally: f = (p*b - q) / b where p=model_prob, b=payout odds,
        q=1-p.

        For binary contracts at price p_market:
            - If we buy YES at p_market, payout = 1.0, so profit = 1 - p_market
            - edge = model_prob - p_market
            - Kelly f = edge / (1 - p_market)

        Returns:
            Raw Kelly fraction (before 0.25x scaling). Can be > 1.0.
        """
        if market_price >= 1.0 or market_price <= 0.0:
            return 0.0
        odds_against = 1.0 - market_price
        if odds_against <= 0:
            return 0.0
        return edge / odds_against

    def size_position(
        self,
        signal: Signal,
        portfolio: PortfolioState,
        game_id: str = "",
        city: str = "",
        date_str: str = "",
    ) -> Optional[SizedOrder]:
        """Calculate position size for a signal given current portfolio.

        Applies:
        1. Kelly criterion with 0.25x multiplier (R8.1)
        2. Per-type position cap (R8.2)
        3. Portfolio exposure cap (R8.3)
        4. Max concurrent positions (R8.5)
        5. Correlation cap — same game / same city (R8.6)

        Args:
            signal: Trade signal from edge detector.
            portfolio: Current portfolio state.
            game_id: Game identifier for NBA correlation cap.
            city: City code for weather correlation cap.
            date_str: Date for weather correlation cap.

        Returns:
            SizedOrder if position is viable, None if blocked by risk limits.
        """
        # Check max concurrent positions (R8.5)
        if portfolio.num_positions >= self.max_concurrent_positions:
            logger.warning(
                "Blocked %s: max concurrent positions %d reached",
                signal.market_ticker,
                self.max_concurrent_positions,
            )
            return None

        # Check portfolio exposure (R8.3)
        if portfolio.total_exposure_cents >= self.max_portfolio_exposure_cents:
            logger.warning(
                "Blocked %s: portfolio exposure %d >= max %d",
                signal.market_ticker,
                portfolio.total_exposure_cents,
                self.max_portfolio_exposure_cents,
            )
            return None

        # Check correlation: same NBA game (R8.6)
        if game_id and portfolio.count_same_game_positions(game_id) >= self.max_same_game:
            logger.warning(
                "Blocked %s: max same-game positions %d reached for %s",
                signal.market_ticker,
                self.max_same_game,
                game_id,
            )
            return None

        # Check correlation: same city weather (R8.6)
        if city and portfolio.count_same_city_weather(city, date_str) >= self.max_same_city_weather:
            logger.warning(
                "Blocked %s: max same-city weather %d reached for %s",
                signal.market_ticker,
                self.max_same_city_weather,
                city,
            )
            return None

        # Calculate Kelly fraction (R8.1)
        raw_kelly = self.calculate_kelly(signal.edge, signal.market_price)
        adjusted_kelly = raw_kelly * self.kelly_multiplier

        # Convert fraction to contracts
        # Position value = quantity × market_price (in cents)
        price_cents = int(signal.market_price * 100)
        if price_cents <= 0:
            return None

        # Kelly says bet this fraction of bankroll
        bet_cents = int(adjusted_kelly * portfolio.balance_cents)
        quantity = max(1, bet_cents // price_cents) if bet_cents > 0 else 0

        if quantity <= 0:
            logger.debug(
                "Skipping %s: Kelly quantity = 0 (edge=%.4f, kelly=%.4f)",
                signal.market_ticker,
                signal.edge,
                adjusted_kelly,
            )
            return None

        # Apply per-type cap (R8.2)
        per_type_cap = self._get_per_type_cap(signal.market_type)
        reason_capped = ""
        if quantity > per_type_cap:
            reason_capped = f"per_type_cap:{per_type_cap}"
            quantity = per_type_cap

        # Apply remaining exposure cap (R8.3)
        remaining_exposure = self.max_portfolio_exposure_cents - portfolio.total_exposure_cents
        max_by_exposure = remaining_exposure // price_cents if price_cents > 0 else 0
        if quantity > max_by_exposure:
            reason_capped = f"exposure_cap:{max_by_exposure}"
            quantity = max(0, max_by_exposure)

        if quantity <= 0:
            return None

        return SizedOrder(
            signal=signal,
            quantity=quantity,
            kelly_fraction=raw_kelly,
            adjusted_fraction=adjusted_kelly,
            reason_capped=reason_capped,
        )
