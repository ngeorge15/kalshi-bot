"""Watchlist authoring, validation, and review-aid tests."""
from datetime import date, datetime, timedelta, timezone
import json

import pytest

from src.data.weather.station_map import STATIONS
from src.paper.weather import predict_hourly_high
from src.paper.watchlist import (
    ERROR,
    WARNING,
    STATION_STANDARD_UTC_OFFSET_HOURS,
    bracket_coverage,
    explain,
    local_day_start,
    scaffold,
    validate,
)

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
TARGET_DATE = "2026-09-10"
KNYC = STATIONS["KNYC"]


def reviewed_entry(**overrides) -> dict:
    """A fully valid, reviewed entry -- no placeholders, no findings."""
    entry = {
        "ticker": "KXTEMPNYC-26SEP10-B60",
        "market_type": "temperature",
        "event_key": f"NYC-{TARGET_DATE}",
        "latitude": KNYC["lat"],
        "longitude": KNYC["lon"],
        "rules_source": "https://kalshi.com/rules/reviewed",
        "weather_spec": {"date": TARGET_DATE, "utc_offset_hours": STATION_STANDARD_UTC_OFFSET_HOURS["KNYC"],
                         "lower_bound_f": 60.0, "upper_bound_f": 70.0},
        "eligibility": {"available": True, "checked_at": (NOW - timedelta(hours=1)).isoformat(),
                        "expires_at": (NOW + timedelta(days=3)).isoformat(), "source": "reviewed-account-check"},
    }
    entry.update(overrides)
    return entry


def bracket_entry(ticker, lower, upper, event_key=f"NYC-{TARGET_DATE}", **overrides) -> dict:
    """A minimal entry shaped for bracket_coverage / explain (not full validate())."""
    entry = {"event_key": event_key, "ticker": ticker,
             "weather_spec": {"lower_bound_f": lower, "upper_bound_f": upper},
             "eligibility": {"available": True}}
    entry.update(overrides)
    return entry


def make_snapshot(start: datetime, issued_at: datetime, temperature: float = 65.0) -> dict:
    periods = [{"startTime": (start + timedelta(hours=h)).isoformat(),
                "endTime": (start + timedelta(hours=h + 1)).isoformat(),
                "temperature": temperature, "temperatureUnit": "F"} for h in range(24)]
    return {"source": "fixture-boundary-check", "issued_at": issued_at.isoformat(), "periods": periods}


# --- scaffold ----------------------------------------------------------------

def test_scaffold_station_case_insensitive():
    lower = scaffold("knyc", TARGET_DATE, [(60.0, 70.0)], NOW)
    upper = scaffold("KNYC", TARGET_DATE, [(60.0, 70.0)], NOW)
    assert lower[0]["latitude"] == upper[0]["latitude"] == KNYC["lat"]
    assert lower[0]["longitude"] == upper[0]["longitude"] == KNYC["lon"]


def test_scaffold_default_offset_uses_station_standard():
    entries = scaffold("KMDW", TARGET_DATE, [(60.0, 70.0)], NOW)
    assert entries[0]["weather_spec"]["utc_offset_hours"] == STATION_STANDARD_UTC_OFFSET_HOURS["KMDW"]


def test_scaffold_explicit_offset_overrides_default():
    entries = scaffold("KMDW", TARGET_DATE, [(60.0, 70.0)], NOW, utc_offset_hours=-5)
    assert entries[0]["weather_spec"]["utc_offset_hours"] == -5
    assert entries[0]["weather_spec"]["utc_offset_hours"] != STATION_STANDARD_UTC_OFFSET_HOURS["KMDW"]


@pytest.mark.parametrize("kwargs, match", [
    (dict(station_code="ZZZZ"), "Unknown station_code"),
    (dict(target_date="09-10-2026"), "target_date must be"),
    (dict(brackets=[]), "brackets must contain"),
    (dict(brackets=[(70.0, 60.0)]), "strictly less than"),
    (dict(brackets=[(None, None)]), "both lower_bound_f"),
    (dict(utc_offset_hours=99), "utc_offset_hours must be"),
    (dict(now=datetime(2026, 9, 1)), "timezone-aware"),
])
def test_scaffold_value_errors(kwargs, match):
    args = dict(station_code="KNYC", target_date=TARGET_DATE, brackets=[(60.0, 70.0)], now=NOW)
    args.update(kwargs)
    with pytest.raises(ValueError, match=match):
        scaffold(**args)


def test_scaffold_all_entries_start_unavailable():
    entries = scaffold("KNYC", TARGET_DATE, [(None, 60.0), (60.0, 70.0), (70.0, None)], NOW)
    assert all(e["eligibility"]["available"] is False for e in entries)


def test_scaffold_validate_round_trip_only_placeholder_and_expiry_findings():
    entries = scaffold("KNYC", TARGET_DATE, [(None, 60.0), (60.0, 70.0), (70.0, None)], NOW)
    result = validate(entries, NOW)
    assert not result.ok

    allowed_error_fields = {"ticker", "rules_source", "eligibility.source"}
    for finding in result.errors:
        assert finding.field in allowed_error_fields
    for finding in result.warnings:
        assert finding.field == "eligibility.expires_at"
        assert "expired" in finding.message

    assert len(result.errors) == 3 * len(entries)
    assert len(result.warnings) == len(entries)


# --- validate: structure --------------------------------------------------

def test_validate_rejects_non_list_watchlist():
    result = validate({"not": "a list"}, NOW)
    assert not result.ok
    assert result.errors[0].field == "<watchlist>"
    assert result.errors[0].index is None


def test_validate_rejects_non_dict_entry():
    result = validate([123], NOW)
    assert not result.ok
    assert result.errors[0].index == 0
    assert result.errors[0].field == "<entry>"


def test_validate_rejects_naive_now():
    with pytest.raises(ValueError, match="timezone-aware"):
        validate([], datetime(2026, 9, 1))


@pytest.mark.parametrize("key", ["ticker", "market_type", "event_key", "latitude", "longitude",
                                 "rules_source", "weather_spec", "eligibility"])
def test_validate_flags_missing_required_entry_key(key):
    entry = reviewed_entry()
    del entry[key]
    result = validate([entry], NOW)
    assert any(f.field == key and f.severity == ERROR and "missing required key" in f.message
              for f in result.findings)


@pytest.mark.parametrize("key", ["date", "utc_offset_hours"])
def test_validate_flags_missing_weather_spec_key(key):
    entry = reviewed_entry()
    del entry["weather_spec"][key]
    result = validate([entry], NOW)
    assert any(f.field == f"weather_spec.{key}" and f.severity == ERROR for f in result.findings)


@pytest.mark.parametrize("key", ["available", "checked_at", "expires_at", "source"])
def test_validate_flags_missing_eligibility_key(key):
    entry = reviewed_entry()
    del entry["eligibility"][key]
    result = validate([entry], NOW)
    assert any(f.field == f"eligibility.{key}" and f.severity == ERROR for f in result.findings)


def test_validate_flags_placeholder_in_nested_field():
    entry = reviewed_entry()
    entry["eligibility"]["source"] = "REPLACE-ME"
    result = validate([entry], NOW)
    assert any(f.field == "eligibility.source" and "placeholder value" in f.message for f in result.errors)


@pytest.mark.parametrize("mutate, field", [
    (lambda e: e.update(latitude=True), "latitude"),
    (lambda e: e.update(longitude=True), "longitude"),
    (lambda e: e["weather_spec"].update(lower_bound_f=True), "lower_bound_f"),
    (lambda e: e["weather_spec"].update(upper_bound_f=True), "upper_bound_f"),
    (lambda e: e["weather_spec"].update(utc_offset_hours=True), "utc_offset_hours"),
    (lambda e: e.update(latitude=float("nan")), "latitude"),
    (lambda e: e.update(longitude=float("inf")), "longitude"),
    (lambda e: e["weather_spec"].update(lower_bound_f=float("nan")), "lower_bound_f"),
    (lambda e: e["weather_spec"].update(upper_bound_f=float("inf")), "upper_bound_f"),
    (lambda e: e.update(latitude=91.0), "latitude"),
    (lambda e: e.update(latitude=-91.0), "latitude"),
    (lambda e: e.update(longitude=181.0), "longitude"),
    (lambda e: e.update(longitude=-181.0), "longitude"),
])
def test_validate_rejects_bad_numeric_fields(mutate, field):
    entry = reviewed_entry()
    mutate(entry)
    result = validate([entry], NOW)
    assert not result.ok
    assert any(field in f.field for f in result.errors)


def test_validate_coordinate_disagreement_warns():
    entry = reviewed_entry(latitude=KNYC["lat"] + 1.0)
    result = validate([entry], NOW)
    assert any(f.severity == WARNING and f.field == "latitude" and "disagrees" in f.message
              for f in result.findings)


def test_validate_custom_city_does_not_warn():
    entry = reviewed_entry(event_key=f"CUSTOMTOWN-{TARGET_DATE}", latitude=41.0, longitude=-75.0)
    result = validate([entry], NOW)
    assert not any(f.field == "latitude" for f in result.findings)


def test_validate_duplicate_ticker_flags_both_indices():
    entry_a = reviewed_entry()
    entry_b = reviewed_entry()
    result = validate([entry_a, entry_b], NOW)
    dupes = [f for f in result.errors if f.field == "ticker" and "Duplicate" in f.message]
    assert {f.index for f in dupes} == {0, 1}
    for f in dupes:
        other = 1 - f.index
        assert str(other) in f.message


def test_validate_unsupported_market_type_is_error():
    entry = reviewed_entry(market_type="games")
    result = validate([entry], NOW)
    assert any(f.field == "market_type" and "not supported" in f.message for f in result.errors)


def test_validate_empty_market_type_is_error_not_unsupported():
    entry = reviewed_entry(market_type="")
    result = validate([entry], NOW)
    assert any(f.field == "market_type" and "nonempty string" in f.message for f in result.errors)
    assert not any(f.field == "market_type" and "not supported" in f.message for f in result.errors)


def test_validate_bad_weather_spec_date_is_error():
    entry = reviewed_entry()
    entry["weather_spec"]["date"] = "10-09-2026"
    result = validate([entry], NOW)
    assert any(f.field == "weather_spec.date" and "ISO 8601 date" in f.message for f in result.errors)


def test_validate_event_key_date_mismatch_is_error():
    entry = reviewed_entry(event_key="NYC-2026-09-11")
    result = validate([entry], NOW)
    assert any(f.field == "weather_spec.date" and "must agree" in f.message for f in result.errors)


def test_validate_eligibility_non_bool_available_is_error():
    entry = reviewed_entry()
    entry["eligibility"]["available"] = "yes"
    result = validate([entry], NOW)
    assert any(f.field == "eligibility.available" for f in result.errors)


@pytest.mark.parametrize("field", ["checked_at", "expires_at"])
@pytest.mark.parametrize("bad_value", ["2026-09-01T00:00:00", "not-a-timestamp"])
def test_validate_eligibility_naive_or_garbage_timestamps(field, bad_value):
    entry = reviewed_entry()
    entry["eligibility"][field] = bad_value
    result = validate([entry], NOW)
    assert any(f.field == f"eligibility.{field}" for f in result.errors)


def test_validate_checked_at_after_expires_at_is_error():
    entry = reviewed_entry()
    entry["eligibility"]["checked_at"] = (NOW + timedelta(days=3)).isoformat()
    entry["eligibility"]["expires_at"] = (NOW - timedelta(hours=1)).isoformat()
    result = validate([entry], NOW)
    assert any(f.field == "eligibility.checked_at" and "is after" in f.message for f in result.errors)


def test_validate_expired_eligibility_is_warning_not_error():
    entry = reviewed_entry()
    entry["eligibility"]["checked_at"] = (NOW - timedelta(hours=2)).isoformat()
    entry["eligibility"]["expires_at"] = (NOW - timedelta(hours=1)).isoformat()
    result = validate([entry], NOW)
    assert result.ok
    assert any(f.severity == WARNING and f.field == "eligibility.expires_at" and "expired" in f.message
              for f in result.findings)


def test_fully_valid_reviewed_entry_has_no_findings():
    result = validate([reviewed_entry()], NOW)
    assert result.ok
    assert result.findings == []


# --- boundary agreement with predict_hourly_high ------------------------------

def test_validate_and_predict_agree_on_the_target_day_boundary():
    offset = STATION_STANDARD_UTC_OFFSET_HOURS["KNYC"]
    target = date.fromisoformat(TARGET_DATE)
    start = local_day_start(target, offset)
    spec = {"date": TARGET_DATE, "utc_offset_hours": offset, "lower_bound_f": 60.0, "upper_bound_f": 70.0}
    entry = reviewed_entry(weather_spec=spec)

    just_before = start - timedelta(seconds=1)
    result_before = validate([entry], just_before)
    assert not any(f.field == "weather_spec.date" for f in result_before.errors)

    result_at_start = validate([entry], start)
    assert any(f.field == "weather_spec.date" and "already started" in f.message
              for f in result_at_start.errors)

    snapshot = make_snapshot(start, issued_at=just_before - timedelta(minutes=1))
    ok = predict_hourly_high(snapshot, spec, just_before, sigma_f=5, max_age_seconds=21600)
    assert ok["mean_high_f"] == 65.0
    with pytest.raises(ValueError, match="before the target day starts"):
        predict_hourly_high(snapshot, spec, start, sigma_f=5, max_age_seconds=21600)


# --- bracket_coverage ---------------------------------------------------------

def test_bracket_coverage_clean_partition():
    entries = [bracket_entry("a", None, 60.0), bracket_entry("b", 60.0, 70.0), bracket_entry("c", 70.0, None)]
    report = bracket_coverage(entries)[f"NYC-{TARGET_DATE}"]
    assert report == {"n_brackets": 3, "gaps": [], "overlaps": [],
                       "has_open_lower_tail": True, "has_open_upper_tail": True, "partitions": True}


def test_bracket_coverage_gap():
    entries = [bracket_entry("a", None, 60.0), bracket_entry("b", 70.0, None)]
    report = bracket_coverage(entries)[f"NYC-{TARGET_DATE}"]
    assert report["gaps"] == [{"from_f": 60.0, "to_f": 70.0}]
    assert report["overlaps"] == []
    assert report["partitions"] is False


def test_bracket_coverage_overlap():
    entries = [bracket_entry("a", 60.0, 80.0), bracket_entry("b", 70.0, 90.0)]
    report = bracket_coverage(entries)[f"NYC-{TARGET_DATE}"]
    assert report["overlaps"] == [{"tickers": ["a", "b"], "from_f": 70.0, "to_f": 80.0}]
    assert report["gaps"] == []


def test_bracket_coverage_touching_bounds_are_neither_gap_nor_overlap():
    entries = [bracket_entry("a", 60.0, 70.0), bracket_entry("b", 70.0, 80.0)]
    report = bracket_coverage(entries)[f"NYC-{TARGET_DATE}"]
    assert report["gaps"] == []
    assert report["overlaps"] == []


def test_bracket_coverage_nonadjacent_bracket_covers_a_would_be_gap():
    # Regression for the sorted-neighbours bug: [60,80) covers the 70-75
    # stretch a pairwise check would have called a gap, and it overlaps both
    # its non-adjacent neighbours, not just the one next to it once sorted.
    entries = [bracket_entry("t1", 60.0, 80.0), bracket_entry("t2", 65.0, 70.0), bracket_entry("t3", 75.0, 90.0)]
    report = bracket_coverage(entries)[f"NYC-{TARGET_DATE}"]
    assert report["gaps"] == []
    assert report["overlaps"] == [
        {"tickers": ["t1", "t2"], "from_f": 65.0, "to_f": 70.0},
        {"tickers": ["t1", "t3"], "from_f": 75.0, "to_f": 80.0},
    ]


def test_bracket_coverage_open_upper_tail_not_sorted_last():
    # Regression: an open-upper-tail bracket [70, None) sorts before [80,90)
    # by lower bound, so it is not the "last" bracket -- the old first/last
    # check missed both the tail and the overlap.
    entries = [bracket_entry("t1", 70.0, None), bracket_entry("t2", 80.0, 90.0)]
    report = bracket_coverage(entries)[f"NYC-{TARGET_DATE}"]
    assert report["has_open_upper_tail"] is True
    assert report["overlaps"] == [{"tickers": ["t1", "t2"], "from_f": 80.0, "to_f": 90.0}]


def test_bracket_coverage_two_open_lower_tails_overlap():
    # Regression: two open-lower-tail brackets are adjacent once sorted (both
    # sort first), but the old code skipped any pair touching an open tail.
    entries = [bracket_entry("a", None, 70.0), bracket_entry("b", None, 60.0)]
    report = bracket_coverage(entries)[f"NYC-{TARGET_DATE}"]
    assert report["has_open_lower_tail"] is True
    assert report["overlaps"] == [{"tickers": ["b", "a"], "from_f": None, "to_f": 60.0}]


def test_bracket_coverage_missing_tails_does_not_partition():
    entries = [bracket_entry("a", 60.0, 70.0)]
    report = bracket_coverage(entries)[f"NYC-{TARGET_DATE}"]
    assert report["gaps"] == []
    assert report["overlaps"] == []
    assert report["has_open_lower_tail"] is False
    assert report["has_open_upper_tail"] is False
    assert report["partitions"] is False


def test_bracket_coverage_keeps_multiple_events_separate():
    entries = [bracket_entry("a", None, 60.0, event_key="EV1"), bracket_entry("b", 60.0, None, event_key="EV1"),
               bracket_entry("c", 0.0, 10.0, event_key="EV2")]
    report = bracket_coverage(entries)
    assert report["EV1"]["partitions"] is True
    assert report["EV2"]["partitions"] is False
    assert report["EV1"]["n_brackets"] == 2
    assert report["EV2"]["n_brackets"] == 1


def test_bracket_coverage_skips_invalid_entries():
    entries = [None, {"event_key": "EV"}, {"event_key": "EV", "weather_spec": {}},
               {"event_key": "EV", "weather_spec": {"lower_bound_f": None, "upper_bound_f": None}},
               {"weather_spec": {"lower_bound_f": 0.0, "upper_bound_f": 1.0}},
               bracket_entry("a", None, 60.0, event_key="EV"), bracket_entry("b", 60.0, None, event_key="EV")]
    report = bracket_coverage(entries)
    assert list(report) == ["EV"]
    assert report["EV"]["n_brackets"] == 2
    assert report["EV"]["partitions"] is True


def test_bracket_coverage_json_safe():
    entries = [bracket_entry("a", None, 60.0), bracket_entry("b", 65.0, None), bracket_entry("c", 70.0, None)]
    report = bracket_coverage(entries)
    event_report = report[f"NYC-{TARGET_DATE}"]
    assert event_report["gaps"] == [{"from_f": 60.0, "to_f": 65.0}]
    assert event_report["overlaps"] == [{"tickers": ["b", "c"], "from_f": 70.0, "to_f": None}]
    json.dumps(report, allow_nan=False)


# --- explain -------------------------------------------------------------

def test_explain_empty_or_non_list():
    assert explain([]).startswith("Watchlist is empty")
    assert explain("nope").startswith("Watchlist is empty")


def test_explain_lists_brackets_sorted_and_uses_singular_plural_wording():
    entries = [bracket_entry("b", 60.0, 70.0), bracket_entry("a", None, 60.0)]
    text = explain(entries)
    assert text.startswith(f"2 entries across 1 event(s):")
    assert text.index("ticker=a") < text.index("ticker=b")

    single = explain([bracket_entry("a", None, 60.0)])
    assert single.startswith("1 entry across 1 event(s):")


def test_explain_flags_placeholders_and_not_available():
    entries = scaffold("KNYC", TARGET_DATE, [(60.0, 70.0)], NOW)
    text = explain(entries)
    assert "needs human input" in text
    assert "ticker" in text
    assert "rules_source" in text
    assert "eligibility.source" in text
    assert "eligibility (not marked available)" in text


def test_explain_coverage_verdict_lines():
    partitioning = [bracket_entry("a", None, 60.0), bracket_entry("b", 60.0, None)]
    assert "coverage: brackets partition the real line cleanly" in explain(partitioning)

    gap_only = [bracket_entry("a", None, 60.0), bracket_entry("b", 70.0, None)]
    text = explain(gap_only)
    assert "coverage: DOES NOT partition the real line" in text
    assert "1 gap(s)" in text
