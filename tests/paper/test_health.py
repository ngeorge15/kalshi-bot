"""Data-health check tests: staleness, watchlist eligibility, missing settlement."""
from datetime import datetime, timezone

import pytest

from src.paper.broker import PaperBroker
from src.paper.health import (forecast_staleness, health_report, quote_staleness,
                               unresolved_closed_markets, watchlist_eligibility)


@pytest.fixture
def broker(tmp_path):
    return PaperBroker(str(tmp_path / "paper.db"))


def quote(event_id="q0", second=0, ticker="TEMP", close_at="2026-09-04T18:00:00Z", **changes):
    timestamp = f"2026-09-04T12:00:{second:02d}Z"
    return {"event_id": event_id, "type": "quote", "at": timestamp,
            "observed_at": timestamp, "ticker": ticker,
            "market_type": "temperature", "event_key": "NYC-2026-09-04",
            "close_at": close_at, "available": True,
            "yes_asks": [[45, 5]], "no_asks": [[60, 20]], **changes}


def forecast(event_id="p1", second=1, ticker="TEMP", **changes):
    return {"event_id": event_id, "type": "forecast", "at": f"2026-09-04T12:00:{second:02d}Z",
            "ticker": ticker, "yes_probability": 0.6, "model_name": "weather", "model_version": "1", **changes}


def settlement(event_id="s1", ticker="TEMP", at_iso="2026-09-04T18:00:00Z", **changes):
    return {"event_id": event_id, "type": "settlement", "at": at_iso, "ticker": ticker, "result": "yes", **changes}


def at(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc)


# --- quote_staleness -------------------------------------------------------

def test_quote_staleness_ok_when_fresh(broker):
    broker.process(quote())
    result = quote_staleness(broker.db_path, at("2026-09-04T12:00:30Z"), 60)
    assert result["severity"] == "ok"
    assert result["items"] == []
    assert result["count"] == 0


def test_quote_staleness_warns_past_threshold(broker):
    broker.process(quote())
    result = quote_staleness(broker.db_path, at("2026-09-04T12:05:00Z"), 60)
    assert result["severity"] == "warn"
    assert result["count"] == 1
    assert result["items"][0]["ticker"] == "TEMP"
    assert result["items"][0]["reason"] == "stale_quote"


def test_quote_staleness_excludes_settled_markets(broker):
    broker.process(quote())
    broker.process(settlement())
    result = quote_staleness(broker.db_path, at("2026-09-05T00:00:00Z"), 60)
    assert result["severity"] == "ok"
    assert result["items"] == []


# --- forecast_staleness ------------------------------------------------

def test_forecast_staleness_ok_when_fresh(broker):
    broker.process(quote())
    broker.process(forecast())
    result = forecast_staleness(broker.db_path, at("2026-09-04T12:00:31Z"), 21_600)
    assert result["severity"] == "ok"
    assert result["items"] == []


def test_forecast_staleness_warns_past_threshold(broker):
    broker.process(quote())
    broker.process(forecast())
    result = forecast_staleness(broker.db_path, at("2026-09-04T18:00:02Z"), 21_600)
    assert result["severity"] == "warn"
    assert result["count"] == 1
    item = result["items"][0]
    assert item["ticker"] == "TEMP"
    assert item["model_name"] == "weather"
    assert item["reason"] == "stale_forecast"


def test_forecast_staleness_excludes_settled_markets(broker):
    broker.process(quote())
    broker.process(forecast())
    broker.process(settlement())
    result = forecast_staleness(broker.db_path, at("2026-09-05T00:00:00Z"), 21_600)
    assert result["severity"] == "ok"
    assert result["items"] == []


def test_forecast_staleness_ok_with_no_forecasts(broker):
    broker.process(quote())
    result = forecast_staleness(broker.db_path, at("2026-09-04T12:00:01Z"), 21_600)
    assert result["severity"] == "ok"


# --- watchlist_eligibility ---------------------------------------------

def eligible_entry(ticker="ELIGIBLE-TICKER", checked_at="2026-09-04T11:00:00Z",
                    expires_at="2026-09-04T13:00:00Z"):
    return {"ticker": ticker, "eligibility": {"available": True, "checked_at": checked_at,
            "expires_at": expires_at, "source": "reviewed"}}


def test_watchlist_eligibility_ok_when_all_eligible():
    result = watchlist_eligibility([eligible_entry()], at("2026-09-04T12:00:00Z"))
    assert result["severity"] == "ok"
    assert result["items"] == []


def test_watchlist_eligibility_warns_on_shipped_example_shape():
    # Mirrors examples/paper_watchlist.json: available false, placeholder
    # ticker. This is the expected default state, not a fail-worthy anomaly.
    entry = {"ticker": "REPLACE-WITH-REVIEWED-TEMPERATURE-TICKER", "eligibility": {
        "available": False, "checked_at": "2026-09-04T00:00:00Z",
        "expires_at": "2026-09-05T00:00:00Z", "source": "REPLACE-WITH-CURRENT-AVAILABILITY-REVIEW"}}
    result = watchlist_eligibility([entry], at("2026-09-04T12:00:00Z"))
    assert result["severity"] == "warn"
    assert result["items"][0]["reason"] == "not_available"


def test_watchlist_eligibility_warns_when_expired():
    entry = eligible_entry(expires_at="2026-09-04T11:59:00Z")
    result = watchlist_eligibility([entry], at("2026-09-04T12:00:00Z"))
    assert result["severity"] == "warn"
    assert result["items"][0]["reason"] == "expired"


def test_watchlist_eligibility_fails_on_missing_block():
    result = watchlist_eligibility([{"ticker": "NO-BLOCK"}], at("2026-09-04T12:00:00Z"))
    assert result["severity"] == "fail"
    assert result["items"][0]["reason"] == "eligibility_missing"


def test_watchlist_eligibility_fails_on_malformed_timestamps():
    entry = {"ticker": "BAD-TIMES", "eligibility": {"available": True, "checked_at": "not-a-time",
             "expires_at": "2026-09-05T00:00:00Z", "source": "x"}}
    result = watchlist_eligibility([entry], at("2026-09-04T12:00:00Z"))
    assert result["severity"] == "fail"
    assert result["items"][0]["reason"] == "malformed_eligibility_timestamps"


def test_watchlist_eligibility_empty_list_is_ok():
    result = watchlist_eligibility([], at("2026-09-04T12:00:00Z"))
    assert result["severity"] == "ok"


# --- unresolved_closed_markets -------------------------------------------

def test_unresolved_closed_markets_ok_when_still_open(broker):
    broker.process(quote(close_at="2026-09-04T18:00:00Z"))
    result = unresolved_closed_markets(broker.db_path, at("2026-09-04T12:00:01Z"))
    assert result["severity"] == "ok"
    assert result["items"] == []


def test_unresolved_closed_markets_fails_past_close_without_result(broker):
    broker.process(quote(close_at="2026-09-04T12:05:00Z"))
    result = unresolved_closed_markets(broker.db_path, at("2026-09-04T13:00:00Z"))
    assert result["severity"] == "fail"
    assert result["count"] == 1
    assert result["items"][0]["ticker"] == "TEMP"


def test_unresolved_closed_markets_ok_once_settled(broker):
    broker.process(quote(close_at="2026-09-04T12:05:00Z"))
    broker.process(settlement(at_iso="2026-09-04T12:05:01Z"))
    result = unresolved_closed_markets(broker.db_path, at("2026-09-04T13:00:00Z"))
    assert result["severity"] == "ok"
    assert result["items"] == []


# --- health_report -------------------------------------------------------

def test_health_report_ok_on_healthy_database(broker):
    broker.process(quote())
    broker.process(forecast())
    report = health_report(broker.db_path, [eligible_entry()], at("2026-09-04T12:00:05Z"), 60, 21_600)
    assert report["severity"] == "ok"
    for check in report["checks"].values():
        assert check["severity"] == "ok"


def test_health_report_reflects_worst_severity(broker):
    # Unresolved settlement (fail) alongside an otherwise-fine watchlist (ok)
    # and a fresh quote (ok): overall must be fail, not averaged away.
    broker.process(quote(close_at="2026-09-04T12:05:00Z"))
    report = health_report(broker.db_path, [eligible_entry()], at("2026-09-04T13:00:00Z"), 60, 21_600)
    assert report["severity"] == "fail"
    assert report["checks"]["unresolved_closed_markets"]["severity"] == "fail"


def test_health_report_accepts_open_connection(broker):
    import sqlite3

    broker.process(quote())
    conn = sqlite3.connect(broker.db_path)
    try:
        report = health_report(conn, [], at("2026-09-04T12:00:05Z"), 60, 21_600)
        assert report["severity"] == "ok"
    finally:
        conn.close()
