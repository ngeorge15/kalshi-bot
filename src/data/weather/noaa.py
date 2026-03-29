"""NOAA CDO (Climate Data Online) historical daily weather data client.

Fetches up to 3 years of TMAX/TMIN/PRCP records per station from the NOAA
CDO API and persists them to the ``noaa_daily_weather`` SQLite table.
Subsequent calls read from the database (pull-once strategy, D-09).

NOAA CDO API pattern::

    GET https://www.ncei.noaa.gov/cdo-web/api/v2/data
    Headers: token: <NOAA_API_TOKEN>
    Params:
        datasetid=GHCND
        stationid=GHCND:USW00094728
        datatypeid=TMAX,TMIN,PRCP
        startdate=YYYY-MM-DD
        enddate=YYYY-MM-DD
        limit=1000
        offset=1
        units=standard

    TMAX/TMIN are returned in tenths of a degree — divide by 10 for Fahrenheit.
    PRCP is returned in inches — store as-is.

Usage::

    from src.data.weather.noaa import fetch_noaa_historical, get_noaa_historical
    from src.db.database import Database

    db = Database()
    rows_written = fetch_noaa_historical("KNYC", db)
    history = get_noaa_historical("KNYC", db, start_date="2025-01-01")
"""

import logging
import os
from datetime import date, timedelta

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from src.data.cache import cache_get, cache_get_stale, cache_set
from src.data.weather.station_map import STATIONS
from src.db.database import Database

logger = logging.getLogger(__name__)

NOAA_BASE = "https://www.ncei.noaa.gov/cdo-web/api/v2"


def _get_noaa_token() -> str:
    """Return the NOAA CDO API token from environment.

    Returns:
        Token string.

    Raises:
        RuntimeError: If ``NOAA_API_TOKEN`` is not set, with instructions for
            obtaining a free token.
    """
    token = os.environ.get("NOAA_API_TOKEN")
    if not token:
        raise RuntimeError(
            "NOAA_API_TOKEN not set. "
            "Get a free token at https://www.ncdc.noaa.gov/cdo-web/token"
        )
    return token


def _get_session() -> requests.Session:
    """Build a requests.Session configured for the NOAA CDO API.

    Sets the ``token`` header required by NOAA and mounts an HTTPAdapter
    with retry logic for transient failures.

    Returns:
        Configured ``requests.Session``.
    """
    token = _get_noaa_token()
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
            "token": token,
            "Accept": "application/json",
        }
    )
    return session


def _fetch_noaa_page(
    session: requests.Session,
    station_ghcnd: str,
    start_date: str,
    end_date: str,
    offset: int = 1,
) -> dict:
    """Fetch one page of NOAA CDO daily data.

    Args:
        session: Configured requests.Session with token header set.
        station_ghcnd: Full GHCND station identifier (e.g. ``"GHCND:USW00094728"``).
        start_date: Start date in ``YYYY-MM-DD`` format.
        end_date: End date in ``YYYY-MM-DD`` format.
        offset: Pagination offset (1-based).

    Returns:
        Full JSON response dict with ``metadata`` and ``results`` keys.
    """
    params = {
        "datasetid": "GHCND",
        "stationid": station_ghcnd,
        "datatypeid": "TMAX,TMIN,PRCP",
        "startdate": start_date,
        "enddate": end_date,
        "limit": 1000,
        "offset": offset,
        "units": "standard",
    }
    resp = session.get(f"{NOAA_BASE}/data", params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def fetch_noaa_historical(
    station_code: str,
    db: "Database | None" = None,
    years: int = 3,
) -> int:
    """Fetch historical daily NOAA weather data and persist it to SQLite.

    Implements pull-once strategy (D-09): checks cache first; on cache hit,
    returns 0 immediately.  Otherwise fetches all pages, groups by date,
    applies TMAX/TMIN ÷ 10 conversion, and inserts into ``noaa_daily_weather``
    with ``INSERT OR IGNORE`` (deduplication).

    On HTTP failure, serves stale cache and logs a warning (D-04).

    Args:
        station_code: Kalshi station ticker (e.g. ``"KNYC"``).
        db: Database instance to write to.  If ``None``, a new default
            ``Database()`` is created.
        years: Number of years of history to fetch (default 3, per D-10).

    Returns:
        Number of newly inserted rows (0 if cache hit or all rows already
        existed).

    Raises:
        RuntimeError: If ``NOAA_API_TOKEN`` is not set.
        KeyError: If ``station_code`` is not found in STATIONS.
    """
    station = STATIONS[station_code.upper()]
    ghcnd_id = station["ghcnd_id"]

    end_date = date.today()
    start_date = end_date - timedelta(days=years * 365)
    end_str = end_date.isoformat()
    start_str = start_date.isoformat()

    cache_params = {
        "type": "noaa",
        "station": station_code.upper(),
        "start": start_str,
        "end": end_str,
    }

    # Pull-once: cache hit means we already fetched and stored this batch
    cached = cache_get("historical", cache_params)
    if cached is not None:
        logger.debug("NOAA historical cache hit for %s, skipping fetch", station_code)
        return 0

    _db = db if db is not None else Database()

    # Validate token before attempting HTTP — raises RuntimeError on missing token
    _get_noaa_token()

    try:
        session = _get_session()
        all_results: list[dict] = []

        offset = 1
        while True:
            resp_data = _fetch_noaa_page(session, ghcnd_id, start_str, end_str, offset)
            results = resp_data.get("results", [])
            all_results.extend(results)

            total_count = resp_data["metadata"]["resultset"]["count"]
            if offset + 1000 > total_count:
                break
            offset += 1000

        # Group by date
        by_date: dict[str, dict] = {}
        for rec in all_results:
            # Date can be "2026-01-01T00:00:00" — take the date part
            rec_date = rec["date"][:10]
            if rec_date not in by_date:
                by_date[rec_date] = {"tmax": None, "tmin": None, "prcp": None}
            dtype = rec["datatype"]
            val = rec["value"]
            if dtype == "TMAX":
                # TMAX/TMIN returned in tenths of degree — divide by 10
                by_date[rec_date]["tmax"] = val / 10
            elif dtype == "TMIN":
                by_date[rec_date]["tmin"] = val / 10
            elif dtype == "PRCP":
                # PRCP in inches — store as-is
                by_date[rec_date]["prcp"] = val

        rows_inserted = 0
        for rec_date, vals in by_date.items():
            cursor = _db.execute(
                "INSERT OR IGNORE INTO noaa_daily_weather "
                "(station_id, station_code, date, tmax_f, tmin_f, prcp_in) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (ghcnd_id, station_code.upper(), rec_date, vals["tmax"], vals["tmin"], vals["prcp"]),
            )
            rows_inserted += cursor.rowcount

        cache_set("historical", cache_params, rows_inserted)
        logger.info(
            "NOAA historical: inserted %d rows for %s (%s to %s)",
            rows_inserted,
            station_code,
            start_str,
            end_str,
        )
        return rows_inserted

    except Exception as exc:
        logger.warning(
            "NOAA historical fetch failed for %s (%s), serving stale cache",
            station_code,
            exc,
        )
        stale = cache_get_stale("historical", cache_params)
        if stale is not None:
            logger.warning("Returning stale NOAA cache for %s", station_code)
            return 0
        logger.warning("No stale NOAA cache available for %s", station_code)
        return 0


def get_noaa_historical(
    station_code: str,
    db: "Database | None" = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[dict]:
    """Query stored NOAA historical data from the database.

    Args:
        station_code: Kalshi station ticker (e.g. ``"KNYC"``).
        db: Database instance.  If ``None``, a new default ``Database()`` is
            created.
        start_date: Optional ISO date string for range filter (inclusive).
        end_date: Optional ISO date string for range filter (inclusive).

    Returns:
        List of row dicts with columns ``id``, ``station_id``, ``station_code``,
        ``date``, ``tmax_f``, ``tmin_f``, ``prcp_in``, ``created_at``.
    """
    _db = db if db is not None else Database()
    station = STATIONS.get(station_code.upper())
    if station is None:
        logger.warning("Unknown station code: %s", station_code)
        return []

    sql = "SELECT * FROM noaa_daily_weather WHERE station_code = ?"
    params: list = [station_code.upper()]

    if start_date is not None:
        sql += " AND date >= ?"
        params.append(start_date)
    if end_date is not None:
        sql += " AND date <= ?"
        params.append(end_date)

    sql += " ORDER BY date ASC"
    return _db.fetchall(sql, tuple(params))
