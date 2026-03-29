"""NWS (National Weather Service) forecast and gridpoint data client.

Fetches probabilistic forecasts from the NWS API using the two-step
/points -> /gridpoints pattern.  All HTTP calls are cached and serve stale
data on failure (D-04).

API flow::

    Step 1: GET /points/{lat},{lon}
        -> properties.gridId, gridX, gridY, forecast, forecastHourly, forecastGridData

    Step 2a: GET /gridpoints/{office}/{gridX},{gridY}/forecast
        -> properties.periods (12-hour windows)

    Step 2b: GET /gridpoints/{office}/{gridX},{gridY}/forecast/hourly
        -> properties.periods (1-hour windows)

    Step 2c: GET /gridpoints/{office}/{gridX},{gridY}
        -> properties (temperature, probabilityOfPrecipitation time series)

Usage::

    from src.data.weather.nws import get_nws_forecast, get_nws_griddata

    periods = get_nws_forecast(40.7794, -73.9692)
    griddata = get_nws_griddata(40.7794, -73.9692)
"""

import logging
import os
import re
from datetime import datetime, timedelta

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from src.data.cache import cache_get, cache_get_stale, cache_set

logger = logging.getLogger(__name__)

NWS_BASE = "https://api.weather.gov"
NWS_CONTACT_EMAIL = os.environ.get("NWS_CONTACT_EMAIL", "kalshi-bot@example.com")

# Module-level session singleton — built once and reused.
_SESSION: requests.Session | None = None


def _get_session() -> requests.Session:
    """Return the module-level requests.Session (built once).

    The session is configured with:
    - ``User-Agent: (kalshi-bot, {NWS_CONTACT_EMAIL})`` — required by NWS API ToS
    - HTTPAdapter retry on 429 / 5xx with backoff
    - 10-second timeout applied per-call

    Returns:
        Configured ``requests.Session``.
    """
    global _SESSION
    if _SESSION is None:
        session = requests.Session()

        retry = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)

        session.headers.update(
            {
                "User-Agent": f"(kalshi-bot, {NWS_CONTACT_EMAIL})",
                "Accept": "application/geo+json",
            }
        )
        _SESSION = session
    return _SESSION


def _parse_valid_time(valid_time: str) -> tuple[datetime, timedelta]:
    """Parse an NWS ``validTime`` string into a (start_datetime, duration) pair.

    NWS ``validTime`` format::

        "2026-03-29T18:00:00+00:00/PT6H"

    The string is split on ``/`` to separate the ISO 8601 timestamp from the
    ISO 8601 duration.  Only ``PTnH`` (hour-based) durations are supported.

    Args:
        valid_time: NWS validTime string (e.g. ``"2026-03-29T18:00:00+00:00/PT1H"``).

    Returns:
        ``(start_datetime, duration_timedelta)`` tuple.

    Raises:
        ValueError: If the duration cannot be parsed.
    """
    start_str, duration_str = valid_time.split("/")
    start_dt = datetime.fromisoformat(start_str)

    match = re.search(r"PT(\d+)H", duration_str)
    if not match:
        raise ValueError(f"Cannot parse duration: {duration_str!r}")
    hours = int(match.group(1))
    return start_dt, timedelta(hours=hours)


def _resolve_gridpoint(lat: float, lon: float) -> dict:
    """Resolve NWS gridpoint metadata for a lat/lon coordinate pair.

    Calls ``GET /points/{lat:.4f},{lon:.4f}`` and caches the result for 24 h
    (grid mappings do not change).

    Args:
        lat: Latitude (decimal degrees).
        lon: Longitude (decimal degrees, negative west).

    Returns:
        Dict with keys: ``office``, ``gridX``, ``gridY``, ``forecast_url``,
        ``forecast_hourly_url``, ``forecast_grid_url``.
    """
    cache_params = {"lat": round(lat, 4), "lon": round(lon, 4)}
    cached = cache_get("nws_gridpoint", cache_params, ttl_seconds=86400)
    if cached is not None:
        return cached

    url = f"{NWS_BASE}/points/{lat:.4f},{lon:.4f}"
    session = _get_session()
    resp = session.get(url, timeout=10)
    resp.raise_for_status()
    props = resp.json()["properties"]

    result = {
        "office": props["gridId"],
        "gridX": props["gridX"],
        "gridY": props["gridY"],
        "forecast_url": props["forecast"],
        "forecast_hourly_url": props["forecastHourly"],
        "forecast_grid_url": props["forecastGridData"],
    }
    cache_set("nws_gridpoint", cache_params, result)
    return result


def get_nws_forecast(lat: float, lon: float) -> list[dict]:
    """Fetch NWS 12-hour period forecast for a lat/lon location.

    Serves stale cached data and logs a warning if the API call fails (D-04).

    Args:
        lat: Latitude (decimal degrees).
        lon: Longitude (decimal degrees, negative west).

    Returns:
        List of period dicts, each with ``temperature``, ``shortForecast``,
        ``name``, ``startTime``, etc.  Empty list if all sources fail.
    """
    cache_params = {"lat": round(lat, 4), "lon": round(lon, 4)}
    cached = cache_get("nws_forecast", cache_params, ttl_seconds=3600)
    if cached is not None:
        return cached

    try:
        gridpoint = _resolve_gridpoint(lat, lon)
        session = _get_session()
        resp = session.get(gridpoint["forecast_url"], timeout=10)
        resp.raise_for_status()
        periods = resp.json()["properties"]["periods"]
        cache_set("nws_forecast", cache_params, periods)
        return periods
    except Exception as exc:
        logger.warning("NWS forecast API failed (%s), serving stale cache", exc)
        stale = cache_get_stale("nws_forecast", cache_params)
        if stale is not None:
            return stale
        logger.warning("No stale NWS forecast cache available for lat=%s lon=%s", lat, lon)
        return []


def get_nws_hourly(lat: float, lon: float) -> list[dict]:
    """Fetch NWS hourly forecast for a lat/lon location.

    Serves stale cached data and logs a warning if the API call fails (D-04).

    Args:
        lat: Latitude (decimal degrees).
        lon: Longitude (decimal degrees, negative west).

    Returns:
        List of hourly period dicts.  Empty list if all sources fail.
    """
    cache_params = {"lat": round(lat, 4), "lon": round(lon, 4), "type": "hourly"}
    cached = cache_get("nws_hourly", cache_params, ttl_seconds=3600)
    if cached is not None:
        return cached

    try:
        gridpoint = _resolve_gridpoint(lat, lon)
        session = _get_session()
        resp = session.get(gridpoint["forecast_hourly_url"], timeout=10)
        resp.raise_for_status()
        periods = resp.json()["properties"]["periods"]
        cache_set("nws_hourly", cache_params, periods)
        return periods
    except Exception as exc:
        logger.warning("NWS hourly API failed (%s), serving stale cache", exc)
        stale = cache_get_stale("nws_hourly", cache_params)
        if stale is not None:
            return stale
        logger.warning("No stale NWS hourly cache available for lat=%s lon=%s", lat, lon)
        return []


def get_nws_griddata(lat: float, lon: float) -> dict:
    """Fetch NWS raw griddata (temperature + precipitation time series).

    Returns the full ``properties`` dict from the griddata endpoint, which
    contains time-series data for temperature, maxTemperature, minTemperature,
    probabilityOfPrecipitation, and other forecast elements.

    Serves stale cached data and logs a warning if the API call fails (D-04).

    Args:
        lat: Latitude (decimal degrees).
        lon: Longitude (decimal degrees, negative west).

    Returns:
        ``properties`` dict with ``temperature``, ``probabilityOfPrecipitation``
        keys.  Empty dict if all sources fail.
    """
    cache_params = {"lat": round(lat, 4), "lon": round(lon, 4), "type": "griddata"}
    cached = cache_get("nws_griddata", cache_params, ttl_seconds=3600)
    if cached is not None:
        return cached

    try:
        gridpoint = _resolve_gridpoint(lat, lon)
        session = _get_session()
        resp = session.get(gridpoint["forecast_grid_url"], timeout=10)
        resp.raise_for_status()
        props = resp.json()["properties"]
        cache_set("nws_griddata", cache_params, props)
        return props
    except Exception as exc:
        logger.warning("NWS griddata API failed (%s), serving stale cache", exc)
        stale = cache_get_stale("nws_griddata", cache_params)
        if stale is not None:
            return stale
        logger.warning("No stale NWS griddata cache available for lat=%s lon=%s", lat, lon)
        return {}


def compare_forecast_models(lat: float, lon: float) -> dict:
    """Compare 12-hour period forecast with hourly forecast for divergence analysis.

    Per R3.5: returns both period and hourly forecasts.  Divergence between the
    12-hour temperature and hourly average for the same window signals forecast
    uncertainty, which is useful input for confidence calibration.

    Args:
        lat: Latitude (decimal degrees).
        lon: Longitude (decimal degrees, negative west).

    Returns:
        Dict with ``period_forecast`` and ``hourly_forecast`` keys, each
        containing the respective period lists from NWS.
    """
    period_forecast = get_nws_forecast(lat, lon)
    hourly_forecast = get_nws_hourly(lat, lon)
    return {
        "period_forecast": period_forecast,
        "hourly_forecast": hourly_forecast,
    }


def detect_forecast_delta(
    old_periods: list[dict], new_periods: list[dict]
) -> list[dict]:
    """Identify temperature changes between two sets of NWS forecast periods.

    Per R3.6: compares periods by name and returns a list of change records for
    any period where the temperature differs.  Used to detect when NWS has
    updated its forecast, which may signal a trading opportunity.

    Args:
        old_periods: Previous forecast period list (each period has ``name`` and
            ``temperature`` keys).
        new_periods: Updated forecast period list.

    Returns:
        List of change dicts: ``{"period": name, "old_temp": T1, "new_temp": T2,
        "delta": T2-T1}`` for each period where temperature changed.
    """
    old_by_name = {p["name"]: p["temperature"] for p in old_periods if "name" in p}
    deltas = []
    for period in new_periods:
        name = period.get("name")
        new_temp = period.get("temperature")
        if name is None or new_temp is None:
            continue
        old_temp = old_by_name.get(name)
        if old_temp is not None and old_temp != new_temp:
            deltas.append(
                {
                    "period": name,
                    "old_temp": old_temp,
                    "new_temp": new_temp,
                    "delta": new_temp - old_temp,
                }
            )
    return deltas
