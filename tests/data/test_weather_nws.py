"""Tests for src/data/weather/nws.py — NWS forecast, griddata, and delta detection."""

import json
import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _mock_response(json_data: dict, status_code: int = 200) -> MagicMock:
    """Return a mock requests.Response with .json() and .raise_for_status()."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.raise_for_status = MagicMock()
    return resp


POINTS_RESPONSE = {
    "properties": {
        "gridId": "OKX",
        "gridX": 33,
        "gridY": 37,
        "forecast": "https://api.weather.gov/gridpoints/OKX/33,37/forecast",
        "forecastHourly": "https://api.weather.gov/gridpoints/OKX/33,37/forecast/hourly",
        "forecastGridData": "https://api.weather.gov/gridpoints/OKX/33,37",
    }
}

FORECAST_RESPONSE = {
    "properties": {
        "periods": [
            {
                "name": "Tonight",
                "startTime": "2026-03-29T18:00:00-05:00",
                "temperature": 55,
                "temperatureUnit": "F",
                "shortForecast": "Partly Cloudy",
            },
            {
                "name": "Monday",
                "startTime": "2026-03-30T06:00:00-05:00",
                "temperature": 70,
                "temperatureUnit": "F",
                "shortForecast": "Sunny",
            },
        ]
    }
}

HOURLY_RESPONSE = {
    "properties": {
        "periods": [
            {
                "startTime": "2026-03-29T18:00:00-05:00",
                "temperature": 58,
                "temperatureUnit": "F",
                "shortForecast": "Partly Cloudy",
            }
        ]
    }
}

GRIDDATA_RESPONSE = {
    "properties": {
        "temperature": {
            "uom": "wmoUnit:degC",
            "values": [
                {"validTime": "2026-03-29T18:00:00+00:00/PT1H", "value": 12.8}
            ],
        },
        "probabilityOfPrecipitation": {
            "uom": "wmoUnit:percent",
            "values": [
                {"validTime": "2026-03-29T18:00:00+00:00/PT6H", "value": 20}
            ],
        },
    }
}


# ---------------------------------------------------------------------------
# _parse_valid_time
# ---------------------------------------------------------------------------


def test_parse_valid_time_returns_datetime_and_timedelta():
    from src.data.weather.nws import _parse_valid_time

    dt, dur = _parse_valid_time("2026-03-29T18:00:00+00:00/PT1H")
    assert isinstance(dt, datetime)
    assert dur == timedelta(hours=1)


def test_parse_valid_time_pt6h():
    from src.data.weather.nws import _parse_valid_time

    _, dur = _parse_valid_time("2026-03-29T18:00:00+00:00/PT6H")
    assert dur == timedelta(hours=6)


def test_parse_valid_time_pt12h():
    from src.data.weather.nws import _parse_valid_time

    _, dur = _parse_valid_time("2026-03-29T18:00:00+00:00/PT12H")
    assert dur == timedelta(hours=12)


def test_parse_valid_time_datetime_value():
    from src.data.weather.nws import _parse_valid_time

    dt, _ = _parse_valid_time("2026-03-29T18:00:00+00:00/PT1H")
    assert dt.year == 2026
    assert dt.month == 3
    assert dt.day == 29
    assert dt.hour == 18


# ---------------------------------------------------------------------------
# get_nws_forecast
# ---------------------------------------------------------------------------


def test_get_nws_forecast_returns_periods(tmp_path, monkeypatch):
    import src.data.cache as cache_mod
    from src.data.weather import nws

    monkeypatch.setattr(cache_mod, "CACHE_DIR", tmp_path / "cache")
    # Reset the module-level session singleton
    monkeypatch.setattr(nws, "_SESSION", None)

    with patch("requests.Session.get") as mock_get:
        mock_get.side_effect = [
            _mock_response(POINTS_RESPONSE),   # /points call
            _mock_response(FORECAST_RESPONSE), # /forecast call
        ]
        periods = nws.get_nws_forecast(40.7794, -73.9692)

    assert isinstance(periods, list)
    assert len(periods) == 2
    assert periods[0]["temperature"] == 55
    assert "shortForecast" in periods[0]


def test_get_nws_forecast_uses_cache(tmp_path, monkeypatch):
    """Second call within TTL should not trigger a new HTTP request."""
    import src.data.cache as cache_mod
    from src.data.weather import nws

    monkeypatch.setattr(cache_mod, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(nws, "_SESSION", None)

    with patch("requests.Session.get") as mock_get:
        mock_get.side_effect = [
            _mock_response(POINTS_RESPONSE),
            _mock_response(FORECAST_RESPONSE),
        ]
        # First call populates cache
        nws.get_nws_forecast(40.7794, -73.9692)
        call_count_after_first = mock_get.call_count

        # Second call should read from cache
        nws.get_nws_forecast(40.7794, -73.9692)
        assert mock_get.call_count == call_count_after_first


def test_get_nws_forecast_stale_fallback_on_error(tmp_path, monkeypatch, caplog):
    """When the HTTP call raises, stale cache is served with a warning log."""
    import src.data.cache as cache_mod
    from src.data.weather import nws

    monkeypatch.setattr(cache_mod, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(nws, "_SESSION", None)

    # Seed the stale cache with expired timestamp (cached_at far in the past)
    import time, json
    cache_params = {"lat": 40.7794, "lon": -73.9692}
    key = cache_mod._cache_key("nws_forecast", cache_params)
    key.parent.mkdir(parents=True, exist_ok=True)
    # Write with a cached_at 7200 seconds in the past so TTL=3600 is exceeded
    key.write_text(json.dumps({
        "cached_at": time.time() - 7200,
        "value": FORECAST_RESPONSE["properties"]["periods"],
    }))

    with patch("requests.Session.get") as mock_get:
        mock_get.side_effect = [
            _mock_response(POINTS_RESPONSE),
            Exception("network error"),
        ]
        with caplog.at_level(logging.WARNING, logger="src.data.weather.nws"):
            result = nws.get_nws_forecast(40.7794, -73.9692)

    assert result is not None
    # Warning was logged about the stale fallback
    assert len(caplog.records) > 0


def test_get_nws_forecast_user_agent_header(tmp_path, monkeypatch):
    """NWS session must include a User-Agent with 'kalshi-bot'."""
    import src.data.cache as cache_mod
    from src.data.weather import nws

    monkeypatch.setattr(cache_mod, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(nws, "_SESSION", None)

    captured_headers = {}

    def fake_get(url, **kwargs):
        captured_headers.update(kwargs.get("headers", {}))
        # Also check session headers via the request object
        return _mock_response(POINTS_RESPONSE)

    with patch("requests.Session.get", side_effect=fake_get):
        try:
            nws.get_nws_forecast(40.7794, -73.9692)
        except Exception:
            pass

    # Verify session headers contain User-Agent with kalshi-bot
    sess = nws._get_session()
    assert "kalshi-bot" in sess.headers.get("User-Agent", "")


# ---------------------------------------------------------------------------
# get_nws_griddata
# ---------------------------------------------------------------------------


def test_get_nws_griddata_returns_temperature_and_precip(tmp_path, monkeypatch):
    import src.data.cache as cache_mod
    from src.data.weather import nws

    monkeypatch.setattr(cache_mod, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(nws, "_SESSION", None)

    with patch("requests.Session.get") as mock_get:
        mock_get.side_effect = [
            _mock_response(POINTS_RESPONSE),
            _mock_response(GRIDDATA_RESPONSE),
        ]
        result = nws.get_nws_griddata(40.7794, -73.9692)

    assert "temperature" in result
    assert "probabilityOfPrecipitation" in result


# ---------------------------------------------------------------------------
# get_nws_hourly
# ---------------------------------------------------------------------------


def test_get_nws_hourly_returns_periods(tmp_path, monkeypatch):
    import src.data.cache as cache_mod
    from src.data.weather import nws

    monkeypatch.setattr(cache_mod, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(nws, "_SESSION", None)

    with patch("requests.Session.get") as mock_get:
        mock_get.side_effect = [
            _mock_response(POINTS_RESPONSE),
            _mock_response(HOURLY_RESPONSE),
        ]
        result = nws.get_nws_hourly(40.7794, -73.9692)

    assert isinstance(result, list)
    assert result[0]["temperature"] == 58


# ---------------------------------------------------------------------------
# compare_forecast_models
# ---------------------------------------------------------------------------


def test_compare_forecast_models_returns_both_forecasts(tmp_path, monkeypatch):
    import src.data.cache as cache_mod
    from src.data.weather import nws

    monkeypatch.setattr(cache_mod, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(nws, "_SESSION", None)

    with patch("requests.Session.get") as mock_get:
        mock_get.side_effect = [
            _mock_response(POINTS_RESPONSE),  # points for forecast
            _mock_response(FORECAST_RESPONSE),
            _mock_response(POINTS_RESPONSE),  # points for hourly (may be cached)
            _mock_response(HOURLY_RESPONSE),
        ]
        result = nws.compare_forecast_models(40.7794, -73.9692)

    assert "period_forecast" in result
    assert "hourly_forecast" in result
    assert isinstance(result["period_forecast"], list)
    assert isinstance(result["hourly_forecast"], list)


# ---------------------------------------------------------------------------
# detect_forecast_delta
# ---------------------------------------------------------------------------


def test_detect_forecast_delta_finds_temperature_change():
    from src.data.weather.nws import detect_forecast_delta

    old_periods = [
        {"name": "Tonight", "temperature": 55},
        {"name": "Monday", "temperature": 70},
    ]
    new_periods = [
        {"name": "Tonight", "temperature": 58},  # changed +3
        {"name": "Monday", "temperature": 70},   # unchanged
    ]
    deltas = detect_forecast_delta(old_periods, new_periods)

    assert len(deltas) == 1
    assert deltas[0]["period"] == "Tonight"
    assert deltas[0]["old_temp"] == 55
    assert deltas[0]["new_temp"] == 58
    assert deltas[0]["delta"] == 3


def test_detect_forecast_delta_no_change_returns_empty():
    from src.data.weather.nws import detect_forecast_delta

    periods = [{"name": "Tonight", "temperature": 55}]
    deltas = detect_forecast_delta(periods, periods)
    assert deltas == []


def test_detect_forecast_delta_multiple_changes():
    from src.data.weather.nws import detect_forecast_delta

    old = [{"name": f"Day{i}", "temperature": 60 + i} for i in range(3)]
    new = [{"name": f"Day{i}", "temperature": 65 + i} for i in range(3)]
    deltas = detect_forecast_delta(old, new)
    assert len(deltas) == 3
    assert all(d["delta"] == 5 for d in deltas)
