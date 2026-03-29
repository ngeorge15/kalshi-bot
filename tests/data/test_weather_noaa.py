"""Tests for src/data/weather/noaa.py — NOAA historical daily data to SQLite."""

import json
import logging
import time
from datetime import date
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_response(json_data: dict, status_code: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.raise_for_status = MagicMock()
    return resp


def _noaa_page(results: list, count: int | None = None) -> dict:
    """Build a NOAA CDO API response page."""
    return {
        "metadata": {
            "resultset": {
                "offset": 1,
                "count": count if count is not None else len(results),
                "limit": 1000,
            }
        },
        "results": results,
    }


def _daily_records(date_str: str, tmax: int = 720, tmin: int = 650, prcp: float = 0.12) -> list:
    """Return a list of NOAA CDO records for a single date."""
    return [
        {"date": f"{date_str}T00:00:00", "datatype": "TMAX", "station": "GHCND:USW00094728", "value": tmax},
        {"date": f"{date_str}T00:00:00", "datatype": "TMIN", "station": "GHCND:USW00094728", "value": tmin},
        {"date": f"{date_str}T00:00:00", "datatype": "PRCP", "station": "GHCND:USW00094728", "value": prcp},
    ]


# ---------------------------------------------------------------------------
# In-memory database fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    """Provide a fresh in-memory-path SQLite Database for each test."""
    from src.db.database import Database

    db_file = str(tmp_path / "test.db")
    return Database(db_path=db_file)


@pytest.fixture
def noaa_env(monkeypatch):
    """Set NOAA_API_TOKEN in environment."""
    monkeypatch.setenv("NOAA_API_TOKEN", "test-token-123")


# ---------------------------------------------------------------------------
# Test: NOAA token error
# ---------------------------------------------------------------------------


def test_fetch_noaa_historical_missing_token_raises(db, monkeypatch):
    """RuntimeError raised with instructions when NOAA_API_TOKEN is not set."""
    monkeypatch.delenv("NOAA_API_TOKEN", raising=False)

    from src.data.weather import noaa

    with pytest.raises(RuntimeError, match="NOAA_API_TOKEN"):
        noaa.fetch_noaa_historical("KNYC", db)


# ---------------------------------------------------------------------------
# Test: basic write with TMAX/TMIN division
# ---------------------------------------------------------------------------


def test_fetch_noaa_historical_writes_records(db, noaa_env, tmp_path, monkeypatch):
    """fetch_noaa_historical writes records to noaa_daily_weather table."""
    import src.data.cache as cache_mod
    from src.data.weather import noaa

    monkeypatch.setattr(cache_mod, "CACHE_DIR", tmp_path / "cache")

    records = _daily_records("2026-01-01", tmax=720, tmin=650, prcp=0.12)
    page = _noaa_page(records, count=3)

    with patch("requests.Session.get", return_value=_mock_response(page)):
        count = noaa.fetch_noaa_historical("KNYC", db)

    assert count > 0

    rows = db.fetchall("SELECT * FROM noaa_daily_weather WHERE station_code = 'KNYC'")
    assert len(rows) > 0


def test_fetch_noaa_historical_tmax_tmin_divided_by_10(db, noaa_env, tmp_path, monkeypatch):
    """TMAX=720 should be stored as 72.0, TMIN=650 as 65.0."""
    import src.data.cache as cache_mod
    from src.data.weather import noaa

    monkeypatch.setattr(cache_mod, "CACHE_DIR", tmp_path / "cache")

    records = _daily_records("2026-01-01", tmax=720, tmin=650, prcp=0.0)
    page = _noaa_page(records, count=3)

    with patch("requests.Session.get", return_value=_mock_response(page)):
        noaa.fetch_noaa_historical("KNYC", db)

    row = db.fetchone(
        "SELECT tmax_f, tmin_f FROM noaa_daily_weather WHERE station_code = 'KNYC' AND date = '2026-01-01'"
    )
    assert row is not None
    assert row["tmax_f"] == pytest.approx(72.0)
    assert row["tmin_f"] == pytest.approx(65.0)


def test_fetch_noaa_historical_prcp_stored_as_is(db, noaa_env, tmp_path, monkeypatch):
    """PRCP should be stored as-is (not divided)."""
    import src.data.cache as cache_mod
    from src.data.weather import noaa

    monkeypatch.setattr(cache_mod, "CACHE_DIR", tmp_path / "cache")

    records = _daily_records("2026-01-02", tmax=700, tmin=600, prcp=0.25)
    page = _noaa_page(records, count=3)

    with patch("requests.Session.get", return_value=_mock_response(page)):
        noaa.fetch_noaa_historical("KNYC", db)

    row = db.fetchone(
        "SELECT prcp_in FROM noaa_daily_weather WHERE station_code = 'KNYC' AND date = '2026-01-02'"
    )
    assert row is not None
    assert row["prcp_in"] == pytest.approx(0.25)


# ---------------------------------------------------------------------------
# Test: pagination
# ---------------------------------------------------------------------------


def test_fetch_noaa_historical_paginates(db, noaa_env, tmp_path, monkeypatch):
    """When count > 1000 the client fetches subsequent pages with offset."""
    import src.data.cache as cache_mod
    from src.data.weather import noaa

    monkeypatch.setattr(cache_mod, "CACHE_DIR", tmp_path / "cache")

    # Page 1: 1000 total records signalled, but we only provide 1 for simplicity
    page1 = {
        "metadata": {"resultset": {"offset": 1, "count": 1002, "limit": 1000}},
        "results": _daily_records("2026-01-01"),
    }
    # Page 2: remaining records
    page2 = {
        "metadata": {"resultset": {"offset": 1001, "count": 1002, "limit": 1000}},
        "results": _daily_records("2026-01-02"),
    }

    with patch("requests.Session.get") as mock_get:
        mock_get.side_effect = [_mock_response(page1), _mock_response(page2)]
        noaa.fetch_noaa_historical("KNYC", db)

    # Two HTTP calls were made (two pages)
    assert mock_get.call_count == 2


# ---------------------------------------------------------------------------
# Test: deduplication
# ---------------------------------------------------------------------------


def test_fetch_noaa_historical_deduplication(db, noaa_env, tmp_path, monkeypatch):
    """Calling twice with same date does not create duplicate rows."""
    import src.data.cache as cache_mod
    from src.data.weather import noaa

    monkeypatch.setattr(cache_mod, "CACHE_DIR", tmp_path / "cache")

    records = _daily_records("2026-01-03")
    page = _noaa_page(records, count=3)

    with patch("requests.Session.get", return_value=_mock_response(page)):
        noaa.fetch_noaa_historical("KNYC", db)

    # Force re-fetch by clearing cache
    import src.data.cache as cache_mod2
    for f in (tmp_path / "cache").rglob("*.json"):
        f.unlink()

    with patch("requests.Session.get", return_value=_mock_response(page)):
        noaa.fetch_noaa_historical("KNYC", db)

    rows = db.fetchall(
        "SELECT * FROM noaa_daily_weather WHERE station_code = 'KNYC' AND date = '2026-01-03'"
    )
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# Test: get_noaa_historical
# ---------------------------------------------------------------------------


def test_get_noaa_historical_returns_rows(db, noaa_env, tmp_path, monkeypatch):
    """get_noaa_historical returns stored records with expected columns."""
    import src.data.cache as cache_mod
    from src.data.weather import noaa

    monkeypatch.setattr(cache_mod, "CACHE_DIR", tmp_path / "cache")

    records = _daily_records("2026-01-04", tmax=800, tmin=700, prcp=0.0)
    page = _noaa_page(records, count=3)

    with patch("requests.Session.get", return_value=_mock_response(page)):
        noaa.fetch_noaa_historical("KNYC", db)

    rows = noaa.get_noaa_historical("KNYC", db)
    assert len(rows) > 0
    assert "tmax_f" in rows[0]
    assert "tmin_f" in rows[0]
    assert "prcp_in" in rows[0]


def test_get_noaa_historical_date_filter(db, noaa_env, tmp_path, monkeypatch):
    """get_noaa_historical respects start_date and end_date filters."""
    import src.data.cache as cache_mod
    from src.data.weather import noaa

    monkeypatch.setattr(cache_mod, "CACHE_DIR", tmp_path / "cache")

    # Insert two dates manually
    db.execute(
        "INSERT OR IGNORE INTO noaa_daily_weather (station_id, station_code, date, tmax_f, tmin_f, prcp_in) VALUES (?,?,?,?,?,?)",
        ("GHCND:USW00094728", "KNYC", "2026-01-10", 70.0, 55.0, 0.0),
    )
    db.execute(
        "INSERT OR IGNORE INTO noaa_daily_weather (station_id, station_code, date, tmax_f, tmin_f, prcp_in) VALUES (?,?,?,?,?,?)",
        ("GHCND:USW00094728", "KNYC", "2026-02-10", 65.0, 50.0, 0.1),
    )

    rows = noaa.get_noaa_historical("KNYC", db, start_date="2026-01-10", end_date="2026-01-31")
    assert len(rows) == 1
    assert rows[0]["date"] == "2026-01-10"


# ---------------------------------------------------------------------------
# Test: stale cache fallback on API failure
# ---------------------------------------------------------------------------


def test_fetch_noaa_historical_stale_fallback_on_error(db, noaa_env, tmp_path, monkeypatch, caplog):
    """API failure serves stale cache with warning log (D-04)."""
    import src.data.cache as cache_mod
    from src.data.weather import noaa
    from src.data.weather.station_map import STATIONS

    monkeypatch.setattr(cache_mod, "CACHE_DIR", tmp_path / "cache")

    today = date.today().isoformat()
    start_date = str(date.today().replace(year=date.today().year - 3))
    station = STATIONS["KNYC"]

    # Seed an expired stale cache entry
    cache_params = {
        "type": "noaa",
        "station": "KNYC",
        "start": start_date,
        "end": today,
    }
    # Re-build dates as noaa module would compute them
    from datetime import timedelta
    end_d = date.today()
    start_d = end_d - timedelta(days=3 * 365)
    cache_params2 = {
        "type": "noaa",
        "station": "KNYC",
        "start": start_d.isoformat(),
        "end": end_d.isoformat(),
    }

    key = cache_mod._cache_key("historical", cache_params2)
    key.parent.mkdir(parents=True, exist_ok=True)
    # Write expired stale entry (7200s ago, ttl is 604800 so this is still fresh —
    # but we test that when HTTP fails, stale is used even on first call)
    # Actually, let's write it fresh so cache_get returns it on the first "fetch" attempt
    # but then simulate the code path where no cache exists and HTTP fails.
    # Better: just test that when HTTP raises, warning is logged.
    # Seed NO cache and verify warning is issued.
    key.write_text(json.dumps({
        "cached_at": time.time() - 700000,  # expired (>604800s)
        "value": 1,  # pretend 1 row was previously written
    }))

    with patch("requests.Session.get", side_effect=Exception("connection timeout")):
        with caplog.at_level(logging.WARNING, logger="src.data.weather.noaa"):
            result = noaa.fetch_noaa_historical("KNYC", db)

    # Should return 0 (stale fallback returns 0 new rows)
    assert result == 0
    # Warning was logged
    assert len(caplog.records) > 0


# ---------------------------------------------------------------------------
# Test: 3-year date range (D-10)
# ---------------------------------------------------------------------------


def test_fetch_noaa_historical_uses_3_year_range(db, noaa_env, tmp_path, monkeypatch):
    """fetch_noaa_historical fetches 3 years of data (years=3, D-10)."""
    import src.data.cache as cache_mod
    from src.data.weather import noaa

    monkeypatch.setattr(cache_mod, "CACHE_DIR", tmp_path / "cache")

    captured_params = {}

    def fake_get(url, **kwargs):
        captured_params.update(kwargs.get("params", {}))
        return _mock_response(_noaa_page([], count=0))

    with patch("requests.Session.get", side_effect=fake_get):
        noaa.fetch_noaa_historical("KNYC", db)

    assert "startdate" in captured_params
    start = date.fromisoformat(captured_params["startdate"])
    end = date.fromisoformat(captured_params["enddate"])
    diff = (end - start).days
    # Should be approximately 3 years (1095 days ± a day)
    assert 1090 <= diff <= 1100
