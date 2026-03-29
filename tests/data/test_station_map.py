"""Tests for src/data/weather/station_map.py — static station lookup."""

import pytest


def test_knyc_lat_lon():
    from src.data.weather.station_map import STATIONS

    s = STATIONS["KNYC"]
    assert s["lat"] == 40.7794
    assert s["lon"] == -73.9692


def test_knyc_ghcnd_id():
    from src.data.weather.station_map import STATIONS

    assert STATIONS["KNYC"]["ghcnd_id"] == "GHCND:USW00094728"


def test_kmdw_coords_and_ghcnd():
    from src.data.weather.station_map import STATIONS

    s = STATIONS["KMDW"]
    assert s["lat"] == 41.7861
    assert s["lon"] == -87.7522
    assert s["ghcnd_id"] == "GHCND:USW00014819"


def test_kmia_coords_and_ghcnd():
    from src.data.weather.station_map import STATIONS

    s = STATIONS["KMIA"]
    assert s["lat"] == 25.7959
    assert s["lon"] == -80.2870
    assert s["ghcnd_id"] == "GHCND:USW00012839"


def test_kaus_coords_and_ghcnd():
    from src.data.weather.station_map import STATIONS

    s = STATIONS["KAUS"]
    assert s["lat"] == 30.1945
    assert s["lon"] == -97.6699
    assert s["ghcnd_id"] == "GHCND:USW00013904"


def test_ticker_to_station_returns_knyc():
    from src.data.weather.station_map import ticker_to_station

    result = ticker_to_station("KNYC")
    assert result is not None
    assert result["lat"] == 40.7794


def test_ticker_to_station_case_insensitive():
    from src.data.weather.station_map import ticker_to_station

    assert ticker_to_station("knyc") == ticker_to_station("KNYC")


def test_ticker_to_station_unknown_returns_none():
    from src.data.weather.station_map import ticker_to_station

    assert ticker_to_station("ZZZZ") is None


def test_get_station_coords_returns_tuple():
    from src.data.weather.station_map import get_station_coords

    coords = get_station_coords("KNYC")
    assert coords == (40.7794, -73.9692)


def test_get_station_coords_unknown_returns_none():
    from src.data.weather.station_map import get_station_coords

    assert get_station_coords("XXXX") is None


def test_all_four_stations_present():
    from src.data.weather.station_map import STATIONS

    for ticker in ("KNYC", "KMDW", "KMIA", "KAUS"):
        assert ticker in STATIONS, f"{ticker} missing from STATIONS"
