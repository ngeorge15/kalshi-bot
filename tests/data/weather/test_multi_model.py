"""Tests for src/data/weather/multi_model.py — multi-model ensemble archive."""

import json
import statistics
from datetime import date, timedelta
from unittest.mock import MagicMock

import pytest

from src.data.weather import multi_model
from src.data.weather.archive import _format_utc, _local_day_start


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_response(json_data=None, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    if json_data is not None:
        resp.json.return_value = json_data
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


@pytest.fixture(autouse=True)
def isolate_cache_dir(tmp_path, monkeypatch):
    """Redirect the shared cache module to a fresh tmp directory for every test."""
    import src.data.cache as cache_mod
    monkeypatch.setattr(cache_mod, "CACHE_DIR", tmp_path / "cache")


def _full_day_forecasts(target_iso, offset_hours, lead1_values=None, lead2_values=None, skip_hours=()):
    """Build 24 hourly forecast rows covering the local-standard day `target_iso`."""
    target = date.fromisoformat(target_iso)
    start = _local_day_start(target, offset_hours)
    rows = []
    for h in range(24):
        if h in skip_hours:
            continue
        valid = start + timedelta(hours=h)
        rows.append({
            "valid_utc": _format_utc(valid),
            "lead1_f": (lead1_values[h] if lead1_values else 50.0 + h),
            "lead2_f": (lead2_values[h] if lead2_values else 48.0 + h),
        })
    return rows


# ---------------------------------------------------------------------------
# fetch_model_previous_runs
# ---------------------------------------------------------------------------

def test_fetch_model_previous_runs_parses_hourly_rows():
    session = MagicMock()
    session.get.return_value = _mock_response(
        _previous_runs_payload(
            ["2025-06-01T00:00", "2025-06-01T01:00"],
            [70.0, 71.0],
            [69.5, 70.5],
        )
    )
    rows = multi_model.fetch_model_previous_runs(
        "gfs_seamless", "KNYC", "2025-06-01", "2025-06-01", session=session, today=date(2025, 7, 1)
    )
    assert rows == [
        {"valid_utc": "2025-06-01T00:00:00Z", "lead1_f": 70.0, "lead2_f": 69.5},
        {"valid_utc": "2025-06-01T01:00:00Z", "lead1_f": 71.0, "lead2_f": 70.5},
    ]
    _, kwargs = session.get.call_args
    assert kwargs["params"]["models"] == "gfs_seamless"
    assert kwargs["params"]["temperature_unit"] == "fahrenheit"
    assert kwargs["params"]["timezone"] == "GMT"
    assert kwargs["timeout"] == (5, 30)


def test_fetch_model_previous_runs_unknown_station_raises():
    with pytest.raises(ValueError):
        multi_model.fetch_model_previous_runs("gfs_seamless", "ZZZZ", "2025-01-01", "2025-01-02")


def test_fetch_model_previous_runs_start_after_end_raises():
    with pytest.raises(ValueError):
        multi_model.fetch_model_previous_runs("gfs_seamless", "KNYC", "2025-01-05", "2025-01-01")


def test_fetch_model_previous_runs_all_nulls_is_not_an_error():
    """Querying a model before its archive coverage begins returns nulls, not an exception."""
    session = MagicMock()
    session.get.return_value = _mock_response(
        _previous_runs_payload(["2025-01-15T00:00"], [None], [None])
    )
    rows = multi_model.fetch_model_previous_runs(
        "ecmwf_aifs025_single", "KNYC", "2025-01-15", "2025-01-15", session=session, today=date(2025, 6, 1)
    )
    assert rows == [{"valid_utc": "2025-01-15T00:00:00Z", "lead1_f": None, "lead2_f": None}]


def test_fetch_model_previous_runs_caches_completed_chunk(monkeypatch):
    session = MagicMock()
    session.get.return_value = _mock_response(
        _previous_runs_payload(["2024-11-01T00:00"], [40.0], [39.0])
    )
    monkeypatch.setattr(multi_model.time, "sleep", lambda _: None)

    multi_model.fetch_model_previous_runs(
        "icon_seamless", "KNYC", "2024-11-01", "2024-11-01", session=session, today=date(2025, 1, 1)
    )
    assert session.get.call_count == 1

    rows = multi_model.fetch_model_previous_runs(
        "icon_seamless", "KNYC", "2024-11-01", "2024-11-01", session=session, today=date(2025, 1, 1)
    )
    assert session.get.call_count == 1
    assert rows[0]["lead1_f"] == 40.0


def test_fetch_model_previous_runs_different_models_do_not_share_cache(monkeypatch):
    """Same station/date range, two different models -- both must hit the network."""
    session = MagicMock()
    session.get.return_value = _mock_response(
        _previous_runs_payload(["2024-11-01T00:00"], [40.0], [39.0])
    )
    monkeypatch.setattr(multi_model.time, "sleep", lambda _: None)

    multi_model.fetch_model_previous_runs(
        "icon_seamless", "KNYC", "2024-11-01", "2024-11-01", session=session, today=date(2025, 1, 1)
    )
    multi_model.fetch_model_previous_runs(
        "gfs_seamless", "KNYC", "2024-11-01", "2024-11-01", session=session, today=date(2025, 1, 1)
    )
    assert session.get.call_count == 2


def test_fetch_model_previous_runs_refetches_current_incomplete_month(monkeypatch):
    session = MagicMock()
    session.get.return_value = _mock_response(
        _previous_runs_payload(["2025-01-01T00:00"], [40.0], [39.0])
    )
    monkeypatch.setattr(multi_model.time, "sleep", lambda _: None)

    multi_model.fetch_model_previous_runs(
        "gfs_seamless", "KNYC", "2025-01-01", "2025-01-01", session=session, today=date(2025, 1, 1)
    )
    multi_model.fetch_model_previous_runs(
        "gfs_seamless", "KNYC", "2025-01-01", "2025-01-01", session=session, today=date(2025, 1, 1)
    )
    assert session.get.call_count == 2


def test_fetch_model_previous_runs_use_cache_false_always_hits_network(monkeypatch):
    session = MagicMock()
    session.get.return_value = _mock_response(
        _previous_runs_payload(["2024-11-01T00:00"], [40.0], [39.0])
    )
    monkeypatch.setattr(multi_model.time, "sleep", lambda _: None)

    multi_model.fetch_model_previous_runs(
        "gfs_seamless", "KNYC", "2024-11-01", "2024-11-01",
        session=session, today=date(2025, 1, 1), use_cache=False,
    )
    multi_model.fetch_model_previous_runs(
        "gfs_seamless", "KNYC", "2024-11-01", "2024-11-01",
        session=session, today=date(2025, 1, 1), use_cache=False,
    )
    assert session.get.call_count == 2


def test_fetch_model_previous_runs_chunks_multi_month_range():
    session = MagicMock()
    session.get.return_value = _mock_response(_previous_runs_payload([], [], []))
    multi_model.fetch_model_previous_runs(
        "gfs_seamless", "KNYC", "2024-11-01", "2025-02-01", session=session, today=date(2026, 1, 1)
    )
    assert session.get.call_count >= 3


# ---------------------------------------------------------------------------
# build_multi_model_dataset -- frozen schema
# ---------------------------------------------------------------------------

def test_build_multi_model_dataset_frozen_schema_keys():
    forecasts_by_model = {
        "ncep_nbm_conus": _full_day_forecasts("2025-06-15", -5, lead1_values=[70.0 + h for h in range(24)]),
        "gfs_seamless": _full_day_forecasts("2025-06-15", -5, lead1_values=[72.0 + h for h in range(24)]),
    }
    cli_rows = [{"date_lst": "2025-06-15", "max_f": 80.0, "source": "test"}]
    rows = multi_model.build_multi_model_dataset("KNYC", forecasts_by_model, cli_rows, -5)
    assert len(rows) == 1
    row = rows[0]

    assert set(row.keys()) == {
        "station", "date_lst", "models",
        "ensemble_mean_lead1_f", "ensemble_spread_lead1_f",
        "ensemble_min_lead1_f", "ensemble_max_lead1_f", "n_models_lead1",
        "ensemble_mean_lead2_f", "ensemble_spread_lead2_f", "n_models_lead2",
        "observed_max_f", "complete", "reason",
    }
    assert row["station"] == "KNYC"
    assert row["date_lst"] == "2025-06-15"
    assert isinstance(row["models"], dict)
    for model_id in multi_model.MODEL_IDS:
        assert model_id in row["models"]
        assert set(row["models"][model_id].keys()) == {
            "forecast_max_lead1_f", "forecast_max_lead2_f", "degenerate_lead1", "degenerate_lead2",
        }


def test_build_multi_model_dataset_complete_day_values():
    forecasts_by_model = {
        "ncep_nbm_conus": _full_day_forecasts("2025-06-15", -5, lead1_values=[70.0 + h for h in range(24)]),
        "gfs_seamless": _full_day_forecasts("2025-06-15", -5, lead1_values=[72.0 + h for h in range(24)]),
    }
    cli_rows = [{"date_lst": "2025-06-15", "max_f": 80.0, "source": "test"}]
    row = multi_model.build_multi_model_dataset("KNYC", forecasts_by_model, cli_rows, -5)[0]

    assert row["models"]["ncep_nbm_conus"]["forecast_max_lead1_f"] == pytest.approx(93.0)
    assert row["models"]["gfs_seamless"]["forecast_max_lead1_f"] == pytest.approx(95.0)
    assert row["n_models_lead1"] == 2
    assert row["ensemble_mean_lead1_f"] == pytest.approx(94.0)
    assert row["ensemble_min_lead1_f"] == pytest.approx(93.0)
    assert row["ensemble_max_lead1_f"] == pytest.approx(95.0)
    assert row["observed_max_f"] == 80.0
    assert row["complete"] is True
    assert row["reason"] is None


# ---------------------------------------------------------------------------
# Degenerate exclusion
# ---------------------------------------------------------------------------

def test_degenerate_model_excluded_from_ensemble_aggregates():
    forecasts_by_model = {
        "ncep_nbm_conus": _full_day_forecasts("2025-06-15", -5, lead1_values=[70.0 + h for h in range(24)]),
        # Fill-value plateau: constant 32.0F for all 24 hours.
        "gfs_seamless": _full_day_forecasts("2025-06-15", -5, lead1_values=[32.0] * 24),
    }
    cli_rows = [{"date_lst": "2025-06-15", "max_f": 80.0, "source": "test"}]
    row = multi_model.build_multi_model_dataset("KNYC", forecasts_by_model, cli_rows, -5)[0]

    assert row["models"]["gfs_seamless"]["degenerate_lead1"] is True
    assert row["models"]["gfs_seamless"]["forecast_max_lead1_f"] is None
    # Only the non-degenerate model contributes to the ensemble.
    assert row["n_models_lead1"] == 1
    assert row["ensemble_mean_lead1_f"] == pytest.approx(93.0)
    assert "gfs_seamless lead1 constant" in row["reason"]
    assert "fill value" in row["reason"]


def test_degenerate_series_short_run_not_flagged():
    # A single non-constant hour among an otherwise-missing series is not
    # degenerate -- but is also incomplete (missing hours), so it still
    # contributes None. Sanity-check it isn't misflagged as degenerate.
    lead1 = [None] * 24
    lead1[0] = 32.0
    forecasts_by_model = {"gfs_seamless": _full_day_forecasts("2025-06-15", -5, lead1_values=lead1)}
    cli_rows = [{"date_lst": "2025-06-15", "max_f": 80.0, "source": "test"}]
    row = multi_model.build_multi_model_dataset("KNYC", forecasts_by_model, cli_rows, -5)[0]
    assert row["models"]["gfs_seamless"]["degenerate_lead1"] is False
    assert row["models"]["gfs_seamless"]["forecast_max_lead1_f"] is None


# ---------------------------------------------------------------------------
# Incomplete lead -> None
# ---------------------------------------------------------------------------

def test_incomplete_lead_missing_hour_is_none():
    forecasts_by_model = {
        "gfs_seamless": _full_day_forecasts("2025-06-15", -5, skip_hours={5}),
    }
    cli_rows = [{"date_lst": "2025-06-15", "max_f": 80.0, "source": "test"}]
    row = multi_model.build_multi_model_dataset("KNYC", forecasts_by_model, cli_rows, -5)[0]
    assert row["models"]["gfs_seamless"]["forecast_max_lead1_f"] is None
    assert row["n_models_lead1"] == 0
    assert row["ensemble_mean_lead1_f"] is None
    assert row["complete"] is False
    assert "no model produced a complete lead1" in row["reason"]


# ---------------------------------------------------------------------------
# Spread
# ---------------------------------------------------------------------------

def test_spread_none_when_fewer_than_two_models():
    forecasts_by_model = {
        "gfs_seamless": _full_day_forecasts("2025-06-15", -5, lead1_values=[70.0 + h for h in range(24)]),
    }
    cli_rows = [{"date_lst": "2025-06-15", "max_f": 80.0, "source": "test"}]
    row = multi_model.build_multi_model_dataset("KNYC", forecasts_by_model, cli_rows, -5)[0]
    assert row["n_models_lead1"] == 1
    assert row["ensemble_spread_lead1_f"] is None


def test_spread_correct_for_known_set():
    # Three models, lead1 maxima 90, 92, 94 -- known population stdev.
    forecasts_by_model = {
        "ncep_nbm_conus": _full_day_forecasts("2025-06-15", -5, lead1_values=[66.0 + h for h in range(24)]),  # max 89
        "gfs_seamless": _full_day_forecasts("2025-06-15", -5, lead1_values=[68.0 + h for h in range(24)]),  # max 91
        "icon_seamless": _full_day_forecasts("2025-06-15", -5, lead1_values=[70.0 + h for h in range(24)]),  # max 93
    }
    cli_rows = [{"date_lst": "2025-06-15", "max_f": 90.0, "source": "test"}]
    row = multi_model.build_multi_model_dataset("KNYC", forecasts_by_model, cli_rows, -5)[0]

    values = [89.0, 91.0, 93.0]
    assert row["n_models_lead1"] == 3
    assert row["ensemble_mean_lead1_f"] == pytest.approx(statistics.mean(values))
    assert row["ensemble_spread_lead1_f"] == pytest.approx(statistics.pstdev(values))
    assert row["ensemble_min_lead1_f"] == pytest.approx(89.0)
    assert row["ensemble_max_lead1_f"] == pytest.approx(93.0)


def test_lead2_ensemble_stats_independent_of_lead1():
    forecasts_by_model = {
        "gfs_seamless": _full_day_forecasts(
            "2025-06-15", -5,
            lead1_values=[70.0 + h for h in range(24)],
            lead2_values=[60.0 + h for h in range(24)],
        ),
        "icon_seamless": _full_day_forecasts(
            "2025-06-15", -5,
            lead1_values=[71.0 + h for h in range(24)],
            lead2_values=[62.0 + h for h in range(24)],
        ),
    }
    cli_rows = [{"date_lst": "2025-06-15", "max_f": 90.0, "source": "test"}]
    row = multi_model.build_multi_model_dataset("KNYC", forecasts_by_model, cli_rows, -5)[0]
    assert row["n_models_lead2"] == 2
    assert row["ensemble_mean_lead2_f"] == pytest.approx(statistics.mean([83.0, 85.0]))
    assert row["ensemble_spread_lead2_f"] == pytest.approx(statistics.pstdev([83.0, 85.0]))


# ---------------------------------------------------------------------------
# Per-model archive start handling
# ---------------------------------------------------------------------------

def test_model_queried_before_its_archive_start_contributes_none_not_degenerate():
    """A model with an all-null day (pre-onset) must not be flagged degenerate."""
    all_null = [
        {"valid_utc": row["valid_utc"], "lead1_f": None, "lead2_f": None}
        for row in _full_day_forecasts("2025-01-01", -5)
    ]
    forecasts_by_model = {
        "ecmwf_aifs025_single": all_null,  # before MODEL_ARCHIVE_USABLE_START for this model
        "gfs_seamless": _full_day_forecasts("2025-01-01", -5, lead1_values=[40.0 + h for h in range(24)]),
    }
    cli_rows = [{"date_lst": "2025-01-01", "max_f": 45.0, "source": "test"}]
    row = multi_model.build_multi_model_dataset("KNYC", forecasts_by_model, cli_rows, -5)[0]

    assert row["models"]["ecmwf_aifs025_single"]["degenerate_lead1"] is False
    assert row["models"]["ecmwf_aifs025_single"]["forecast_max_lead1_f"] is None
    assert row["n_models_lead1"] == 1  # only gfs_seamless contributed
    assert row["complete"] is True  # at least one model + observed max


def test_model_archive_usable_start_recorded_for_all_model_ids():
    for model_id in multi_model.MODEL_IDS:
        assert model_id in multi_model.MODEL_ARCHIVE_USABLE_START
        assert isinstance(multi_model.MODEL_ARCHIVE_USABLE_START[model_id], date)


# ---------------------------------------------------------------------------
# Missing model (absent from forecasts_by_model entirely)
# ---------------------------------------------------------------------------

def test_missing_model_treated_same_as_no_rows():
    forecasts_by_model = {
        "gfs_seamless": _full_day_forecasts("2025-06-15", -5, lead1_values=[70.0 + h for h in range(24)]),
        # All other MODEL_IDS simply absent from the dict.
    }
    cli_rows = [{"date_lst": "2025-06-15", "max_f": 80.0, "source": "test"}]
    row = multi_model.build_multi_model_dataset("KNYC", forecasts_by_model, cli_rows, -5)[0]
    assert row["n_models_lead1"] == 1
    for model_id in multi_model.MODEL_IDS:
        if model_id != "gfs_seamless":
            assert row["models"][model_id]["forecast_max_lead1_f"] is None
            assert row["models"][model_id]["degenerate_lead1"] is False


# ---------------------------------------------------------------------------
# No CLI observation / missing value
# ---------------------------------------------------------------------------

def test_no_cli_observation_marks_incomplete_with_reason():
    forecasts_by_model = {
        "gfs_seamless": _full_day_forecasts("2025-06-15", -5),
    }
    rows = multi_model.build_multi_model_dataset("KNYC", forecasts_by_model, [], -5)
    row = rows[0]
    assert row["complete"] is False
    assert row["observed_max_f"] is None
    assert "no CLI observation for date" in row["reason"]


def test_sorted_by_date():
    forecasts_by_model = {
        "gfs_seamless": (
            _full_day_forecasts("2025-06-16", -5) + _full_day_forecasts("2025-06-15", -5)
        ),
    }
    cli_rows = [
        {"date_lst": "2025-06-16", "max_f": 81.0, "source": "test"},
        {"date_lst": "2025-06-15", "max_f": 80.0, "source": "test"},
    ]
    rows = multi_model.build_multi_model_dataset("KNYC", forecasts_by_model, cli_rows, -5)
    assert [r["date_lst"] for r in rows] == ["2025-06-15", "2025-06-16"]


# ---------------------------------------------------------------------------
# summarize_multi_model_errors
# ---------------------------------------------------------------------------

def _row(date_lst, models, observed, ensemble_mean_lead1=None, ensemble_mean_lead2=None):
    return {
        "station": "KNYC",
        "date_lst": date_lst,
        "models": models,
        "ensemble_mean_lead1_f": ensemble_mean_lead1,
        "ensemble_spread_lead1_f": None,
        "ensemble_min_lead1_f": None,
        "ensemble_max_lead1_f": None,
        "n_models_lead1": sum(1 for m in models.values() if m.get("forecast_max_lead1_f") is not None),
        "ensemble_mean_lead2_f": ensemble_mean_lead2,
        "ensemble_spread_lead2_f": None,
        "n_models_lead2": sum(1 for m in models.values() if m.get("forecast_max_lead2_f") is not None),
        "observed_max_f": observed,
        "complete": True,
        "reason": None,
    }


def _model_entry(lead1=None, lead2=None):
    return {
        "forecast_max_lead1_f": lead1, "forecast_max_lead2_f": lead2,
        "degenerate_lead1": False, "degenerate_lead2": False,
    }


def test_summarize_multi_model_errors_train_test_split_and_mae():
    rows = [
        _row("2025-01-01", {"ncep_nbm_conus": _model_entry(50.0)}, 52.0, ensemble_mean_lead1=50.0),
        _row("2025-01-02", {"ncep_nbm_conus": _model_entry(50.0)}, 48.0, ensemble_mean_lead1=50.0),
        _row("2025-02-01", {"ncep_nbm_conus": _model_entry(50.0)}, 55.0, ensemble_mean_lead1=50.0),
    ]
    summary = multi_model.summarize_multi_model_errors(rows, "2025-01-31")
    train = summary["train"]["ncep_nbm_conus"]["lead1"]
    test = summary["test"]["ncep_nbm_conus"]["lead1"]
    assert summary["train"]["n_days"] == 2
    assert summary["test"]["n_days"] == 1
    assert train["n"] == 2
    assert train["mae_f"] == pytest.approx(2.0)  # |52-50|, |48-50| -> mean 2
    assert test["n"] == 1
    assert test["mae_f"] == pytest.approx(5.0)
    assert train["coverage_pct"] == pytest.approx(100.0)

    ensemble_train = summary["train"][multi_model.ENSEMBLE_KEY]["lead1"]
    assert ensemble_train["mae_f"] == pytest.approx(2.0)


def test_summarize_multi_model_errors_missing_model_excluded_not_zero():
    rows = [
        _row("2025-01-01", {"ncep_nbm_conus": _model_entry(50.0)}, 52.0),
        _row("2025-01-02", {"ncep_nbm_conus": _model_entry(None)}, 48.0),  # model absent this day
    ]
    summary = multi_model.summarize_multi_model_errors(rows, "2025-12-31")
    stats = summary["train"]["ncep_nbm_conus"]["lead1"]
    assert stats["n"] == 1
    assert stats["mae_f"] == pytest.approx(2.0)
    assert stats["coverage_pct"] == pytest.approx(50.0)


def test_format_multi_model_summary_runs_without_error():
    rows = [_row("2025-01-01", {"ncep_nbm_conus": _model_entry(50.0)}, 52.0, ensemble_mean_lead1=50.0)]
    summary = multi_model.summarize_multi_model_errors(rows, "2025-12-31")
    text = multi_model.format_multi_model_summary(summary)
    assert "ncep_nbm_conus" in text
    assert multi_model.ENSEMBLE_KEY in text
    assert "TRAIN" in text


# ---------------------------------------------------------------------------
# CLI (parse_args / run_build / run_summarize)
# ---------------------------------------------------------------------------

def test_parse_args_build_defaults():
    args = multi_model.parse_args(["build", "--start", "2024-12-01", "--end", "2025-01-01"])
    assert args.command == "build"
    assert args.output == multi_model.DEFAULT_OUTPUT_PATH
    assert set(args.models.split(",")) == set(multi_model.MODEL_IDS)


def test_parse_args_summarize():
    args = multi_model.parse_args(["summarize", "--input", "foo.jsonl", "--train-end", "2025-12-31"])
    assert args.command == "summarize"
    assert args.input == "foo.jsonl"
    assert args.train_end == "2025-12-31"


def test_run_build_writes_jsonl(tmp_path, monkeypatch):
    forecasts = _full_day_forecasts("2025-06-15", -5)
    cli_rows = [{"date_lst": "2025-06-15", "max_f": 80.0, "source": "test"}]

    monkeypatch.setattr(multi_model, "fetch_model_previous_runs", lambda *a, **k: forecasts)
    monkeypatch.setattr(multi_model, "fetch_cli_daily_highs", lambda *a, **k: cli_rows)

    output_path = tmp_path / "out" / "dataset.jsonl"
    rows = multi_model.run_build(
        "2025-06-15", "2025-06-15", ["KNYC"], str(output_path), model_ids=("gfs_seamless",)
    )

    assert len(rows) == 1
    assert output_path.exists()
    written = [json.loads(line) for line in output_path.read_text().splitlines()]
    assert written == rows
    assert rows[0]["models"]["gfs_seamless"]["forecast_max_lead1_f"] is not None


def test_run_build_skips_fetch_before_model_usable_start(monkeypatch):
    """A model with no coverage for the whole requested range should not be fetched."""
    calls = []

    def fake_fetch(model_id, *a, **k):
        calls.append(model_id)
        return []

    monkeypatch.setattr(multi_model, "fetch_model_previous_runs", fake_fetch)
    monkeypatch.setattr(multi_model, "fetch_cli_daily_highs", lambda *a, **k: [])

    multi_model.run_build(
        "2024-01-01", "2024-01-02", ["KNYC"], "/dev/null",
        model_ids=("ecmwf_aifs025_single", "gfs_seamless"),
    )
    # ecmwf_aifs025_single's usable start (2025-02-20) is after this range -> skipped.
    assert "ecmwf_aifs025_single" not in calls
    assert "gfs_seamless" in calls


def test_run_summarize_reads_jsonl(tmp_path):
    input_path = tmp_path / "dataset.jsonl"
    row = _row("2025-01-01", {"ncep_nbm_conus": _model_entry(50.0)}, 52.0, ensemble_mean_lead1=50.0)
    input_path.write_text(json.dumps(row) + "\n")
    summary = multi_model.run_summarize(str(input_path), "2025-12-31")
    assert summary["train"]["ncep_nbm_conus"]["lead1"]["n"] == 1
