"""Tests for src/research/watchlist_gen.py -- watchlist generation from live Kalshi data.

No real network calls: `fetch_event_markets` is monkeypatched to return fixture
payloads shaped like the real KXHIGHNY event documented in
`research/kalshi-public-data.md` (6 brackets: one open lower tail, four 2F-wide
"between" brackets, one open upper tail).
"""
from datetime import datetime, timedelta, timezone

import pytest

from src.data.weather.station_map import STATIONS
from src.paper.watchlist import STATION_STANDARD_UTC_OFFSET_HOURS, validate
from src.research import watchlist_gen as wg

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
TARGET_DATE = "2026-09-18"

WEATHER_COMPANY_RULES = (
    "If the maximum temperature recorded at New York City (CLINYC) for Sep 18, 2026, is between "
    "81-82 fahrenheit according to The Weather Company, then the market resolves to Yes."
)


def _raw_bracket(ticker, strike_type, floor_strike=None, cap_strike=None, sub_title=None,
                 event_ticker=f"KXHIGHNY-26SEP18", settlement_url="https://weather.com/kalshi",
                 rules_primary=WEATHER_COMPANY_RULES, status="active", result=None,
                 close_time="2026-09-19T05:00:00Z"):
    return {
        "ticker": ticker,
        "event_ticker": event_ticker,
        "strike_type": strike_type,
        "floor_strike": floor_strike,
        "cap_strike": cap_strike,
        "yes_sub_title": sub_title,
        "status": status,
        "result": result,
        "close_time": close_time,
        "rules_primary": rules_primary,
        "settlement_sources": [{"name": "The Weather Company", "url": settlement_url}] if settlement_url else [],
    }


def nyc_ladder(event_ticker="KXHIGHNY-26SEP18"):
    """The documented 6-bracket ladder for one event: contiguous, both tails open.

    Ticker prefix is derived from `event_ticker` so each series/station gets its
    own distinct tickers, exactly as the real API would.
    """
    return [
        _raw_bracket(f"{event_ticker}-T75", "less", cap_strike=75, sub_title="74° or below",
                    event_ticker=event_ticker),
        _raw_bracket(f"{event_ticker}-B75.5", "between", floor_strike=75, cap_strike=76,
                    sub_title="75° to 76°", event_ticker=event_ticker),
        _raw_bracket(f"{event_ticker}-B77.5", "between", floor_strike=77, cap_strike=78,
                    sub_title="77° to 78°", event_ticker=event_ticker),
        _raw_bracket(f"{event_ticker}-B79.5", "between", floor_strike=79, cap_strike=80,
                    sub_title="79° to 80°", event_ticker=event_ticker),
        _raw_bracket(f"{event_ticker}-B81.5", "between", floor_strike=81, cap_strike=82,
                    sub_title="81° to 82°", event_ticker=event_ticker),
        _raw_bracket(f"{event_ticker}-T82", "greater", floor_strike=82, sub_title="83° or above",
                    event_ticker=event_ticker),
    ]


@pytest.fixture(autouse=True)
def stub_fetch(monkeypatch):
    """By default, every station's fetch returns a ladder shaped like the real NYC one,
    with tickers/event_ticker derived from that station's own series so multi-station
    tests never collide on ticker names."""
    def _fetch(series, target_date, session=None, cutoff=None):
        from src.data.kalshi_history import event_ticker_for_date
        event_ticker = event_ticker_for_date(series, target_date)
        return nyc_ladder(event_ticker=event_ticker)

    monkeypatch.setattr(wg, "fetch_event_markets", _fetch)
    return _fetch


# --- build_watchlist: happy path ---------------------------------------------

def test_build_watchlist_one_station_six_brackets():
    entries = wg.build_watchlist(["KNYC"], TARGET_DATE, NOW)
    assert len(entries) == 6
    tickers = [e["ticker"] for e in entries]
    assert tickers == [
        "KXHIGHNY-26SEP18-T75", "KXHIGHNY-26SEP18-B75.5", "KXHIGHNY-26SEP18-B77.5",
        "KXHIGHNY-26SEP18-B79.5", "KXHIGHNY-26SEP18-B81.5", "KXHIGHNY-26SEP18-T82",
    ]


def test_build_watchlist_bounds_match_documented_mapping():
    [entry] = [e for e in wg.build_watchlist(["KNYC"], TARGET_DATE, NOW)
              if e["ticker"] == "KXHIGHNY-26SEP18-B81.5"]
    assert entry["weather_spec"]["lower_bound_f"] == 80.5
    assert entry["weather_spec"]["upper_bound_f"] == 82.5


def test_build_watchlist_open_tails():
    entries = wg.build_watchlist(["KNYC"], TARGET_DATE, NOW)
    lower_tail = next(e for e in entries if e["ticker"].endswith("-T75"))
    upper_tail = next(e for e in entries if e["ticker"].endswith("-T82"))
    assert lower_tail["weather_spec"]["lower_bound_f"] is None
    assert lower_tail["weather_spec"]["upper_bound_f"] == 74.5
    assert upper_tail["weather_spec"]["lower_bound_f"] == 82.5
    assert upper_tail["weather_spec"]["upper_bound_f"] is None


def test_build_watchlist_uses_real_settlement_source_url():
    entries = wg.build_watchlist(["KNYC"], TARGET_DATE, NOW)
    assert all(e["rules_source"] == "https://weather.com/kalshi" for e in entries)


def test_build_watchlist_falls_back_to_rules_primary_when_no_url(monkeypatch):
    def _fetch(series, target_date, session=None, cutoff=None):
        return [_raw_bracket("KXHIGHNY-26SEP18-B81.5", "between", floor_strike=81, cap_strike=82,
                             sub_title="81° to 82°", settlement_url=None)]

    monkeypatch.setattr(wg, "fetch_event_markets", _fetch)
    [entry] = wg.build_watchlist(["KNYC"], TARGET_DATE, NOW)
    assert entry["rules_source"] == WEATHER_COMPANY_RULES


def test_build_watchlist_no_url_no_rules_primary_raises(monkeypatch):
    def _fetch(series, target_date, session=None, cutoff=None):
        return [_raw_bracket("KXHIGHNY-26SEP18-B81.5", "between", floor_strike=81, cap_strike=82,
                             sub_title="81° to 82°", settlement_url=None, rules_primary=None)]

    monkeypatch.setattr(wg, "fetch_event_markets", _fetch)
    with pytest.raises(ValueError, match="rules_source"):
        wg.build_watchlist(["KNYC"], TARGET_DATE, NOW)


def test_build_watchlist_coordinates_and_event_key():
    [entry, *_] = wg.build_watchlist(["KNYC"], TARGET_DATE, NOW)
    knyc = STATIONS["KNYC"]
    assert entry["latitude"] == knyc["lat"]
    assert entry["longitude"] == knyc["lon"]
    assert entry["event_key"] == f"NYC-{TARGET_DATE}"


def test_build_watchlist_weather_spec_offset_and_date():
    [entry, *_] = wg.build_watchlist(["KNYC"], TARGET_DATE, NOW)
    assert entry["weather_spec"]["date"] == TARGET_DATE
    assert entry["weather_spec"]["utc_offset_hours"] == STATION_STANDARD_UTC_OFFSET_HOURS["KNYC"]


def test_build_watchlist_market_type_is_temperature():
    entries = wg.build_watchlist(["KNYC"], TARGET_DATE, NOW)
    assert all(e["market_type"] == "temperature" for e in entries)


def test_build_watchlist_multiple_stations():
    entries = wg.build_watchlist(["KNYC", "KMDW"], TARGET_DATE, NOW)
    assert len(entries) == 12
    event_keys = {e["event_key"] for e in entries}
    assert event_keys == {f"NYC-{TARGET_DATE}", f"CHI-{TARGET_DATE}"}


def test_build_watchlist_station_code_case_insensitive():
    entries = wg.build_watchlist(["knyc"], TARGET_DATE, NOW)
    assert entries[0]["latitude"] == STATIONS["KNYC"]["lat"]


# --- eligibility stance -------------------------------------------------------

def test_default_not_available():
    entries = wg.build_watchlist(["KNYC"], TARGET_DATE, NOW)
    assert all(e["eligibility"]["available"] is False for e in entries)


def test_available_true_requires_source():
    with pytest.raises(ValueError, match="eligibility_source"):
        wg.build_watchlist(["KNYC"], TARGET_DATE, NOW, available=True)


def test_available_true_rejects_blank_source():
    with pytest.raises(ValueError, match="eligibility_source"):
        wg.build_watchlist(["KNYC"], TARGET_DATE, NOW, available=True, eligibility_source="   ")


def test_available_true_with_source_marks_entries_available():
    entries = wg.build_watchlist(["KNYC"], TARGET_DATE, NOW, available=True,
                                 eligibility_source="reviewed by hand, 2026-09-17")
    assert all(e["eligibility"]["available"] is True for e in entries)
    assert all(e["eligibility"]["source"] == "reviewed by hand, 2026-09-17" for e in entries)


def test_naive_now_rejected():
    with pytest.raises(ValueError, match="timezone-aware"):
        wg.build_watchlist(["KNYC"], TARGET_DATE, datetime(2026, 9, 17, 12, 0))


# --- validate() round trip ----------------------------------------------------

def test_available_false_validates_with_only_eligibility_warning():
    entries = wg.build_watchlist(["KNYC", "KMDW", "KMIA", "KAUS"], TARGET_DATE, NOW)
    result = validate(entries, NOW)
    assert result.ok  # no errors
    assert len(result.warnings) == len(entries)
    for finding in result.warnings:
        assert finding.field == "eligibility.expires_at"
        assert "expired" in finding.message


def test_available_true_validates_with_zero_findings():
    entries = wg.build_watchlist(["KNYC", "KMDW", "KMIA", "KAUS"], TARGET_DATE, NOW,
                                 available=True, eligibility_source="reviewed by hand, 2026-09-17")
    result = validate(entries, NOW)
    assert result.ok
    assert result.errors == []
    assert result.warnings == []


def test_validate_still_rejects_started_target_day():
    entries = wg.build_watchlist(["KNYC"], TARGET_DATE, NOW, available=True,
                                 eligibility_source="reviewed by hand")
    later = NOW + timedelta(days=2)  # now past the 2026-09-18 local day start
    result = validate(entries, later)
    assert not result.ok


# --- errors --------------------------------------------------------------

def test_unknown_station_raises():
    with pytest.raises(ValueError, match="Unknown station code"):
        wg.build_watchlist(["ZZZZ"], TARGET_DATE, NOW)


def test_no_stations_raises():
    with pytest.raises(ValueError, match="at least one station"):
        wg.build_watchlist([], TARGET_DATE, NOW)


def test_empty_event_raises_clear_message(monkeypatch):
    monkeypatch.setattr(wg, "fetch_event_markets", lambda series, target_date, session=None, cutoff=None: [])
    with pytest.raises(ValueError, match="No live markets found"):
        wg.build_watchlist(["KNYC"], TARGET_DATE, NOW)


# --- coverage_summary ----------------------------------------------------

def test_coverage_summary_partitions_the_full_ladder():
    entries = wg.build_watchlist(["KNYC"], TARGET_DATE, NOW)
    summary = wg.coverage_summary(entries)
    [(event_key, report)] = summary.items()
    assert event_key == f"NYC-{TARGET_DATE}"
    assert report["partitions"] is True
    assert report["gaps"] == []
    assert report["overlaps"] == []


def test_coverage_summary_detects_missing_bracket():
    entries = wg.build_watchlist(["KNYC"], TARGET_DATE, NOW)
    del entries[2]  # drop a middle "between" bracket -> creates a real gap
    summary = wg.coverage_summary(entries)
    report = summary[f"NYC-{TARGET_DATE}"]
    assert report["partitions"] is False
    assert report["gaps"]
