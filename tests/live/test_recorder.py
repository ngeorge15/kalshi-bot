"""Read-only recorder: URL allowlist, orderbook-ladder conversion, and the record loop.

Everything here uses a fake session (no real sockets) and a fake clock/sleep
(no real waiting), per the project's rule that unit tests never touch the
network.
"""
import json
from datetime import date, datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
import requests

from src.live.recorder import (
    BASE_URL,
    MIN_INTERVAL_SECONDS,
    REQUEST_TIMEOUT,
    _get,
    _get_session,
    orderbook_to_ladders,
    poll_once,
    record,
)


# --- fakes -------------------------------------------------------------------

class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class FakeSession:
    """Keyed purely by URL (params/timeout recorded for assertions, not matched)."""

    def __init__(self, responses=None, raises=None):
        self.responses = responses or {}
        self.raises = raises or {}
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        if url in self.raises:
            raise self.raises[url]
        return FakeResponse(self.responses[url])


def raw_market(ticker="KXHIGHNY-26SEP19-B80.5", event_ticker="KXHIGHNY-26SEP19"):
    return {
        "ticker": ticker,
        "event_ticker": event_ticker,
        "strike_type": "between",
        "floor_strike": 80,
        "cap_strike": 81,
        "yes_sub_title": "80° to 81°",
        "status": "active",
        "close_time": "2026-09-20T03:59:00Z",
        "result": None,
        "rules_primary": "Settled using data from The Weather Company.",
    }


# --- URL allowlist -------------------------------------------------------------

def test_url_allowlist_rejects_order_path():
    session = Mock()
    with pytest.raises(ValueError):
        _get(session, f"{BASE_URL}/portfolio/orders")
    session.get.assert_not_called()


def test_url_allowlist_rejects_order_placement_path():
    session = Mock()
    with pytest.raises(ValueError):
        _get(session, f"{BASE_URL}/portfolio/orders", params={"ticker": "X"})
    session.get.assert_not_called()


@pytest.mark.parametrize("url", [
    f"{BASE_URL}/markets",
    f"{BASE_URL}/markets/KXHIGHNY-26SEP19-B80.5",
    f"{BASE_URL}/markets/KXHIGHNY-26SEP19-B80.5/orderbook",
    f"{BASE_URL}/events",
    f"{BASE_URL}/events/KXHIGHNY-26SEP19",
])
def test_url_allowlist_accepts_read_only_paths(url):
    session = Mock()
    session.get.return_value = Mock(raise_for_status=lambda: None)
    _get(session, url, params={"a": "b"})
    session.get.assert_called_once_with(url, params={"a": "b"}, timeout=REQUEST_TIMEOUT)


# --- retry / timeout configuration ---------------------------------------------

def test_session_retries_only_get_on_429_and_5xx():
    session = _get_session()
    adapter = session.get_adapter("https://external-api.kalshi.com")
    retry = adapter.max_retries
    assert retry.total == 3
    assert set(retry.status_forcelist) == {429, 500, 502, 503, 504}
    assert set(retry.allowed_methods) == {"GET"}


def test_request_timeout_is_connect_then_read():
    assert REQUEST_TIMEOUT == (5, 30)


# --- orderbook ladder conversion ------------------------------------------------

def test_two_sided_book_converts_bids_directly_and_asks_by_complement():
    payload = {"orderbook_fp": {"yes_dollars": [["0.555", "3.9"]], "no_dollars": [["0.40", "2.99"]]}}
    yes_bids, yes_asks = orderbook_to_ladders(payload)
    # price floors down, size floors down -- never flatters a simulated fill.
    assert yes_bids == [[55, 3]]
    # complement rounds the ask price up, size still floors down.
    assert yes_asks == [[60, 2]]


def test_empty_sides_produce_empty_lists_not_errors():
    payload = {"orderbook_fp": {"yes_dollars": [], "no_dollars": [["0.10", "2"]]}}
    yes_bids, yes_asks = orderbook_to_ladders(payload)
    assert yes_bids == []
    assert yes_asks == [[90, 2]]


def test_one_sided_book_missing_key_entirely():
    payload = {"orderbook_fp": {"yes_dollars": [["0.20", "5"]]}}  # no "no_dollars" key at all
    yes_bids, yes_asks = orderbook_to_ladders(payload)
    assert yes_bids == [[20, 5]]
    assert yes_asks == []


def test_missing_orderbook_fp_entirely_is_empty_not_an_error():
    assert orderbook_to_ladders({}) == ([], [])


def test_malformed_levels_are_skipped_not_raised():
    payload = {
        "orderbook_fp": {
            "yes_dollars": [["not-a-price", "1"], ["0.20", "5"]],
            "no_dollars": [["0.90", "-3"], ["0.10", "2"]],  # negative size skipped
        }
    }
    yes_bids, yes_asks = orderbook_to_ladders(payload)
    assert yes_bids == [[20, 5]]
    assert yes_asks == [[90, 2]]


def test_duplicate_price_levels_are_summed():
    payload = {"orderbook_fp": {"yes_dollars": [["0.30", "4"], ["0.30", "6"]], "no_dollars": []}}
    yes_bids, _ = orderbook_to_ladders(payload)
    assert yes_bids == [[30, 10]]


def test_bids_sorted_descending_asks_sorted_ascending():
    payload = {
        "orderbook_fp": {
            "yes_dollars": [["0.30", "1"], ["0.50", "1"], ["0.10", "1"]],
            "no_dollars": [["0.80", "1"], ["0.60", "1"]],
        }
    }
    yes_bids, yes_asks = orderbook_to_ladders(payload)
    assert [p for p, _ in yes_bids] == sorted((p for p, _ in yes_bids), reverse=True)
    assert [p for p, _ in yes_asks] == sorted(p for p, _ in yes_asks)


# --- poll_once -----------------------------------------------------------------

FIXED_CLOCK = lambda: datetime(2026, 9, 19, 4, 5, 6, tzinfo=timezone.utc)  # noqa: E731


def test_snapshot_shape_is_exact():
    ticker = "KXHIGHNY-26SEP19-B80.5"
    session = FakeSession(responses={
        f"{BASE_URL}/markets": {"markets": [raw_market(ticker)]},
        f"{BASE_URL}/markets/{ticker}/orderbook": {
            "orderbook_fp": {"yes_dollars": [["0.30", "10"]], "no_dollars": [["0.65", "5"]]}
        },
    })
    stats = {}
    snapshots = poll_once(["KNYC"], date(2026, 9, 19), session=session, stats=stats, clock=FIXED_CLOCK)
    assert snapshots == [{
        "captured_utc": "2026-09-19T04:05:06Z",
        "received_utc": "2026-09-19T04:05:06Z",
        "source": "rest_poll",
        "event_ticker": "KXHIGHNY-26SEP19",
        "ticker": ticker,
        "station": "KNYC",
        "date_lst": "2026-09-19",
        "lower_bound_f": 79.5,
        "upper_bound_f": 81.5,
        "yes_bids": [[30, 10]],
        "yes_asks": [[35, 5]],
        "status": "active",
        "close_time": "2026-09-20T03:59:00Z",
    }]
    assert stats == {"captured": 1, "skipped": 0}


def test_unknown_station_is_skipped_and_counted():
    stats = {}
    snapshots = poll_once(["ZZZZ"], date(2026, 9, 19), session=FakeSession(), stats=stats)
    assert snapshots == []
    assert stats == {"captured": 0, "skipped": 1}


def test_market_fetch_failure_skips_the_station_not_the_run():
    session = FakeSession(raises={f"{BASE_URL}/markets": requests.ConnectionError("down")})
    stats = {}
    snapshots = poll_once(["KNYC"], date(2026, 9, 19), session=session, stats=stats)
    assert snapshots == []
    assert stats == {"captured": 0, "skipped": 1}


def test_malformed_market_is_skipped_good_market_still_captured():
    good_ticker = "KXHIGHNY-26SEP19-B80.5"
    bad_market = {"ticker": "BAD", "status": "active"}  # missing event_ticker -> KeyError in parse_market
    session = FakeSession(responses={
        f"{BASE_URL}/markets": {"markets": [bad_market, raw_market(good_ticker)]},
        f"{BASE_URL}/markets/{good_ticker}/orderbook": {"orderbook_fp": {"yes_dollars": [], "no_dollars": []}},
    })
    stats = {}
    snapshots = poll_once(["KNYC"], date(2026, 9, 19), session=session, stats=stats)
    assert [s["ticker"] for s in snapshots] == [good_ticker]
    assert stats == {"captured": 1, "skipped": 1}


def test_orderbook_fetch_failure_is_skipped_not_raised():
    ticker = "KXHIGHNY-26SEP19-B80.5"
    session = FakeSession(
        responses={f"{BASE_URL}/markets": {"markets": [raw_market(ticker)]}},
        raises={f"{BASE_URL}/markets/{ticker}/orderbook": requests.Timeout("slow")},
    )
    stats = {}
    snapshots = poll_once(["KNYC"], date(2026, 9, 19), session=session, stats=stats)
    assert snapshots == []
    assert stats == {"captured": 0, "skipped": 1}


# --- record loop -----------------------------------------------------------------

class FakeClock:
    """Advances itself when `sleep` is called, so `record`'s loop is deterministic."""

    def __init__(self, start):
        self.now = start

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += timedelta(seconds=seconds)


def test_interval_below_minimum_is_refused(tmp_path):
    assert MIN_INTERVAL_SECONDS == 5
    with pytest.raises(ValueError):
        record(["KNYC"], date(2026, 9, 19), tmp_path / "out.jsonl", 4,
               until=datetime(2026, 9, 19, tzinfo=timezone.utc))
    assert not (tmp_path / "out.jsonl").exists()


def test_record_stops_at_until_and_sleeps_the_remainder(tmp_path):
    clock = FakeClock(datetime(2026, 9, 19, 0, 0, 0, tzinfo=timezone.utc))
    until = clock.now + timedelta(seconds=40)
    out = tmp_path / "out.jsonl"

    def stub_poll(stations, target_date, session=None, stats=None, clock=None):
        if stats is not None:
            stats["captured"], stats["skipped"] = 2, 1
        return [{"ticker": "X", "source": "rest_poll"}]

    summary = record(["KNYC"], date(2026, 9, 19), out, 15, until,
                      session=Mock(), poll_fn=stub_poll, clock=clock, sleep_fn=clock.sleep)

    assert summary.polls == 3
    assert summary.captured == 6
    assert summary.skipped == 3
    assert summary.gaps == 0
    lines = out.read_text().splitlines()
    assert len(lines) == 3
    for line in lines:
        assert json.loads(line)["ticker"] == "X"


def test_failed_poll_writes_a_gap_marker_and_continues(tmp_path):
    clock = FakeClock(datetime(2026, 9, 19, 0, 0, 0, tzinfo=timezone.utc))
    until = clock.now + timedelta(seconds=20)
    out = tmp_path / "out.jsonl"
    calls = {"n": 0}

    def flaky_poll(stations, target_date, session=None, stats=None, clock=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("network down")
        if stats is not None:
            stats["captured"], stats["skipped"] = 1, 0
        return [{"ticker": "X"}]

    summary = record(["KNYC"], date(2026, 9, 19), out, 15, until,
                      session=Mock(), poll_fn=flaky_poll, clock=clock, sleep_fn=clock.sleep)

    assert summary.polls == 2
    assert summary.gaps == 1
    assert summary.captured == 1
    lines = [json.loads(line) for line in out.read_text().splitlines()]
    assert lines[0]["source"] == "gap"
    assert "error" in lines[0]
    assert lines[1]["ticker"] == "X"


def test_record_never_raises_from_poll_fn_exceptions(tmp_path):
    """A poll_fn that always raises must still let the run finish (all gaps, no crash)."""
    clock = FakeClock(datetime(2026, 9, 19, 0, 0, 0, tzinfo=timezone.utc))
    until = clock.now + timedelta(seconds=20)
    out = tmp_path / "out.jsonl"

    def always_fails(stations, target_date, session=None, stats=None, clock=None):
        raise RuntimeError("always down")

    summary = record(["KNYC"], date(2026, 9, 19), out, 15, until,
                      session=Mock(), poll_fn=always_fails, clock=clock, sleep_fn=clock.sleep)
    assert summary.gaps == 2
    assert summary.captured == 0


def test_all_snapshots_in_one_poll_share_a_captured_utc():
    """A ladder's legs must group together even when a pass crosses a second."""
    a, b = "KXHIGHNY-26SEP19-B80.5", "KXHIGHNY-26SEP19-B82.5"
    book = {"orderbook_fp": {"yes_dollars": [["0.30", "10"]], "no_dollars": [["0.65", "5"]]}}
    session = FakeSession(responses={
        f"{BASE_URL}/markets": {"markets": [raw_market(a), raw_market(b)]},
        f"{BASE_URL}/markets/{a}/orderbook": book,
        f"{BASE_URL}/markets/{b}/orderbook": book,
    })
    ticks = iter([
        datetime(2026, 9, 19, 4, 5, 6, tzinfo=timezone.utc),   # cycle stamp
        datetime(2026, 9, 19, 4, 5, 6, tzinfo=timezone.utc),
        datetime(2026, 9, 19, 4, 5, 7, tzinfo=timezone.utc),   # crosses a second
    ])
    snapshots = poll_once(["KNYC"], date(2026, 9, 19), session=session, clock=lambda: next(ticks))
    assert len(snapshots) == 2
    assert len({s["captured_utc"] for s in snapshots}) == 1
    # The precise per-market receipt time is kept, and still differs.
    assert [s["received_utc"] for s in snapshots] == ["2026-09-19T04:05:06Z", "2026-09-19T04:05:07Z"]
