"""Static station mapping: Kalshi weather tickers to NWS/NOAA station data.

Provides the authoritative lat/lon coordinates and GHCND station IDs for the
four Kalshi weather station tickers used in temperature and precipitation
markets.

Usage::

    from src.data.weather.station_map import STATIONS, ticker_to_station, get_station_coords

    # Direct lookup
    knyc = STATIONS["KNYC"]
    lat, lon = knyc["lat"], knyc["lon"]

    # Helper wrappers
    station = ticker_to_station("KNYC")
    coords = get_station_coords("KNYC")  # (40.7794, -73.9692)
"""

STATIONS: dict[str, dict] = {
    "KNYC": {
        "city": "NYC",
        "name": "Central Park",
        "lat": 40.7794,
        "lon": -73.9692,
        "ghcnd_id": "GHCND:USW00094728",
        "nws_office": "OKX",
    },
    "KMDW": {
        "city": "CHI",
        "name": "Chicago Midway",
        "lat": 41.7861,
        "lon": -87.7522,
        "ghcnd_id": "GHCND:USW00014819",
        "nws_office": "LOT",
    },
    "KMIA": {
        "city": "MIA",
        "name": "Miami International",
        "lat": 25.7959,
        "lon": -80.2870,
        "ghcnd_id": "GHCND:USW00012839",
        "nws_office": "MFL",
    },
    "KAUS": {
        "city": "AUS",
        "name": "Austin Bergstrom",
        "lat": 30.1945,
        "lon": -97.6699,
        "ghcnd_id": "GHCND:USW00013904",
        "nws_office": "EWX",
    },
}


def ticker_to_station(ticker_prefix: str) -> dict | None:
    """Return the station dict for a Kalshi weather ticker prefix.

    Args:
        ticker_prefix: Kalshi station ticker (e.g. ``"KNYC"``). Case-insensitive.

    Returns:
        Station dict with lat, lon, ghcnd_id, nws_office, city, name, or
        ``None`` if the ticker is not known.
    """
    return STATIONS.get(ticker_prefix.upper())


def get_station_coords(station_code: str) -> tuple[float, float] | None:
    """Return ``(lat, lon)`` for a station code, or ``None`` if not found.

    Args:
        station_code: Station ticker (e.g. ``"KNYC"``). Case-insensitive.

    Returns:
        ``(lat, lon)`` tuple, or ``None`` if the station is not in STATIONS.
    """
    s = STATIONS.get(station_code.upper())
    return (s["lat"], s["lon"]) if s else None
