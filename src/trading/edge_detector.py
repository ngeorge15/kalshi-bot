"""Edge detector — compares model probabilities to Kalshi market prices.

Scans all open markets in scope, calculates edge as model_prob - market_price,
applies per-type minimum edge thresholds and liquidity filters, ranks signals
by quality score: edge × sqrt(liquidity) × confidence.

Usage:
    from src.trading.edge_detector import EdgeDetector

    detector = EdgeDetector(config)
    signals = detector.scan_markets(kalshi_client, models)
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class Signal:
    """A trade signal with edge, confidence, and quality metrics (R7.7)."""

    market_ticker: str
    market_type: str
    side: str  # 'yes' or 'no'
    model_prob: float
    market_price: float  # price in [0, 1] (cents / 100)
    edge: float  # model_prob - market_price
    confidence: str  # 'high', 'medium', 'low'
    liquidity_score: float  # sqrt(volume) or similar
    quality_score: float = 0.0  # edge × sqrt(liquidity) × confidence_weight
    model_name: str = ""
    model_version: int = 0
    features_json: str = ""
    bid_ask_spread: float = 0.0
    volume: int = 0

    def to_dict(self) -> dict:
        """Convert to dictionary for SQLite storage."""
        return {
            "market_ticker": self.market_ticker,
            "market_type": self.market_type,
            "side": self.side,
            "model_prob": self.model_prob,
            "market_price": self.market_price,
            "edge": self.edge,
            "confidence": self.confidence,
            "liquidity_score": self.liquidity_score,
            "quality_score": self.quality_score,
            "model_name": self.model_name,
            "model_version": self.model_version,
            "features_json": self.features_json,
            "bid_ask_spread": self.bid_ask_spread,
            "volume": self.volume,
        }


# Confidence weight mapping for quality score calculation
CONFIDENCE_WEIGHTS = {"high": 1.0, "medium": 0.7, "low": 0.3}


class EdgeDetector:
    """Detect edges between model probabilities and Kalshi market prices (R7).

    Compares model output to best available market price for each open market,
    filters by minimum edge threshold (R7.3), liquidity (R7.4), and confidence
    (R7.5), then ranks signals by quality (R7.6).

    Args:
        config: Trading config dict (from Config.markets and Config.risk).
    """

    def __init__(self, config) -> None:
        self._nba_config = config.markets.get("nba", {})
        self._weather_config = config.markets.get("weather", {})

    def _get_min_edge(self, market_type: str) -> float:
        """Return minimum edge threshold for a market type (R7.3)."""
        nba_edges = self._nba_config.get("min_edge", {})
        weather_edges = self._weather_config.get("min_edge", {})
        all_edges = {**nba_edges, **weather_edges}
        return all_edges.get(market_type, 0.05)

    def _get_max_spread(self, market_type: str) -> float:
        """Return max bid-ask spread filter.

        Default 0.15 (15 cents) — skip illiquid markets (R7.4).
        """
        return 0.15

    def _get_min_volume(self, market_type: str) -> int:
        """Return minimum volume filter (R7.4). Default 5 contracts."""
        return 5

    def calculate_edge(
        self,
        model_prob: float,
        market_yes_price: float,
    ) -> tuple[float, str]:
        """Calculate edge and optimal side (R7.2).

        Returns:
            (edge, side) where edge is always positive if there's an opportunity,
            and side is 'yes' or 'no'.
        """
        # Edge on YES side: model says higher prob than market
        yes_edge = model_prob - market_yes_price
        # Edge on NO side: model says lower prob than market
        no_edge = (1.0 - model_prob) - (1.0 - market_yes_price)
        # no_edge = market_yes_price - model_prob (equivalent)

        if yes_edge >= abs(no_edge):
            return yes_edge, "yes"
        else:
            return -yes_edge, "no"  # -yes_edge = no_edge

    def evaluate_market(
        self,
        ticker: str,
        market_type: str,
        model_prob: float,
        market_yes_price: float,
        confidence: str = "high",
        volume: int = 0,
        bid_ask_spread: float = 0.0,
        model_name: str = "",
        model_version: int = 0,
        features_json: str = "",
    ) -> Optional[Signal]:
        """Evaluate a single market and return a Signal if edge exceeds threshold.

        Args:
            ticker: Market ticker.
            market_type: Category (games, spreads, totals, props, temperature, etc.)
            model_prob: Model's probability for the YES outcome.
            market_yes_price: Market YES price in [0, 1].
            confidence: Feature completeness confidence ('high', 'medium', 'low').
            volume: Recent trading volume.
            bid_ask_spread: Current bid-ask spread in [0, 1].
            model_name: Name of the model that generated the probability.
            model_version: Version of the model.
            features_json: JSON string of features used.

        Returns:
            Signal if edge exceeds threshold and passes filters, else None.
        """
        import math

        min_edge = self._get_min_edge(market_type)
        max_spread = self._get_max_spread(market_type)
        min_volume = self._get_min_volume(market_type)

        # R7.5: Confidence filtering — skip low-confidence predictions
        if confidence == "low":
            logger.debug(
                "Skipping %s: low confidence (incomplete features)", ticker
            )
            return None

        # R7.4: Liquidity filter — skip wide spreads
        if bid_ask_spread > max_spread:
            logger.debug(
                "Skipping %s: bid-ask spread %.3f > max %.3f",
                ticker,
                bid_ask_spread,
                max_spread,
            )
            return None

        # R7.4: Volume filter
        if volume < min_volume:
            logger.debug(
                "Skipping %s: volume %d < min %d", ticker, volume, min_volume
            )
            return None

        # R7.2: Calculate edge
        edge, side = self.calculate_edge(model_prob, market_yes_price)

        # R7.3: Minimum edge threshold
        if edge < min_edge:
            logger.debug(
                "Skipping %s: edge %.4f < min %.4f", ticker, edge, min_edge
            )
            return None

        # R7.6: Quality score = edge × sqrt(liquidity) × confidence_weight
        liquidity_score = math.sqrt(max(volume, 1))
        confidence_weight = CONFIDENCE_WEIGHTS.get(confidence, 0.5)
        quality_score = edge * liquidity_score * confidence_weight

        signal = Signal(
            market_ticker=ticker,
            market_type=market_type,
            side=side,
            model_prob=model_prob,
            market_price=market_yes_price if side == "yes" else (1.0 - market_yes_price),
            edge=edge,
            confidence=confidence,
            liquidity_score=liquidity_score,
            quality_score=quality_score,
            model_name=model_name,
            model_version=model_version,
            features_json=features_json,
            bid_ask_spread=bid_ask_spread,
            volume=volume,
        )

        logger.info(
            "Signal: %s %s edge=%.4f quality=%.4f confidence=%s",
            ticker,
            side,
            edge,
            quality_score,
            confidence,
        )
        return signal

    def rank_signals(self, signals: list[Signal]) -> list[Signal]:
        """Rank signals by quality score descending (R7.6).

        Returns:
            Sorted list of signals, highest quality first.
        """
        return sorted(signals, key=lambda s: s.quality_score, reverse=True)

    def scan_and_rank(
        self,
        market_evaluations: list[dict],
    ) -> list[Signal]:
        """Evaluate all markets and return ranked signals.

        Args:
            market_evaluations: List of dicts, each with keys:
                ticker, market_type, model_prob, market_yes_price,
                confidence, volume, bid_ask_spread, model_name,
                model_version, features_json.

        Returns:
            Ranked list of signals passing all filters.
        """
        signals = []
        for m in market_evaluations:
            signal = self.evaluate_market(
                ticker=m["ticker"],
                market_type=m["market_type"],
                model_prob=m["model_prob"],
                market_yes_price=m["market_yes_price"],
                confidence=m.get("confidence", "high"),
                volume=m.get("volume", 0),
                bid_ask_spread=m.get("bid_ask_spread", 0.0),
                model_name=m.get("model_name", ""),
                model_version=m.get("model_version", 0),
                features_json=m.get("features_json", ""),
            )
            if signal is not None:
                signals.append(signal)

        ranked = self.rank_signals(signals)
        logger.info(
            "scan_and_rank: %d markets evaluated → %d signals",
            len(market_evaluations),
            len(ranked),
        )
        return ranked
