"""Tests for src/models/weather_temp.py — Temperature bracket model.

Wave 0: All tests marked xfail(strict=True).
"""
import pytest
import numpy as np


@pytest.mark.xfail(strict=True, reason="weather_temp.py not implemented yet")
def test_predict_bracket_probabilities_sum_to_one():
    """predict_brackets() returns probabilities that sum to 1.0 across all brackets."""
    from src.models.weather_temp import weather_temp_model
    probs = weather_temp_model.predict_brackets(station="KNYC", forecast_date="2026-04-01")
    assert abs(sum(probs.values()) - 1.0) < 0.01


@pytest.mark.xfail(strict=True, reason="weather_temp.py not implemented yet")
def test_predict_threshold_returns_valid_probability():
    """predict_threshold() returns float in [0.0, 1.0]."""
    from src.models.weather_temp import weather_temp_model
    prob = weather_temp_model.predict_threshold(station="KNYC", threshold_f=50.0, forecast_date="2026-04-01")
    assert 0.0 <= prob <= 1.0


@pytest.mark.xfail(strict=True, reason="weather_temp.py not implemented yet")
def test_bias_correction_applied():
    """predict_brackets() uses additive bias correction per station x month (D-10)."""
    from src.models.weather_temp import weather_temp_model
    # Verify bias_offsets dict is populated after fit
    assert "KNYC" in weather_temp_model.bias_offsets
    assert len(weather_temp_model.bias_offsets["KNYC"]) == 12  # 12 months
