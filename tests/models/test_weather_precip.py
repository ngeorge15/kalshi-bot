"""Tests for src/models/weather_precip.py — Precipitation threshold model."""
import pytest
from unittest.mock import patch


def test_predict_returns_probability_in_range(tmp_path):
    """predict() returns P(precip > threshold) in [0.0, 1.0]."""
    from src.models.weather_precip import WeatherPrecipModel

    model = WeatherPrecipModel(db_path=str(tmp_path / "test.db"))

    # Mock NWS forecast to return a period with 40% PoP
    mock_periods = [
        {
            "startTime": "2026-04-01T06:00:00-04:00",
            "probabilityOfPrecipitation": {"value": 40},
            "detailedForecast": "Chance of rain.",
        }
    ]
    with patch("src.data.weather.nws.get_nws_forecast", return_value=mock_periods):
        prob = model.predict(
            station="KNYC", threshold_in=0.1, forecast_date="2026-04-01",
        )

    assert 0.0 <= prob <= 1.0


def test_predict_lead_time_weighting(tmp_path):
    """Closer forecasts should weight NWS PoP more heavily (R5.5)."""
    from src.models.weather_precip import WeatherPrecipModel

    model = WeatherPrecipModel(db_path=str(tmp_path / "test.db"))

    mock_periods = [
        {
            "startTime": "2026-04-01T06:00:00-04:00",
            "probabilityOfPrecipitation": {"value": 80},
            "detailedForecast": "",
        }
    ]
    with patch("src.data.weather.nws.get_nws_forecast", return_value=mock_periods):
        prob_1day = model.predict("KNYC", 0.1, "2026-04-01", days_until=1)
        prob_6day = model.predict("KNYC", 0.1, "2026-04-01", days_until=6)

    # 1-day forecast should weight NWS more → higher prob when NWS says 80%
    assert prob_1day > prob_6day
