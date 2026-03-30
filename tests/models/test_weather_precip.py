"""Tests for src/models/weather_precip.py — Precipitation threshold model.

Wave 0: All tests marked xfail(strict=True).
"""
import pytest


@pytest.mark.xfail(strict=True, reason="weather_precip.py not implemented yet")
def test_predict_returns_probability_in_range():
    """predict() returns P(precip > threshold) in [0.0, 1.0]."""
    from src.models.weather_precip import weather_precip_model
    prob = weather_precip_model.predict(station="KNYC", threshold_in=0.1, forecast_date="2026-04-01")
    assert 0.0 <= prob <= 1.0
