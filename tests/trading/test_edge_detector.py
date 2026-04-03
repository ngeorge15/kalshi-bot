"""Unit tests for the edge detector module."""

import math

import pytest

from src.trading.edge_detector import EdgeDetector, Signal, CONFIDENCE_WEIGHTS


class FakeConfig:
    """Minimal config stub for EdgeDetector."""

    def __init__(self):
        self.markets = {
            "nba": {
                "min_edge": {
                    "games": 0.05,
                    "spreads": 0.05,
                    "totals": 0.05,
                    "props": 0.07,
                    "futures": 0.10,
                },
            },
            "weather": {
                "min_edge": {
                    "temperature": 0.03,
                    "precipitation": 0.05,
                    "severe": 0.08,
                },
            },
        }
        self.risk = {}


@pytest.fixture
def config():
    return FakeConfig()


@pytest.fixture
def detector(config):
    return EdgeDetector(config)


class TestEdgeCalculation:
    """R7.2: Edge = model_prob - market_price."""

    def test_positive_yes_edge(self, detector):
        """Model says 70% but market is at 50% → buy YES."""
        edge, side = detector.calculate_edge(0.70, 0.50)
        assert side == "yes"
        assert abs(edge - 0.20) < 1e-6

    def test_positive_no_edge(self, detector):
        """Model says 30% but market is at 50% → buy NO."""
        edge, side = detector.calculate_edge(0.30, 0.50)
        assert side == "no"
        assert abs(edge - 0.20) < 1e-6

    def test_zero_edge(self, detector):
        """Model agrees with market → edge is 0."""
        edge, side = detector.calculate_edge(0.50, 0.50)
        assert abs(edge) < 1e-6

    def test_small_edge_yes(self, detector):
        """Small YES edge."""
        edge, side = detector.calculate_edge(0.55, 0.50)
        assert side == "yes"
        assert abs(edge - 0.05) < 1e-6

    def test_extreme_edge(self, detector):
        """Model says 90% vs market 10% → large YES edge."""
        edge, side = detector.calculate_edge(0.90, 0.10)
        assert side == "yes"
        assert abs(edge - 0.80) < 1e-6


class TestMinEdgeThresholds:
    """R7.3: Per-type minimum edge thresholds."""

    def test_nba_games_min_edge(self, detector):
        assert detector._get_min_edge("games") == 0.05

    def test_nba_props_min_edge(self, detector):
        assert detector._get_min_edge("props") == 0.07

    def test_weather_temperature_min_edge(self, detector):
        assert detector._get_min_edge("temperature") == 0.03

    def test_unknown_market_type_default(self, detector):
        assert detector._get_min_edge("unknown_type") == 0.05

    def test_edge_below_threshold_returns_none(self, detector):
        """Signal with edge below threshold should be filtered out."""
        signal = detector.evaluate_market(
            ticker="KXNBAGAME-test",
            market_type="games",
            model_prob=0.53,  # edge = 0.03, below 0.05 min
            market_yes_price=0.50,
            confidence="high",
            volume=100,
            bid_ask_spread=0.02,
        )
        assert signal is None

    def test_edge_above_threshold_returns_signal(self, detector):
        """Signal with edge above threshold should pass."""
        signal = detector.evaluate_market(
            ticker="KXNBAGAME-test",
            market_type="games",
            model_prob=0.60,  # edge = 0.10, above 0.05 min
            market_yes_price=0.50,
            confidence="high",
            volume=100,
            bid_ask_spread=0.02,
        )
        assert signal is not None
        assert signal.market_ticker == "KXNBAGAME-test"
        assert abs(signal.edge - 0.10) < 1e-6


class TestLiquidityFilter:
    """R7.4: Skip markets with wide spreads or low volume."""

    def test_wide_spread_filtered(self, detector):
        """Wide bid-ask spread → filtered out."""
        signal = detector.evaluate_market(
            ticker="KXNBAGAME-test",
            market_type="games",
            model_prob=0.70,
            market_yes_price=0.50,
            confidence="high",
            volume=100,
            bid_ask_spread=0.20,  # > 0.15 max
        )
        assert signal is None

    def test_low_volume_filtered(self, detector):
        """Low volume → filtered out."""
        signal = detector.evaluate_market(
            ticker="KXNBAGAME-test",
            market_type="games",
            model_prob=0.70,
            market_yes_price=0.50,
            confidence="high",
            volume=2,  # < 5 min
            bid_ask_spread=0.02,
        )
        assert signal is None

    def test_sufficient_liquidity_passes(self, detector):
        """Adequate volume and tight spread → passes."""
        signal = detector.evaluate_market(
            ticker="KXNBAGAME-test",
            market_type="games",
            model_prob=0.70,
            market_yes_price=0.50,
            confidence="high",
            volume=50,
            bid_ask_spread=0.03,
        )
        assert signal is not None


class TestConfidenceFilter:
    """R7.5: Skip markets with low confidence (incomplete features)."""

    def test_low_confidence_filtered(self, detector):
        """Low confidence (e.g., GTD player) → filtered out."""
        signal = detector.evaluate_market(
            ticker="KXNBAPROP-test",
            market_type="props",
            model_prob=0.80,
            market_yes_price=0.50,
            confidence="low",
            volume=100,
            bid_ask_spread=0.02,
        )
        assert signal is None

    def test_medium_confidence_passes(self, detector):
        """Medium confidence still passes (with lower weight)."""
        signal = detector.evaluate_market(
            ticker="KXNBAGAME-test",
            market_type="games",
            model_prob=0.70,
            market_yes_price=0.50,
            confidence="medium",
            volume=50,
            bid_ask_spread=0.02,
        )
        assert signal is not None
        assert signal.confidence == "medium"


class TestQualityRanking:
    """R7.6: Quality score = edge × sqrt(liquidity) × confidence_weight."""

    def test_quality_score_formula(self, detector):
        """Quality score correctly combines edge, liquidity, confidence."""
        signal = detector.evaluate_market(
            ticker="KXNBAGAME-test",
            market_type="games",
            model_prob=0.70,
            market_yes_price=0.50,
            confidence="high",
            volume=100,
            bid_ask_spread=0.02,
        )
        assert signal is not None
        expected = 0.20 * math.sqrt(100) * 1.0  # 0.20 * 10 * 1.0 = 2.0
        assert abs(signal.quality_score - expected) < 1e-4

    def test_medium_confidence_lower_quality(self, detector):
        """Medium confidence → 0.7x quality score."""
        signal = detector.evaluate_market(
            ticker="KXNBAGAME-test",
            market_type="games",
            model_prob=0.70,
            market_yes_price=0.50,
            confidence="medium",
            volume=100,
            bid_ask_spread=0.02,
        )
        assert signal is not None
        expected = 0.20 * math.sqrt(100) * 0.7  # 0.20 * 10 * 0.7 = 1.4
        assert abs(signal.quality_score - expected) < 1e-4

    def test_rank_signals_descending(self, detector):
        """Signals are ranked by quality score, highest first."""
        signals = [
            Signal(
                market_ticker="low",
                market_type="games",
                side="yes",
                model_prob=0.55,
                market_price=0.50,
                edge=0.05,
                confidence="high",
                liquidity_score=5.0,
                quality_score=0.25,
            ),
            Signal(
                market_ticker="high",
                market_type="games",
                side="yes",
                model_prob=0.80,
                market_price=0.50,
                edge=0.30,
                confidence="high",
                liquidity_score=10.0,
                quality_score=3.0,
            ),
            Signal(
                market_ticker="mid",
                market_type="games",
                side="yes",
                model_prob=0.65,
                market_price=0.50,
                edge=0.15,
                confidence="high",
                liquidity_score=7.0,
                quality_score=1.05,
            ),
        ]
        ranked = detector.rank_signals(signals)
        assert ranked[0].market_ticker == "high"
        assert ranked[1].market_ticker == "mid"
        assert ranked[2].market_ticker == "low"


class TestScanAndRank:
    """Integration test for full scan_and_rank pipeline."""

    def test_scan_filters_and_ranks(self, detector):
        """Scan evaluates all markets, filters, and ranks."""
        evaluations = [
            {
                "ticker": "GAME-A",
                "market_type": "games",
                "model_prob": 0.70,
                "market_yes_price": 0.50,
                "confidence": "high",
                "volume": 100,
                "bid_ask_spread": 0.02,
            },
            {
                "ticker": "GAME-B",
                "market_type": "games",
                "model_prob": 0.52,  # edge too small (0.02)
                "market_yes_price": 0.50,
                "confidence": "high",
                "volume": 100,
                "bid_ask_spread": 0.02,
            },
            {
                "ticker": "TEMP-C",
                "market_type": "temperature",
                "model_prob": 0.60,
                "market_yes_price": 0.50,
                "confidence": "high",
                "volume": 200,
                "bid_ask_spread": 0.01,
            },
            {
                "ticker": "PROP-D",
                "market_type": "props",
                "model_prob": 0.80,
                "market_yes_price": 0.50,
                "confidence": "low",  # filtered
                "volume": 50,
                "bid_ask_spread": 0.02,
            },
        ]
        signals = detector.scan_and_rank(evaluations)
        # GAME-B filtered (edge too small), PROP-D filtered (low confidence)
        assert len(signals) == 2
        tickers = [s.market_ticker for s in signals]
        assert "GAME-A" in tickers
        assert "TEMP-C" in tickers
        assert "GAME-B" not in tickers
        assert "PROP-D" not in tickers


class TestSignalDataclass:
    """Test Signal dataclass conversion."""

    def test_to_dict(self):
        signal = Signal(
            market_ticker="TEST",
            market_type="games",
            side="yes",
            model_prob=0.70,
            market_price=0.50,
            edge=0.20,
            confidence="high",
            liquidity_score=10.0,
            quality_score=2.0,
            model_name="nba_game",
            model_version=1,
        )
        d = signal.to_dict()
        assert d["market_ticker"] == "TEST"
        assert d["edge"] == 0.20
        assert d["model_name"] == "nba_game"
