"""Integration tests for live API connectivity.

All tests in this module require ``KALSHI_INTEGRATION=true`` to run.
Without this environment variable, all tests are skipped automatically.

These tests verify live connectivity to:
1. NBA schedule API (nba_api ScoreboardV3)
2. NWS forecast API (api.weather.gov)
3. NBA team stats API (nba_api LeagueDashTeamStats)
4. Ticker-to-station round-trip (KNYC -> lat/lon -> NWS forecast)

Run with:
    KALSHI_INTEGRATION=true python -m pytest tests/data/test_integration.py -v
"""

import logging
import time

import pytest

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Test 1: Live NBA schedule
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_live_nba_schedule(skip_without_integration):
    """Call get_todays_schedule() against live nba_api.

    Returns a list — may be empty on off-days, that's OK.
    Each item must be a dict (at minimum).
    """
    from src.data.nba.teams import get_todays_schedule

    result = get_todays_schedule()
    time.sleep(1)

    assert isinstance(result, list), "Expected a list from get_todays_schedule()"

    logger.info("Live NBA schedule: %d games today", len(result))

    if result:
        first = result[0]
        assert isinstance(first, dict), "Each game should be a dict"
        # Allow either key name format
        has_game_id = "GAME_ID" in first or "GameID" in first or "gameId" in first
        assert has_game_id, f"Game dict missing GAME_ID key — got keys: {list(first.keys())}"
        logger.info("Sample game keys: %s", list(first.keys()))


# ---------------------------------------------------------------------------
# Test 2: Live NWS forecast
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_live_nws_forecast(skip_without_integration):
    """Call get_nws_forecast() for KNYC (Central Park) against live NWS API.

    Must return a non-empty list with temperature and shortForecast fields.
    """
    from src.data.weather.nws import get_nws_forecast

    # KNYC: Central Park, NYC
    result = get_nws_forecast(40.7794, -73.9692)

    assert isinstance(result, list), "Expected a list from get_nws_forecast()"
    assert len(result) > 0, "Expected at least one forecast period from NWS"

    first = result[0]
    assert "temperature" in first, f"First period missing 'temperature' key — got: {list(first.keys())}"
    assert "shortForecast" in first, f"First period missing 'shortForecast' key — got: {list(first.keys())}"

    temp = first["temperature"]
    assert isinstance(temp, (int, float)), f"temperature should be numeric, got {type(temp)}"
    assert -50 < temp < 150, f"temperature {temp} is outside the plausible range (-50, 150)"

    logger.info(
        "Live NWS forecast KNYC: %d periods, first period = %s (%s°F)",
        len(result),
        first.get("name"),
        temp,
    )


# ---------------------------------------------------------------------------
# Test 3: Live NBA team stats
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_live_nba_team_stats(skip_without_integration):
    """Call get_team_stats() against live nba_api.

    Must return 30 teams with TEAM_ID, OFF_RATING, DEF_RATING.
    """
    from src.data.nba.teams import get_team_stats

    result = get_team_stats()

    assert isinstance(result, list), "Expected a list from get_team_stats()"
    assert len(result) == 30, f"Expected 30 NBA teams, got {len(result)}"

    for team in result:
        assert "TEAM_ID" in team, f"Team dict missing TEAM_ID — got: {list(team.keys())}"
        assert "OFF_RATING" in team, f"Team dict missing OFF_RATING — got: {list(team.keys())}"
        assert "DEF_RATING" in team, f"Team dict missing DEF_RATING — got: {list(team.keys())}"

    logger.info(
        "Live NBA team stats: %d teams returned, sample = %s",
        len(result),
        result[0].get("TEAM_NAME") if result else "n/a",
    )


# ---------------------------------------------------------------------------
# Test 4: Ticker-to-station round-trip (KNYC end-to-end)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_live_ticker_to_station(skip_without_integration):
    """KNYC ticker -> lat/lon -> NWS forecast round-trip.

    This is the Phase 2 working test: verify we can map a Kalshi weather ticker
    (KNYC) to a lat/lon coordinate and then retrieve a live NWS forecast for it.

    Steps:
    1. ticker_to_station("KNYC") -> station dict with lat/lon
    2. get_nws_forecast(lat, lon) -> non-empty list of forecast periods
    3. First period has 'temperature' key
    """
    from src.data.weather.nws import get_nws_forecast
    from src.data.weather.station_map import ticker_to_station

    # Step 1: ticker lookup
    station = ticker_to_station("KNYC")
    assert station is not None, "ticker_to_station('KNYC') returned None"
    assert isinstance(station, dict), "ticker_to_station should return a dict"
    assert "lat" in station, f"Station dict missing 'lat' key — got: {list(station.keys())}"
    assert "lon" in station, f"Station dict missing 'lon' key — got: {list(station.keys())}"

    lat = station["lat"]
    lon = station["lon"]
    logger.info("KNYC station: lat=%s, lon=%s", lat, lon)

    # Step 2: live NWS forecast call
    result = get_nws_forecast(lat, lon)
    time.sleep(1)

    assert isinstance(result, list), "get_nws_forecast should return a list"
    assert len(result) > 0, "Expected at least one forecast period from NWS for KNYC"

    # Step 3: validate period structure
    first = result[0]
    assert "temperature" in first, (
        f"First NWS forecast period missing 'temperature' key — got: {list(first.keys())}"
    )

    logger.info(
        "KNYC round-trip complete: %d forecast periods, current = %s°F (%s)",
        len(result),
        first.get("temperature"),
        first.get("shortForecast", "n/a"),
    )
