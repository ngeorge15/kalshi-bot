"""Tests for src/data/weather/observations.py -- IEM ASOS intraday observed temperature."""

import json
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from src.data.weather import observations


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_response(text, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.raise_for_status = MagicMock()
    return resp


def _csv(rows):
    """Build an IEM-style CSV body from (valid, tmpf_str) pairs."""
    lines = ["station,valid,tmpf"]
    for valid, tmpf in rows:
        lines.append(f"KNYC,{valid},{tmpf}")
    return "\n".join(lines) + "\n"


HTML_ERROR_PAGE = "<html><head><title>503 Service Unavailable</title></head><body>Service Unavailable</body></html>"


@pytest.fixture(autouse=True)
def isolate_cache_dir(tmp_path, monkeypatch):
    """Redirect the shared cache module to a fresh tmp directory for every test."""
    import src.data.cache as cache_mod
    monkeypatch.setattr(cache_mod, "CACHE_DIR", tmp_path / "cache")


# ---------------------------------------------------------------------------
# _parse_asos_csv
# ---------------------------------------------------------------------------

def test_parse_asos_csv_parses_rows():
    text = _csv([("2025-03-01 00:53", "40.00"), ("2025-03-01 01:53", "41.00")])
    rows = observations._parse_asos_csv(text)
    assert rows == [
        {"valid_utc": "2025-03-01T00:53:00Z", "temp_f": 40.0},
        {"valid_utc": "2025-03-01T01:53:00Z", "temp_f": 41.0},
    ]


def test_parse_asos_csv_drops_missing_temp():
    text = _csv([("2025-03-01 00:53", ""), ("2025-03-01 01:53", "41.00")])
    rows = observations._parse_asos_csv(text)
    assert rows == [{"valid_utc": "2025-03-01T01:53:00Z", "temp_f": 41.0}]


def test_parse_asos_csv_drops_m_sentinel_temp():
    text = _csv([("2025-03-01 00:53", "M"), ("2025-03-01 01:53", "41.00")])
    rows = observations._parse_asos_csv(text)
    assert rows == [{"valid_utc": "2025-03-01T01:53:00Z", "temp_f": 41.0}]


def test_parse_asos_csv_header_only_is_empty_not_an_error():
    """An unknown station or a range with zero rows still returns a valid CSV header."""
    rows = observations._parse_asos_csv("station,valid,tmpf\n")
    assert rows == []


def test_parse_asos_csv_html_error_page_raises():
    with pytest.raises(ValueError):
        observations._parse_asos_csv(HTML_ERROR_PAGE)


def test_parse_asos_csv_plaintext_error_message_raises():
    with pytest.raises(ValueError):
        observations._parse_asos_csv("Invalid times provided.")


def test_parse_asos_csv_empty_body_raises():
    with pytest.raises(ValueError):
        observations._parse_asos_csv("")


# ---------------------------------------------------------------------------
# fetch_asos_observations
# ---------------------------------------------------------------------------

def test_fetch_asos_observations_unknown_station_raises():
    with pytest.raises(ValueError):
        observations.fetch_asos_observations("ZZZZ", "2025-01-01", "2025-01-02")


def test_fetch_asos_observations_start_after_end_raises():
    with pytest.raises(ValueError):
        observations.fetch_asos_observations("KNYC", "2025-01-05", "2025-01-01")


def test_fetch_asos_observations_stamps_station_code_and_sorts(monkeypatch):
    session = MagicMock()
    session.get.return_value = _mock_response(
        _csv([("2025-03-01 01:53", "41.00"), ("2025-03-01 00:53", "40.00")])
    )
    monkeypatch.setattr(observations, "_today", lambda: date(2025, 6, 1))
    monkeypatch.setattr(observations.time, "sleep", lambda _: None)

    rows = observations.fetch_asos_observations("knyc", "2025-03-01", "2025-03-01", session=session)
    assert rows == [
        {"valid_utc": "2025-03-01T00:53:00Z", "temp_f": 40.0, "station_code": "KNYC"},
        {"valid_utc": "2025-03-01T01:53:00Z", "temp_f": 41.0, "station_code": "KNYC"},
    ]
    _, kwargs = session.get.call_args
    assert kwargs["params"]["station"] == "KNYC"
    assert kwargs["params"]["data"] == "tmpf"
    assert kwargs["params"]["report_type"] == 3
    assert kwargs["params"]["day1"] == 1
    assert kwargs["params"]["day2"] == 2  # half-open window: end + 1 day
    assert kwargs["timeout"] == (5, 30)


def test_fetch_asos_observations_caches_completed_day(monkeypatch):
    session = MagicMock()
    session.get.return_value = _mock_response(_csv([("2025-01-01 00:53", "10.00")]))
    monkeypatch.setattr(observations, "_today", lambda: date(2025, 6, 1))
    monkeypatch.setattr(observations.time, "sleep", lambda _: None)

    observations.fetch_asos_observations("KNYC", "2025-01-01", "2025-01-01", session=session)
    assert session.get.call_count == 1

    rows = observations.fetch_asos_observations("KNYC", "2025-01-01", "2025-01-01", session=session)
    assert session.get.call_count == 1  # served from cache, no second network call
    assert rows[0]["temp_f"] == 10.0


def test_fetch_asos_observations_always_refetches_recent_day(monkeypatch):
    session = MagicMock()
    session.get.return_value = _mock_response(_csv([("2025-06-01 00:53", "50.00")]))
    monkeypatch.setattr(observations, "_today", lambda: date(2025, 6, 1))
    monkeypatch.setattr(observations.time, "sleep", lambda _: None)

    observations.fetch_asos_observations("KNYC", "2025-06-01", "2025-06-01", session=session)
    observations.fetch_asos_observations("KNYC", "2025-06-01", "2025-06-01", session=session)
    assert session.get.call_count == 2  # "today" itself is always within the recent window


def test_fetch_asos_observations_use_cache_false_always_hits_network(monkeypatch):
    session = MagicMock()
    session.get.return_value = _mock_response(_csv([("2025-01-01 00:53", "10.00")]))
    monkeypatch.setattr(observations, "_today", lambda: date(2025, 6, 1))
    monkeypatch.setattr(observations.time, "sleep", lambda _: None)

    observations.fetch_asos_observations("KNYC", "2025-01-01", "2025-01-01", session=session, use_cache=False)
    observations.fetch_asos_observations("KNYC", "2025-01-01", "2025-01-01", session=session, use_cache=False)
    assert session.get.call_count == 2


def test_fetch_asos_observations_multi_day_range_chunks_per_day(monkeypatch):
    session = MagicMock()
    session.get.return_value = _mock_response(_csv([]))
    monkeypatch.setattr(observations, "_today", lambda: date(2025, 6, 1))
    monkeypatch.setattr(observations.time, "sleep", lambda _: None)

    observations.fetch_asos_observations("KNYC", "2025-01-01", "2025-01-03", session=session)
    assert session.get.call_count == 3


def test_fetch_asos_observations_html_error_page_raises(monkeypatch):
    session = MagicMock()
    session.get.return_value = _mock_response(HTML_ERROR_PAGE)
    monkeypatch.setattr(observations, "_today", lambda: date(2025, 6, 1))

    with pytest.raises(ValueError):
        observations.fetch_asos_observations("KNYC", "2025-01-01", "2025-01-01", session=session)


# ---------------------------------------------------------------------------
# Retry / GET-only configuration
# ---------------------------------------------------------------------------

def test_session_retries_429_and_5xx(monkeypatch):
    monkeypatch.setattr(observations, "_SESSION", None)
    session = observations._get_session()
    adapter = session.get_adapter("https://mesonet.agron.iastate.edu")
    retry = adapter.max_retries
    assert 429 in retry.status_forcelist
    assert {500, 502, 503, 504}.issubset(retry.status_forcelist)
    assert retry.total >= 3
    assert list(retry.allowed_methods) == ["GET"]


# ---------------------------------------------------------------------------
# running_max_by_instant
# ---------------------------------------------------------------------------

def _obs(valid_utc, temp_f):
    return {"valid_utc": valid_utc, "temp_f": temp_f, "station_code": "KNYC"}


def test_running_max_by_instant_local_day_windowing_and_monotonicity():
    offset = -5
    day_start = observations._local_day_start(date(2025, 6, 15), offset)
    obs = [
        _obs(observations._format_utc(day_start - timedelta(hours=1)), 99.0),  # just before window: excluded
        _obs(observations._format_utc(day_start), 60.0),  # exactly at start: included
        _obs(observations._format_utc(day_start + timedelta(hours=5)), 55.0),  # dips: running max stays 60
        _obs(observations._format_utc(day_start + timedelta(hours=10)), 70.0),  # new max
        _obs(observations._format_utc(day_start + timedelta(hours=24)), 999.0),  # exactly at end: excluded
    ]
    running = observations.running_max_by_instant(obs, date(2025, 6, 15), offset)

    assert len(running) == 3
    assert [r["temp_f"] for r in running] == [60.0, 55.0, 70.0]
    assert [r["running_max_f"] for r in running] == [60.0, 60.0, 70.0]
    assert [r["n_obs"] for r in running] == [1, 2, 3]


def test_running_max_by_instant_sorts_out_of_order_input():
    offset = -5
    day_start = observations._local_day_start(date(2025, 6, 15), offset)
    obs = [
        _obs(observations._format_utc(day_start + timedelta(hours=5)), 70.0),
        _obs(observations._format_utc(day_start + timedelta(hours=1)), 60.0),
    ]
    running = observations.running_max_by_instant(obs, date(2025, 6, 15), offset)
    assert [r["temp_f"] for r in running] == [60.0, 70.0]
    assert [r["running_max_f"] for r in running] == [60.0, 70.0]


def test_running_max_by_instant_empty_input_returns_empty():
    assert observations.running_max_by_instant([], date(2025, 6, 15), -5) == []


def test_running_max_by_instant_no_observations_in_window_returns_empty():
    offset = -5
    other_day_start = observations._local_day_start(date(2025, 6, 16), offset)
    obs = [_obs(observations._format_utc(other_day_start + timedelta(hours=1)), 60.0)]
    assert observations.running_max_by_instant(obs, date(2025, 6, 15), offset) == []


# ---------------------------------------------------------------------------
# observed_max_at -- no-look-ahead boundary
# ---------------------------------------------------------------------------

def test_observed_max_at_requires_aware_datetime():
    running = [{"valid_utc": "2025-06-15T12:00:00Z", "temp_f": 70.0, "running_max_f": 70.0, "n_obs": 1}]
    with pytest.raises(ValueError):
        observations.observed_max_at(running, datetime(2025, 6, 15, 12, 20))  # naive


def test_observed_max_at_exact_cutoff_boundary_is_included():
    valid = datetime(2025, 6, 15, 12, 0, tzinfo=timezone.utc)
    running = [{"valid_utc": observations._format_utc(valid), "temp_f": 70.0, "running_max_f": 70.0, "n_obs": 1}]
    latency = observations.OBSERVATION_LATENCY_MINUTES
    # decision_instant exactly at valid + latency -> just barely usable.
    decision_instant = valid + timedelta(minutes=latency)
    result = observations.observed_max_at(running, decision_instant, latency_minutes=latency)
    assert result is not None
    assert result["temp_f"] == 70.0


def test_observed_max_at_one_minute_before_cutoff_is_excluded():
    valid = datetime(2025, 6, 15, 12, 0, tzinfo=timezone.utc)
    running = [{"valid_utc": observations._format_utc(valid), "temp_f": 70.0, "running_max_f": 70.0, "n_obs": 1}]
    latency = observations.OBSERVATION_LATENCY_MINUTES
    decision_instant = valid + timedelta(minutes=latency) - timedelta(minutes=1)
    result = observations.observed_max_at(running, decision_instant, latency_minutes=latency)
    assert result is None


def test_observed_max_at_one_minute_after_cutoff_is_included():
    valid = datetime(2025, 6, 15, 12, 0, tzinfo=timezone.utc)
    running = [{"valid_utc": observations._format_utc(valid), "temp_f": 70.0, "running_max_f": 70.0, "n_obs": 1}]
    latency = observations.OBSERVATION_LATENCY_MINUTES
    decision_instant = valid + timedelta(minutes=latency) + timedelta(minutes=1)
    result = observations.observed_max_at(running, decision_instant, latency_minutes=latency)
    assert result is not None


def test_observed_max_at_picks_latest_qualifying_row():
    base = datetime(2025, 6, 15, 10, 0, tzinfo=timezone.utc)
    running = [
        {"valid_utc": observations._format_utc(base), "temp_f": 60.0, "running_max_f": 60.0, "n_obs": 1},
        {"valid_utc": observations._format_utc(base + timedelta(hours=1)), "temp_f": 65.0, "running_max_f": 65.0, "n_obs": 2},
        {"valid_utc": observations._format_utc(base + timedelta(hours=2)), "temp_f": 62.0, "running_max_f": 65.0, "n_obs": 3},
    ]
    # Decision instant only far enough past the second row.
    decision_instant = base + timedelta(hours=1, minutes=observations.OBSERVATION_LATENCY_MINUTES)
    result = observations.observed_max_at(running, decision_instant)
    assert result["n_obs"] == 2
    assert result["running_max_f"] == 65.0


def test_observed_max_at_empty_running_returns_none():
    now = datetime(2025, 6, 15, 12, 0, tzinfo=timezone.utc)
    assert observations.observed_max_at([], now) is None


def test_observed_max_at_no_qualifying_row_returns_none():
    valid = datetime(2025, 6, 15, 12, 0, tzinfo=timezone.utc)
    running = [{"valid_utc": observations._format_utc(valid), "temp_f": 70.0, "running_max_f": 70.0, "n_obs": 1}]
    # Decision instant far too early -- no latency-adjusted row qualifies.
    decision_instant = valid - timedelta(hours=1)
    assert observations.observed_max_at(running, decision_instant) is None


# ---------------------------------------------------------------------------
# build_daily_summary / format_daily_summary
# ---------------------------------------------------------------------------

def test_build_daily_summary_multi_day():
    offset = -5
    day1_start = observations._local_day_start(date(2025, 6, 15), offset)
    day2_start = observations._local_day_start(date(2025, 6, 16), offset)
    obs = [
        _obs(observations._format_utc(day1_start), 60.0),
        _obs(observations._format_utc(day1_start + timedelta(hours=5)), 70.0),
        _obs(observations._format_utc(day2_start + timedelta(hours=2)), 55.0),
    ]
    summary = observations.build_daily_summary(obs, "2025-06-15", "2025-06-16", offset)
    assert summary == [
        {"date_lst": "2025-06-15", "observed_max_f": 70.0, "n_obs": 2},
        {"date_lst": "2025-06-16", "observed_max_f": 55.0, "n_obs": 1},
    ]


def test_build_daily_summary_day_with_no_observations():
    summary = observations.build_daily_summary([], "2025-06-15", "2025-06-15", -5)
    assert summary == [{"date_lst": "2025-06-15", "observed_max_f": None, "n_obs": 0}]


def test_format_daily_summary_runs_without_error():
    summary = [{"date_lst": "2025-06-15", "observed_max_f": 70.0, "n_obs": 2}]
    text = observations.format_daily_summary(summary)
    assert "2025-06-15" in text
    assert "70.0" in text


# ---------------------------------------------------------------------------
# CLI (parse_args / run_fetch / run_summary)
# ---------------------------------------------------------------------------

def test_parse_args_fetch():
    args = observations.parse_args(["fetch", "--station", "KNYC", "--start", "2025-01-01", "--end", "2025-01-02"])
    assert args.command == "fetch"
    assert args.station == "KNYC"
    assert args.output == observations.DEFAULT_OUTPUT_PATH


def test_parse_args_summary():
    args = observations.parse_args(
        ["summary", "--input", "foo.jsonl", "--station", "KNYC", "--start", "2025-01-01", "--end", "2025-01-02"]
    )
    assert args.command == "summary"
    assert args.input == "foo.jsonl"


def test_run_fetch_writes_jsonl(tmp_path, monkeypatch):
    session = MagicMock()
    session.get.return_value = _mock_response(_csv([("2025-06-15 00:53", "60.00")]))
    monkeypatch.setattr(observations, "_get_session", lambda: session)
    monkeypatch.setattr(observations, "_today", lambda: date(2025, 6, 1))
    monkeypatch.setattr(observations.time, "sleep", lambda _: None)

    output_path = tmp_path / "out" / "obs.jsonl"
    rows = observations.run_fetch("KNYC", "2025-06-15", "2025-06-15", str(output_path))

    assert len(rows) == 1
    assert output_path.exists()
    written = [json.loads(line) for line in output_path.read_text().splitlines()]
    assert written == rows


def test_run_summary_reads_jsonl_filtered_by_station(tmp_path):
    input_path = tmp_path / "obs.jsonl"
    lines = [
        json.dumps({"station_code": "KNYC", "valid_utc": "2025-06-15T12:53:00Z", "temp_f": 60.0}),
        json.dumps({"station_code": "KMDW", "valid_utc": "2025-06-15T12:53:00Z", "temp_f": 999.0}),
    ]
    input_path.write_text("\n".join(lines) + "\n")

    summary = observations.run_summary(str(input_path), "KNYC", "2025-06-15", "2025-06-15")
    assert summary == [{"date_lst": "2025-06-15", "observed_max_f": 60.0, "n_obs": 1}]
