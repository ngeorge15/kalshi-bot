"""End-to-end integration tests for Phase 3: prediction models + validation.

Tests in TestPhase3Integration class require KALSHI_INTEGRATION=true.
Wave 0: All tests marked xfail(strict=True).
"""
import pytest


@pytest.mark.xfail(strict=True, reason="Phase 3 not implemented yet")
def test_nba_game_model_predict_on_todays_games():
    """NBA game model produces calibrated predictions for tonight's schedule."""
    pytest.skip("Phase 3 not implemented yet")


@pytest.mark.xfail(strict=True, reason="Phase 3 not implemented yet")
def test_weather_temp_model_predict_brackets_nyc():
    """Weather temp model produces bracket probabilities summing to 1.0 for NYC."""
    pytest.skip("Phase 3 not implemented yet")


@pytest.mark.xfail(strict=True, reason="Phase 3 not implemented yet")
def test_walk_forward_validation_on_nba_history():
    """Walk-forward validator runs on NBA historical data without errors."""
    pytest.skip("Phase 3 not implemented yet")


@pytest.mark.xfail(strict=True, reason="Phase 3 not implemented yet")
def test_holdout_never_accessed_during_training():
    """Training pipeline never reads holdout data (verified by access_holdout guard)."""
    pytest.skip("Phase 3 not implemented yet")
