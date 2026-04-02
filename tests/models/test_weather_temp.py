"""Tests for src/models/weather_temp.py — Temperature bracket model."""
import pytest
from unittest.mock import patch, MagicMock


def test_predict_bracket_probabilities_sum_to_one(tmp_path):
    """predict_brackets() returns probabilities that sum to 1.0 across all brackets."""
    from src.models.weather_temp import WeatherTempModel

    model = WeatherTempModel(db_path=str(tmp_path / "test.db"))
    # Pre-set bias offsets so fit_bias_correction is not needed
    model.bias_offsets = {"KNYC": {m: (0.0, 5.0) for m in range(1, 13)}}
    model._is_fitted = True

    # Mock NWS hourly to return a forecast of 55°F
    mock_hourly = [
        {"startTime": "2026-04-01T15:00:00+00:00", "temperature": 55},
        {"startTime": "2026-04-01T18:00:00+00:00", "temperature": 58},
    ]
    with patch("src.data.weather.nws.get_nws_hourly", return_value=mock_hourly):
        probs = model.predict_brackets(station="KNYC", forecast_date="2026-04-01")

    assert abs(sum(probs.values()) - 1.0) < 0.01
    assert all(0.0 <= v <= 1.0 for v in probs.values())


def test_predict_threshold_returns_valid_probability(tmp_path):
    """predict_threshold() returns float in [0.0, 1.0]."""
    from src.models.weather_temp import WeatherTempModel

    model = WeatherTempModel(db_path=str(tmp_path / "test.db"))
    model.bias_offsets = {"KNYC": {m: (0.0, 5.0) for m in range(1, 13)}}
    model._is_fitted = True

    mock_hourly = [
        {"startTime": "2026-04-01T15:00:00+00:00", "temperature": 55},
    ]
    with patch("src.data.weather.nws.get_nws_hourly", return_value=mock_hourly):
        prob = model.predict_threshold(station="KNYC", threshold_f=50.0, forecast_date="2026-04-01")

    assert 0.0 <= prob <= 1.0


def test_bias_correction_applied(tmp_path):
    """fit_bias_correction() populates bias_offsets with 12 months per station."""
    from src.models.weather_temp import WeatherTempModel

    model = WeatherTempModel(db_path=str(tmp_path / "test.db"))

    # No NOAA data → should use default sigma=5.0 for all stations
    result = model.fit_bias_correction(min_observations=30)

    assert "KNYC" in model.bias_offsets
    assert len(model.bias_offsets["KNYC"]) == 12  # 12 months
    # With no data, defaults to (0.0, 5.0)
    assert model.bias_offsets["KNYC"][4] == (0.0, 5.0)
    assert model._is_fitted is True
