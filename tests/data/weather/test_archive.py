"""Tests for src/data/weather/archive.py — NBM-vs-CLI leakage-safe daily archive."""

import json
from datetime import date, timedelta
from unittest.mock import MagicMock

import pytest

from src.data.weather import archive


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_response(json_data=None, text=None, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    if json_data is not None:
        resp.json.return_value = json_data
    if text is not None:
        resp.text = text
    resp.raise_for_status = MagicMock()
    return resp


def _previous_runs_payload(times, lead1, lead2):
    return {
        "hourly": {
            "time": times,
            "temperature_2m_previous_day1": lead1,
            "temperature_2m_previous_day2": lead2,
        }
    }


def _afos_list(entries):
    return {"data": entries}


CLI_YESTERDAY_TEXT = """
443
CDUS41 KOKX 020716
CLINYC

CLIMATE REPORT
NATIONAL WEATHER SERVICE NEW YORK, NY
216 AM EST SUN MAR 02 2025

...THE CENTRAL PARK NY CLIMATE SUMMARY FOR MARCH 1 2025...

TEMPERATURE (F)
 YESTERDAY
  MAXIMUM         64    100 PM  73    1972  45     19       48

$$
"""

CLI_TODAY_TEXT = """
443
CDUS41 KOKX 021936
CLINYC

CLIMATE REPORT
NATIONAL WEATHER SERVICE NEW YORK, NY
439 PM EST SUN MAR 02 2025

...THE CENTRAL PARK NY CLIMATE SUMMARY FOR MARCH 2 2025...

TEMPERATURE (F)
 TODAY
  MAXIMUM         52    331 PM  70    1974  46      6       59

$$
"""

CLI_MISSING_TEXT = """
443
CDUS41 KOKX 030716
CLINYC

CLIMATE REPORT
NATIONAL WEATHER SERVICE NEW YORK, NY
216 AM EST MON MAR 03 2025

...THE CENTRAL PARK NY CLIMATE SUMMARY FOR MARCH 2 2025...

TEMPERATURE (F)
 YESTERDAY
  MAXIMUM         MM    100 PM  73    1972  45     19       48

$$
"""


@pytest.fixture(autouse=True)
def isolate_cache_dir(tmp_path, monkeypatch):
    """Redirect the shared cache module to a fresh tmp directory for every test."""
    import src.data.cache as cache_mod
    monkeypatch.setattr(cache_mod, "CACHE_DIR", tmp_path / "cache")


# ---------------------------------------------------------------------------
# fetch_nbm_previous_runs
# ---------------------------------------------------------------------------

def test_fetch_nbm_previous_runs_parses_hourly_rows():
    session = MagicMock()
    session.get.return_value = _mock_response(
        _previous_runs_payload(
            ["2025-03-01T00:00", "2025-03-01T01:00"],
            [40.0, 41.0],
            [39.5, 40.5],
        )
    )
    rows = archive.fetch_nbm_previous_runs(
        "KNYC", "2025-03-01", "2025-03-01", session=session, today=date(2025, 4, 1)
    )
    assert rows == [
        {"valid_utc": "2025-03-01T00:00:00Z", "lead1_f": 40.0, "lead2_f": 39.5},
        {"valid_utc": "2025-03-01T01:00:00Z", "lead1_f": 41.0, "lead2_f": 40.5},
    ]
    assert session.get.call_count == 1
    _, kwargs = session.get.call_args
    assert kwargs["params"]["models"] == "ncep_nbm_conus"
    assert kwargs["params"]["temperature_unit"] == "fahrenheit"
    assert kwargs["params"]["timezone"] == "GMT"
    assert kwargs["timeout"] == (5, 30)


def test_fetch_nbm_previous_runs_handles_nulls():
    session = MagicMock()
    session.get.return_value = _mock_response(
        _previous_runs_payload(["2024-06-01T00:00"], [None], [None])
    )
    rows = archive.fetch_nbm_previous_runs(
        "KNYC", "2024-06-01", "2024-06-01", session=session, today=date(2025, 1, 1)
    )
    assert rows[0]["lead1_f"] is None
    assert rows[0]["lead2_f"] is None


def test_fetch_nbm_previous_runs_chunks_multi_month_range():
    session = MagicMock()
    session.get.return_value = _mock_response(_previous_runs_payload([], [], []))
    archive.fetch_nbm_previous_runs(
        "KNYC", "2024-11-01", "2025-02-01", session=session, today=date(2026, 1, 1)
    )
    # ~93 days across a 31-day chunk size -> at least 3 chunks/calls.
    assert session.get.call_count >= 3


def test_fetch_nbm_previous_runs_unknown_station_raises():
    with pytest.raises(ValueError):
        archive.fetch_nbm_previous_runs("ZZZZ", "2025-01-01", "2025-01-02")


def test_fetch_nbm_previous_runs_start_after_end_raises():
    with pytest.raises(ValueError):
        archive.fetch_nbm_previous_runs("KNYC", "2025-01-05", "2025-01-01")


def test_fetch_nbm_previous_runs_caches_completed_chunk(monkeypatch, tmp_path):
    session = MagicMock()
    session.get.return_value = _mock_response(
        _previous_runs_payload(["2024-11-01T00:00"], [40.0], [39.0])
    )
    monkeypatch.setattr(archive.time, "sleep", lambda _: None)

    archive.fetch_nbm_previous_runs(
        "KNYC", "2024-11-01", "2024-11-01", session=session, today=date(2025, 1, 1)
    )
    assert session.get.call_count == 1

    # Second call for the same (completed) range must be served from cache.
    rows = archive.fetch_nbm_previous_runs(
        "KNYC", "2024-11-01", "2024-11-01", session=session, today=date(2025, 1, 1)
    )
    assert session.get.call_count == 1
    assert rows[0]["lead1_f"] == 40.0


def test_fetch_nbm_previous_runs_refetches_current_incomplete_month(monkeypatch):
    session = MagicMock()
    session.get.return_value = _mock_response(
        _previous_runs_payload(["2025-01-01T00:00"], [40.0], [39.0])
    )
    monkeypatch.setattr(archive.time, "sleep", lambda _: None)

    # `today` falls inside the requested chunk -> "current month", never cached.
    archive.fetch_nbm_previous_runs(
        "KNYC", "2025-01-01", "2025-01-01", session=session, today=date(2025, 1, 1)
    )
    archive.fetch_nbm_previous_runs(
        "KNYC", "2025-01-01", "2025-01-01", session=session, today=date(2025, 1, 1)
    )
    assert session.get.call_count == 2


# ---------------------------------------------------------------------------
# fetch_cli_daily_highs
# ---------------------------------------------------------------------------

def test_fetch_cli_daily_highs_parses_yesterday_maximum(monkeypatch):
    session = MagicMock()

    def fake_get(url, params=None, timeout=None):
        if url == archive.IEM_AFOS_LIST_URL and params["date"] == "2025-03-02":
            return _mock_response(_afos_list([
                {"entered": "2025-03-02T07:16:00Z", "product_id": "PROD-MORNING"},
                {"entered": "2025-03-02T21:36:00Z", "product_id": "PROD-EVENING"},
            ]))
        if url == archive.IEM_AFOS_LIST_URL:
            return _mock_response(_afos_list([]))
        if "PROD-MORNING" in url:
            return _mock_response(text=CLI_YESTERDAY_TEXT)
        raise AssertionError(f"unexpected GET {url} {params}")

    session.get.side_effect = fake_get
    monkeypatch.setattr(archive.time, "sleep", lambda _: None)

    rows = archive.fetch_cli_daily_highs(
        "KNYC", "2025-03-01", "2025-03-01", session=session, today=date(2025, 6, 1)
    )
    assert rows == [{"date_lst": "2025-03-01", "max_f": 64.0, "source": "IEM CLI CLINYC (PROD-MORNING)"}]


def test_fetch_cli_daily_highs_skips_today_only_issuance(monkeypatch):
    """A day whose only issuance is a same-day 'TODAY' partial has no finalized row."""
    session = MagicMock()

    def fake_get(url, params=None, timeout=None):
        if url == archive.IEM_AFOS_LIST_URL and params["date"] == "2025-03-02":
            return _mock_response(_afos_list([{"entered": "2025-03-02T21:36:00Z", "product_id": "PROD-TODAY"}]))
        if url == archive.IEM_AFOS_LIST_URL:
            return _mock_response(_afos_list([]))
        if "PROD-TODAY" in url:
            return _mock_response(text=CLI_TODAY_TEXT)
        raise AssertionError(f"unexpected GET {url} {params}")

    session.get.side_effect = fake_get
    monkeypatch.setattr(archive.time, "sleep", lambda _: None)

    rows = archive.fetch_cli_daily_highs(
        "KNYC", "2025-03-02", "2025-03-02", session=session, today=date(2025, 6, 1)
    )
    assert rows == []


def test_fetch_cli_daily_highs_handles_missing_value(monkeypatch):
    session = MagicMock()

    def fake_get(url, params=None, timeout=None):
        if url == archive.IEM_AFOS_LIST_URL and params["date"] == "2025-03-03":
            return _mock_response(_afos_list([{"entered": "2025-03-03T07:16:00Z", "product_id": "PROD-MM"}]))
        if url == archive.IEM_AFOS_LIST_URL:
            return _mock_response(_afos_list([]))
        if "PROD-MM" in url:
            return _mock_response(text=CLI_MISSING_TEXT)
        raise AssertionError(f"unexpected GET {url} {params}")

    session.get.side_effect = fake_get
    monkeypatch.setattr(archive.time, "sleep", lambda _: None)

    rows = archive.fetch_cli_daily_highs(
        "KNYC", "2025-03-02", "2025-03-02", session=session, today=date(2025, 6, 1)
    )
    assert rows == [{"date_lst": "2025-03-02", "max_f": None, "source": "IEM CLI CLINYC (PROD-MM)"}]


def test_fetch_cli_daily_highs_unknown_station_raises():
    with pytest.raises(ValueError):
        archive.fetch_cli_daily_highs("ZZZZ", "2025-01-01", "2025-01-02")


def test_fetch_cli_daily_highs_caches_completed_day(monkeypatch):
    session = MagicMock()
    session.get.side_effect = [
        _mock_response(_afos_list([{"entered": "2025-03-02T07:16:00Z", "product_id": "PROD-MORNING"}])),
        _mock_response(text=CLI_YESTERDAY_TEXT),
        _mock_response(_afos_list([])),  # scan_end day, no report
    ]
    monkeypatch.setattr(archive.time, "sleep", lambda _: None)

    archive.fetch_cli_daily_highs(
        "KNYC", "2025-03-01", "2025-03-01", session=session, today=date(2025, 6, 1)
    )
    assert session.get.call_count == 3

    # Same request again: both scanned UTC days (2025-03-02 list+text,
    # 2025-03-03 list) are well before `today` and must be served from cache.
    session.get.side_effect = AssertionError("should not hit the network again")
    rows = archive.fetch_cli_daily_highs(
        "KNYC", "2025-03-01", "2025-03-01", session=session, today=date(2025, 6, 1)
    )
    assert rows == [{"date_lst": "2025-03-01", "max_f": 64.0, "source": "IEM CLI CLINYC (PROD-MORNING)"}]


def test_fetch_cli_daily_highs_always_refetches_near_today(monkeypatch):
    call_count = {"n": 0}

    def fake_get(url, params=None, timeout=None):
        call_count["n"] += 1
        return _mock_response(_afos_list([]))

    session = MagicMock()
    session.get.side_effect = fake_get
    monkeypatch.setattr(archive.time, "sleep", lambda _: None)

    today = date(2025, 3, 2)
    archive.fetch_cli_daily_highs("KNYC", "2025-03-01", "2025-03-01", session=session, today=today)
    first_calls = call_count["n"]
    archive.fetch_cli_daily_highs("KNYC", "2025-03-01", "2025-03-01", session=session, today=today)
    # Both scanned days are within CLI_RECENT_DAYS_ALWAYS_REFETCH of `today`,
    # so the second run must hit the network again, not read from cache.
    assert call_count["n"] == first_calls * 2


# ---------------------------------------------------------------------------
# Retry / GET-only configuration
# ---------------------------------------------------------------------------

def test_session_retries_429_and_5xx(monkeypatch):
    monkeypatch.setattr(archive, "_SESSION", None)
    session = archive._get_session()
    adapter = session.get_adapter("https://previous-runs-api.open-meteo.com")
    retry = adapter.max_retries
    assert 429 in retry.status_forcelist
    assert {500, 502, 503, 504}.issubset(retry.status_forcelist)
    assert retry.total >= 3
    assert list(retry.allowed_methods) == ["GET"]


# ---------------------------------------------------------------------------
# build_daily_dataset
# ---------------------------------------------------------------------------

def _full_day_forecasts(target_iso, offset_hours, lead1_values=None, lead2_values=None, skip_hours=()):
    """Build 24 hourly forecast rows covering the local-standard day `target_iso`."""
    target = date.fromisoformat(target_iso)
    start = archive._local_day_start(target, offset_hours)
    rows = []
    for h in range(24):
        if h in skip_hours:
            continue
        valid = start + timedelta(hours=h)
        rows.append({
            "valid_utc": archive._format_utc(valid),
            "lead1_f": (lead1_values[h] if lead1_values else 50.0 + h),
            "lead2_f": (lead2_values[h] if lead2_values else 48.0 + h),
        })
    return rows


def test_build_daily_dataset_complete_day():
    forecasts = _full_day_forecasts("2025-06-15", -5)
    cli_rows = [{"date_lst": "2025-06-15", "max_f": 80.0, "source": "test"}]
    rows = archive.build_daily_dataset("KNYC", forecasts, cli_rows, -5)
    assert len(rows) == 1
    row = rows[0]
    assert row["complete"] is True
    assert row["reason"] is None
    assert row["forecast_max_lead1_f"] == 50.0 + 23
    assert row["forecast_max_lead2_f"] == 48.0 + 23
    assert row["observed_max_f"] == 80.0
    assert len(row["hourly_lead1"]) == 24
    assert row["station"] == "KNYC"
    assert row["date_lst"] == "2025-06-15"


def test_build_daily_dataset_missing_hour_marks_incomplete():
    forecasts = _full_day_forecasts("2025-06-15", -5, skip_hours={5})
    cli_rows = [{"date_lst": "2025-06-15", "max_f": 80.0, "source": "test"}]
    rows = archive.build_daily_dataset("KNYC", forecasts, cli_rows, -5)
    row = rows[0]
    assert row["complete"] is False
    assert "lead1 missing 1/24 hours" in row["reason"]
    assert row["forecast_max_lead1_f"] is None
    assert row["hourly_lead1"][5] is None


def test_build_daily_dataset_null_value_marks_incomplete():
    lead1 = [50.0 + h for h in range(24)]
    lead1[10] = None
    forecasts = _full_day_forecasts("2025-06-15", -5, lead1_values=lead1)
    cli_rows = [{"date_lst": "2025-06-15", "max_f": 80.0, "source": "test"}]
    rows = archive.build_daily_dataset("KNYC", forecasts, cli_rows, -5)
    row = rows[0]
    assert row["complete"] is False
    assert row["forecast_max_lead1_f"] is None


def test_build_daily_dataset_no_cli_marks_incomplete_with_reason():
    forecasts = _full_day_forecasts("2025-06-15", -5)
    rows = archive.build_daily_dataset("KNYC", forecasts, [], -5)
    row = rows[0]
    assert row["complete"] is False
    assert row["observed_max_f"] is None
    assert "no CLI observation for date" in row["reason"]


def test_build_daily_dataset_cli_missing_value_marks_incomplete_with_reason():
    forecasts = _full_day_forecasts("2025-06-15", -5)
    cli_rows = [{"date_lst": "2025-06-15", "max_f": None, "source": "test"}]
    rows = archive.build_daily_dataset("KNYC", forecasts, cli_rows, -5)
    row = rows[0]
    assert row["complete"] is False
    assert "CLI observed max missing (MM)" in row["reason"]


def test_build_daily_dataset_latest_issued_is_one_hour_before_local_midnight():
    forecasts = _full_day_forecasts("2025-06-15", -5)
    cli_rows = [{"date_lst": "2025-06-15", "max_f": 80.0, "source": "test"}]
    row = archive.build_daily_dataset("KNYC", forecasts, cli_rows, -5)[0]
    start = archive._local_day_start(date(2025, 6, 15), -5)
    expected = archive._format_utc(start - timedelta(hours=1))
    assert row["latest_issued_utc_lead1"] == expected


def test_build_daily_dataset_dst_transition_no_shift():
    """Local-standard offset is fixed, so consecutive days across a real-world
    DST change must still each span exactly 24 UTC hours with no gap/overlap."""
    # US spring-forward 2025-03-09; -5 is the station's fixed *standard* offset
    # (never DST-adjusted), so this must not affect anything computed here.
    day_before = archive._local_day_start(date(2025, 3, 8), -5)
    day_of = archive._local_day_start(date(2025, 3, 9), -5)
    day_after = archive._local_day_start(date(2025, 3, 10), -5)
    assert (day_of - day_before) == timedelta(hours=24)
    assert (day_after - day_of) == timedelta(hours=24)

    forecasts = _full_day_forecasts("2025-03-09", -5)
    cli_rows = [{"date_lst": "2025-03-09", "max_f": 55.0, "source": "test"}]
    row = archive.build_daily_dataset("KNYC", forecasts, cli_rows, -5)[0]
    assert row["complete"] is True
    assert len(row["hourly_lead1"]) == 24


def test_build_daily_dataset_sorted_by_date():
    forecasts = _full_day_forecasts("2025-06-16", -5) + _full_day_forecasts("2025-06-15", -5)
    cli_rows = [
        {"date_lst": "2025-06-16", "max_f": 81.0, "source": "test"},
        {"date_lst": "2025-06-15", "max_f": 80.0, "source": "test"},
    ]
    rows = archive.build_daily_dataset("KNYC", forecasts, cli_rows, -5)
    assert [r["date_lst"] for r in rows] == ["2025-06-15", "2025-06-16"]


# ---------------------------------------------------------------------------
# summarize_errors
# ---------------------------------------------------------------------------

def _row(station, date_lst, forecast1, observed, forecast2=None):
    return {
        "station": station,
        "date_lst": date_lst,
        "forecast_max_lead1_f": forecast1,
        "forecast_max_lead2_f": forecast2,
        "observed_max_f": observed,
        "complete": forecast1 is not None and observed is not None,
        "reason": None,
    }


def test_summarize_errors_train_test_split():
    rows = [
        _row("KNYC", "2025-01-01", 50.0, 52.0),  # train, error +2
        _row("KNYC", "2025-01-02", 50.0, 48.0),  # train, error -2
        _row("KNYC", "2025-02-01", 50.0, 55.0),  # test, error +5
    ]
    summary = archive.summarize_errors(rows, "2025-01-31")
    train = summary["train"]["KNYC"]["lead1"]
    test = summary["test"]["KNYC"]["lead1"]
    assert train["n"] == 2
    assert train["mean_bias_f"] == 0.0
    assert test["n"] == 1
    assert test["mean_bias_f"] == 5.0


def test_summarize_errors_excludes_incomplete_rows():
    rows = [
        _row("KNYC", "2025-01-01", None, 52.0),  # no forecast -> excluded
        _row("KNYC", "2025-01-02", 50.0, None),  # no observation -> excluded
        _row("KNYC", "2025-01-03", 50.0, 51.0),  # counted
    ]
    summary = archive.summarize_errors(rows, "2025-12-31")
    assert summary["train"]["KNYC"]["lead1"]["n"] == 1
    assert summary["train"]["KNYC"]["lead1"]["mean_bias_f"] == 1.0


def test_summarize_errors_by_season():
    rows = [
        _row("KNYC", "2025-01-15", 50.0, 51.0),  # DJF
        _row("KNYC", "2025-04-15", 60.0, 62.0),  # MAM
        _row("KNYC", "2025-07-15", 80.0, 79.0),  # JJA
        _row("KNYC", "2025-10-15", 65.0, 66.0),  # SON
    ]
    summary = archive.summarize_errors(rows, "2025-12-31")
    by_season = summary["train"]["KNYC"]["by_season"]
    assert by_season["DJF"]["lead1"]["n"] == 1
    assert by_season["MAM"]["lead1"]["n"] == 1
    assert by_season["JJA"]["lead1"]["n"] == 1
    assert by_season["SON"]["lead1"]["n"] == 1
    assert by_season["JJA"]["lead1"]["mean_bias_f"] == -1.0


def test_summarize_errors_std_none_for_single_point():
    rows = [_row("KNYC", "2025-01-01", 50.0, 52.0)]
    summary = archive.summarize_errors(rows, "2025-12-31")
    assert summary["train"]["KNYC"]["lead1"]["std_f"] is None


def test_summarize_errors_multiple_stations_independent():
    rows = [
        _row("KNYC", "2025-01-01", 50.0, 52.0),
        _row("KMDW", "2025-01-01", 30.0, 25.0),
    ]
    summary = archive.summarize_errors(rows, "2025-12-31")
    assert summary["train"]["KNYC"]["lead1"]["mean_bias_f"] == 2.0
    assert summary["train"]["KMDW"]["lead1"]["mean_bias_f"] == -5.0


def test_summarize_errors_lead2_tracked_independently():
    rows = [_row("KNYC", "2025-01-01", 50.0, 52.0, forecast2=49.0)]
    summary = archive.summarize_errors(rows, "2025-12-31")
    assert summary["train"]["KNYC"]["lead2"]["mean_bias_f"] == 3.0


def test_format_summary_runs_without_error():
    rows = [_row("KNYC", "2025-01-01", 50.0, 52.0)]
    summary = archive.summarize_errors(rows, "2025-12-31")
    text = archive.format_summary(summary)
    assert "KNYC" in text
    assert "TRAIN" in text


# ---------------------------------------------------------------------------
# CLI (parse_args / run_build / run_summarize)
# ---------------------------------------------------------------------------

def test_parse_args_build():
    args = archive.parse_args(["build", "--start", "2024-11-01", "--end", "2025-01-01"])
    assert args.command == "build"
    assert args.start == "2024-11-01"
    assert args.end == "2025-01-01"
    assert args.output == archive.DEFAULT_OUTPUT_PATH
    assert set(args.stations.split(",")) == {"KNYC", "KMDW", "KMIA", "KAUS"}


def test_parse_args_summarize():
    args = archive.parse_args(["summarize", "--input", "foo.jsonl", "--train-end", "2025-01-01"])
    assert args.command == "summarize"
    assert args.input == "foo.jsonl"
    assert args.train_end == "2025-01-01"


def test_run_build_writes_jsonl(tmp_path, monkeypatch):
    forecasts = _full_day_forecasts("2025-06-15", -5)
    cli_rows = [{"date_lst": "2025-06-15", "max_f": 80.0, "source": "test"}]

    monkeypatch.setattr(archive, "fetch_nbm_previous_runs", lambda *a, **k: forecasts)
    monkeypatch.setattr(archive, "fetch_cli_daily_highs", lambda *a, **k: cli_rows)

    output_path = tmp_path / "out" / "dataset.jsonl"
    rows = archive.run_build("2025-06-15", "2025-06-15", ["KNYC"], str(output_path))

    assert len(rows) == 1
    assert output_path.exists()
    written = [json.loads(line) for line in output_path.read_text().splitlines()]
    assert written == rows


def test_run_summarize_reads_jsonl(tmp_path):
    input_path = tmp_path / "dataset.jsonl"
    input_path.write_text(json.dumps(_row("KNYC", "2025-01-01", 50.0, 52.0)) + "\n")
    summary = archive.run_summarize(str(input_path), "2025-12-31")
    assert summary["train"]["KNYC"]["lead1"]["n"] == 1


class TestDegenerateSeries:
    """Open-Meteo reports an uncovered lead as a constant 0 C, not as null."""

    def test_day_length_constant_series_is_degenerate(self):
        assert archive.is_degenerate_series([32.0] * 24) is True

    def test_varying_series_is_not(self):
        assert archive.is_degenerate_series([32.0] * 23 + [33.0]) is False

    def test_short_constant_run_is_not_flagged(self):
        # A single hour, or a partial window, is ordinary.
        assert archive.is_degenerate_series([32.0]) is False
        assert archive.is_degenerate_series([32.0] * 3) is False

    def test_series_with_a_missing_value_is_not_degenerate(self):
        # That is ordinary incompleteness, reported by its own reason.
        assert archive.is_degenerate_series([32.0] * 23 + [None]) is False

    def test_constant_lead_marks_the_day_incomplete_with_a_named_reason(self):
        # Observed live: KMIA 2024-11-20 had lead2 = 32.0 F for all 24 hours
        # while lead1 forecast the low 80s and the day reached 85 F.
        offset = -5
        start = archive._local_day_start(date(2024, 11, 20), offset)
        forecasts = [
            {"valid_utc": archive._format_utc(start + timedelta(hours=h)),
             "lead1_f": 80.0 + h * 0.1, "lead2_f": 32.0}
            for h in range(24)
        ]
        cli = [{"date_lst": "2024-11-20", "max_f": 85.0, "source": "test"}]
        rows = archive.build_daily_dataset("KMIA", forecasts, cli, offset)
        row = next(r for r in rows if r["date_lst"] == "2024-11-20")
        assert row["complete"] is False
        assert row["forecast_max_lead2_f"] is None
        assert "constant" in row["reason"] and "fill value" in row["reason"]
        # The usable lead is unaffected.
        assert row["forecast_max_lead1_f"] == pytest.approx(82.3)
