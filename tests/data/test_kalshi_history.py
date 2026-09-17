"""Unit tests for src/data/kalshi_history.py — public Kalshi weather-market history.

No real network calls: every test uses a mocked `requests.Session` (or the
module's cache_get/cache_set against a monkeypatched CACHE_DIR). Covers bound
parsing, settlement-source classification, the historical/live partition
(including the "empty result on the wrong side" case), caching behavior,
retry/timeout configuration, GET-only access, and the price-at-instant
boundary rules.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from src.data import cache as cache_module
from src.data import kalshi_history as kh


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolate_cache_dir(tmp_path, monkeypatch):
    """Redirect the on-disk cache to a fresh tmp directory for every test."""
    monkeypatch.setattr(cache_module, "CACHE_DIR", tmp_path / "cache")


@pytest.fixture(autouse=True)
def reset_module_session(monkeypatch):
    """Never let a test accidentally build/reuse the real module-level session."""
    monkeypatch.setattr(kh, "_SESSION", None)


@pytest.fixture(autouse=True)
def no_real_sleeping(monkeypatch):
    """Skip the real politeness `time.sleep(SLEEP_SECONDS)` calls in unit tests."""
    monkeypatch.setattr(kh.time, "sleep", lambda _seconds: None)


def _response(payload: dict, status_code: int = 200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = payload
    resp.raise_for_status = MagicMock()
    return resp


def _mock_session(url_to_payload: dict[str, dict]) -> MagicMock:
    """A `spec=["get"]` session whose `.get(url, ...)` dispatches on a URL substring.

    `spec=["get"]` means calling `.post`/`.put`/etc. on it raises AttributeError,
    which is how the GET-only tests catch an accidental non-GET call.
    """
    session = MagicMock(spec=["get"])

    def _get(url, params=None, timeout=None):
        for substring, payload in url_to_payload.items():
            if substring in url:
                return _response(payload)
        raise AssertionError(f"Unexpected URL requested: {url} (params={params})")

    session.get.side_effect = _get
    return session


CUTOFF_PAYLOAD = {
    "market_settled_ts": "2026-07-18T00:00:00Z",
    "trades_created_ts": "2026-07-18T00:00:00Z",
    "orders_updated_ts": "2026-07-18T00:00:00Z",
    "market_positions_last_updated_ts": "2026-07-18T00:00:00Z",
}


def _raw_market(
    ticker="KXHIGHNY-25SEP16-B73.5",
    event_ticker="KXHIGHNY-25SEP16",
    strike_type="between",
    floor_strike=73,
    cap_strike=74,
    result="yes",
    status="finalized",
    rules_primary="If the highest temperature ... National Weather Service's Climatological Report (Daily) ...",
    close_time="2025-09-17T04:00:00Z",
) -> dict:
    return {
        "ticker": ticker,
        "event_ticker": event_ticker,
        "strike_type": strike_type,
        "floor_strike": floor_strike,
        "cap_strike": cap_strike,
        "result": result,
        "status": status,
        "rules_primary": rules_primary,
        "close_time": close_time,
    }


def _raw_live_candle(end_ts: int, bid="0.4500", ask="0.5000", volume="120", oi="300") -> dict:
    return {
        "end_period_ts": end_ts,
        "yes_bid": {"close_dollars": bid},
        "yes_ask": {"close_dollars": ask},
        "volume_fp": volume,
        "open_interest_fp": oi,
    }


def _raw_historical_candle(end_ts: int, bid="0.3300", ask="0.3700", volume="80", oi="200") -> dict:
    return {
        "end_period_ts": end_ts,
        "yes_bid": {"close": bid},
        "yes_ask": {"close": ask},
        "volume": volume,
        "open_interest": oi,
    }


# ---------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------

class TestDollarsToCents:
    def test_typical_value(self):
        assert kh._dollars_to_cents("0.3300") == 33

    def test_rounds_half_up(self):
        assert kh._dollars_to_cents("0.005") == 1  # 0.5 cents rounds up

    def test_none_is_none(self):
        assert kh._dollars_to_cents(None) is None

    def test_garbage_is_none(self):
        assert kh._dollars_to_cents("not-a-number") is None


class TestToFloat:
    def test_numeric_string(self):
        assert kh._to_float("120") == 120.0

    def test_none_is_none(self):
        assert kh._to_float(None) is None

    def test_garbage_is_none(self):
        assert kh._to_float("nope") is None


class TestDateTimeHelpers:
    def test_as_date_passthrough(self):
        d = date(2025, 1, 1)
        assert kh._as_date(d) is d

    def test_as_date_from_iso_string(self):
        assert kh._as_date("2025-01-01") == date(2025, 1, 1)

    def test_parse_utc_with_z_suffix(self):
        assert kh._parse_utc("2025-01-01T00:00:00Z") == datetime(2025, 1, 1, tzinfo=timezone.utc)

    def test_parse_utc_naive_assumed_utc(self):
        assert kh._parse_utc("2025-01-01T00:00:00") == datetime(2025, 1, 1, tzinfo=timezone.utc)

    def test_format_utc_round_trip(self):
        moment = datetime(2025, 6, 1, 12, 30, tzinfo=timezone.utc)
        assert kh._format_utc(moment) == "2025-06-01T12:30:00Z"


# ---------------------------------------------------------------------------
# Bound parsing (_bounds_from_strike)
# ---------------------------------------------------------------------------

class TestBoundsFromStrike:
    def test_between(self):
        lower, upper = kh._bounds_from_strike("between", 81, 82)
        assert (lower, upper) == (80.5, 82.5)

    def test_less(self):
        lower, upper = kh._bounds_from_strike("less", None, 75)
        assert lower is None
        assert upper == 74.5

    def test_greater(self):
        lower, upper = kh._bounds_from_strike("greater", 82, None)
        assert lower == 82.5
        assert upper is None

    def test_between_missing_floor_raises(self):
        with pytest.raises(ValueError):
            kh._bounds_from_strike("between", None, 82)

    def test_less_missing_cap_raises(self):
        with pytest.raises(ValueError):
            kh._bounds_from_strike("less", None, None)

    def test_greater_missing_floor_raises(self):
        with pytest.raises(ValueError):
            kh._bounds_from_strike("greater", None, None)

    def test_unknown_strike_type_raises(self):
        with pytest.raises(ValueError, match="Unknown strike_type"):
            kh._bounds_from_strike("weird", 1, 2)


# ---------------------------------------------------------------------------
# Settlement source classification (_settlement_source)
# ---------------------------------------------------------------------------

class TestSettlementSource:
    def test_weather_company(self):
        text = "... according to The Weather Company, then the market resolves to Yes."
        assert kh._settlement_source(text) == "weather_company"

    def test_nws_cli_climatological_report(self):
        text = "... as reported by the National Weather Service's Climatological Report (Daily) ..."
        assert kh._settlement_source(text) == "nws_cli"

    def test_nws_cli_nws_only_mention(self):
        text = "... determined by the National Weather Service for that day ..."
        assert kh._settlement_source(text) == "nws_cli"

    def test_unknown_for_unrecognized_text(self):
        assert kh._settlement_source("some unrelated rules text") == "unknown"

    def test_unknown_for_non_string(self):
        assert kh._settlement_source(None) == "unknown"


# ---------------------------------------------------------------------------
# Event ticker <-> date round trip
# ---------------------------------------------------------------------------

class TestEventTicker:
    def test_event_ticker_for_date(self):
        assert kh.event_ticker_for_date("KXHIGHNY", date(2026, 9, 16)) == "KXHIGHNY-26SEP16"

    def test_event_ticker_unknown_series_raises(self):
        with pytest.raises(ValueError):
            kh.event_ticker_for_date("KXHIGHLAX", date(2026, 9, 16))

    def test_round_trip(self):
        day = date(2025, 1, 5)
        ticker = kh.event_ticker_for_date("KXHIGHMIA", day)
        assert kh._parse_event_ticker_date(ticker, "KXHIGHMIA") == day

    def test_parse_wrong_prefix_raises(self):
        with pytest.raises(ValueError):
            kh._parse_event_ticker_date("KXHIGHNY-26SEP16", "KXHIGHCHI")

    def test_parse_bad_month_raises(self):
        with pytest.raises(ValueError):
            kh._parse_event_ticker_date("KXHIGHNY-26ZZZ16", "KXHIGHNY")


# ---------------------------------------------------------------------------
# Decision instant
# ---------------------------------------------------------------------------

class TestDecisionInstant:
    def test_knyc_decision_instant(self):
        # KNYC is fixed EST (UTC-5) year-round for this purpose: local day
        # start = 05:00Z, decision instant = 04:59Z the same UTC day.
        t = kh.decision_instant_utc("KXHIGHNY", date(2026, 9, 16))
        assert t == datetime(2026, 9, 16, 4, 59, tzinfo=timezone.utc)

    def test_kmdw_decision_instant(self):
        # KMDW is fixed CST (UTC-6): local day start = 06:00Z.
        t = kh.decision_instant_utc("KXHIGHCHI", date(2026, 9, 16))
        assert t == datetime(2026, 9, 16, 5, 59, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Historical/live partition + get_historical_cutoff caching
# ---------------------------------------------------------------------------

class TestSessionConfiguration:
    def test_retry_and_timeout_configuration(self):
        session = kh._get_session()
        adapter = session.get_adapter("https://external-api.kalshi.com")
        retry = adapter.max_retries
        assert retry.total == 3
        assert retry.backoff_factor == 1
        assert set(retry.status_forcelist) == {429, 500, 502, 503, 504}
        assert list(retry.allowed_methods) == ["GET"]
        assert retry.raise_on_status is False
        assert kh.REQUEST_TIMEOUT == (5, 30)

    def test_user_agent_set(self):
        session = kh._get_session()
        assert "kalshi-bot-research-history" in session.headers.get("User-Agent", "")

    def test_session_is_built_once(self):
        first = kh._get_session()
        second = kh._get_session()
        assert first is second

    def test_requests_use_configured_timeout(self):
        cutoff_session = _mock_session({"/historical/cutoff": CUTOFF_PAYLOAD})
        kh.get_historical_cutoff(cutoff_session)
        assert cutoff_session.get.call_args.kwargs["timeout"] == kh.REQUEST_TIMEOUT


class TestHistoricalCutoff:
    def test_fetches_and_parses(self):
        session = _mock_session({"/historical/cutoff": CUTOFF_PAYLOAD})
        cutoff = kh.get_historical_cutoff(session)
        assert cutoff["market_settled_ts"] == datetime(2026, 7, 18, tzinfo=timezone.utc)
        assert session.get.call_count == 1

    def test_cached_on_second_call(self):
        session = _mock_session({"/historical/cutoff": CUTOFF_PAYLOAD})
        kh.get_historical_cutoff(session)
        kh.get_historical_cutoff(session)
        assert session.get.call_count == 1

    def test_use_historical_boundary(self):
        cutoff = datetime(2026, 7, 18, tzinfo=timezone.utc)
        assert kh._use_historical(cutoff, cutoff) is True  # exactly at cutoff -> historical
        assert kh._use_historical(cutoff - timedelta(seconds=1), cutoff) is True
        assert kh._use_historical(cutoff + timedelta(seconds=1), cutoff) is False


# ---------------------------------------------------------------------------
# fetch_event_markets: endpoint selection, empty-result handling, caching
# ---------------------------------------------------------------------------

class TestFetchEventMarkets:
    def test_uses_historical_endpoint_when_close_before_cutoff(self):
        cutoff = {"market_settled_ts": datetime(2026, 7, 18, tzinfo=timezone.utc)}
        session = _mock_session({"/historical/markets": {"markets": [_raw_market()]}})
        markets = kh.fetch_event_markets("KXHIGHNY", date(2025, 9, 16), session, cutoff)
        assert len(markets) == 1
        # Confirm the historical URL, not the live one, was actually requested.
        called_url = session.get.call_args.args[0]
        assert "/historical/markets" in called_url

    def test_uses_live_endpoint_when_close_after_cutoff(self):
        cutoff = {"market_settled_ts": datetime(2020, 1, 1, tzinfo=timezone.utc)}
        session = _mock_session({"trade-api/v2/markets": {"markets": [_raw_market()]}})
        markets = kh.fetch_event_markets("KXHIGHNY", date(2026, 9, 16), session, cutoff)
        assert len(markets) == 1
        called_url = session.get.call_args.args[0]
        assert called_url.endswith("/markets")
        assert "/historical/" not in called_url

    def test_empty_result_is_not_reinterpreted(self):
        """An empty response from the *correct* side of the partition must not
        be treated as evidence that the wrong side should have been tried.
        `fetch_event_markets` decides the endpoint before calling, and must
        stick with that decision even when the response is empty."""
        cutoff = {"market_settled_ts": datetime(2026, 7, 18, tzinfo=timezone.utc)}
        session = _mock_session({"/historical/markets": {"markets": []}})
        markets = kh.fetch_event_markets("KXHIGHNY", date(2025, 9, 16), session, cutoff)
        assert markets == []
        # Only the historical endpoint was ever called -- no fallback attempt
        # against the live endpoint just because the result was empty.
        assert session.get.call_count == 1
        called_url = session.get.call_args.args[0]
        assert "/historical/markets" in called_url

    def test_historical_side_is_cached(self):
        cutoff = {"market_settled_ts": datetime(2026, 7, 18, tzinfo=timezone.utc)}
        session = _mock_session({"/historical/markets": {"markets": [_raw_market()]}})
        kh.fetch_event_markets("KXHIGHNY", date(2025, 9, 16), session, cutoff)
        kh.fetch_event_markets("KXHIGHNY", date(2025, 9, 16), session, cutoff)
        assert session.get.call_count == 1

    def test_live_side_is_never_cached(self):
        cutoff = {"market_settled_ts": datetime(2020, 1, 1, tzinfo=timezone.utc)}
        session = _mock_session({"trade-api/v2/markets": {"markets": [_raw_market()]}})
        kh.fetch_event_markets("KXHIGHNY", date(2026, 9, 16), session, cutoff)
        kh.fetch_event_markets("KXHIGHNY", date(2026, 9, 16), session, cutoff)
        assert session.get.call_count == 2

    def test_get_only_raises_if_non_get_used(self):
        """Sanity check on the mock itself: the session is GET-only by construction."""
        cutoff = {"market_settled_ts": datetime(2026, 7, 18, tzinfo=timezone.utc)}
        session = _mock_session({"/historical/markets": {"markets": []}})
        with pytest.raises(AttributeError):
            session.post("http://example.com")
        kh.fetch_event_markets("KXHIGHNY", date(2025, 9, 16), session, cutoff)


# ---------------------------------------------------------------------------
# parse_market
# ---------------------------------------------------------------------------

class TestParseMarket:
    def test_parses_between_market(self):
        raw = _raw_market()
        parsed = kh.parse_market(raw, "KXHIGHNY")
        assert parsed["ticker"] == raw["ticker"]
        assert parsed["station"] == "KNYC"
        assert parsed["date_lst"] == "2025-09-16"
        assert parsed["lower_bound_f"] == 72.5
        assert parsed["upper_bound_f"] == 74.5
        assert parsed["result"] == "yes"
        assert parsed["settlement_source"] == "nws_cli"

    def test_result_none_when_unresolved(self):
        raw = _raw_market(result=None, status="active")
        parsed = kh.parse_market(raw, "KXHIGHNY")
        assert parsed["result"] is None

    def test_result_garbage_value_normalized_to_none(self):
        raw = _raw_market(result="determined")
        parsed = kh.parse_market(raw, "KXHIGHNY")
        assert parsed["result"] is None

    def test_weather_company_settlement_source(self):
        raw = _raw_market(
            rules_primary="... according to The Weather Company, then the market resolves to Yes."
        )
        parsed = kh.parse_market(raw, "KXHIGHNY")
        assert parsed["settlement_source"] == "weather_company"


# ---------------------------------------------------------------------------
# Candlestick normalization
# ---------------------------------------------------------------------------

class TestNormalizeCandle:
    def test_live_shape(self):
        raw = _raw_live_candle(1_700_000_000)
        normalized = kh._normalize_candle(raw, is_historical=False)
        assert normalized == {
            "end_period_ts": 1_700_000_000,
            "yes_bid_cents": 45,
            "yes_ask_cents": 50,
            "volume": 120.0,
            "open_interest": 300.0,
        }

    def test_historical_shape(self):
        raw = _raw_historical_candle(1_700_000_000)
        normalized = kh._normalize_candle(raw, is_historical=True)
        assert normalized == {
            "end_period_ts": 1_700_000_000,
            "yes_bid_cents": 33,
            "yes_ask_cents": 37,
            "volume": 80.0,
            "open_interest": 200.0,
        }

    def test_missing_bid_ask_is_none(self):
        raw = {"end_period_ts": 1, "yes_bid": {}, "yes_ask": {}}
        normalized = kh._normalize_candle(raw, is_historical=True)
        assert normalized["yes_bid_cents"] is None
        assert normalized["yes_ask_cents"] is None


class TestFetchCandlesticks:
    def test_historical_endpoint_and_caching(self):
        cutoff = {"market_settled_ts": datetime(2026, 7, 18, tzinfo=timezone.utc)}
        payload = {"candlesticks": [_raw_historical_candle(1_700_000_000)]}
        session = _mock_session({"/historical/markets/": payload})
        rows = kh.fetch_candlesticks(
            "KXHIGHNY", "KXHIGHNY-25SEP16-B73.5", "KXHIGHNY-25SEP16",
            1_699_900_000, 1_700_000_000, 60, session, cutoff,
        )
        assert rows[0]["yes_bid_cents"] == 33
        assert session.get.call_args.args[0].startswith(f"{kh.BASE_URL}/historical/markets/")

        # Second call with identical params should be served from cache.
        kh.fetch_candlesticks(
            "KXHIGHNY", "KXHIGHNY-25SEP16-B73.5", "KXHIGHNY-25SEP16",
            1_699_900_000, 1_700_000_000, 60, session, cutoff,
        )
        assert session.get.call_count == 1

    def test_live_endpoint_not_cached(self):
        cutoff = {"market_settled_ts": datetime(2020, 1, 1, tzinfo=timezone.utc)}
        payload = {"candlesticks": [_raw_live_candle(1_700_000_000)]}
        session = _mock_session({"/series/KXHIGHNY/markets/": payload})
        kh.fetch_candlesticks(
            "KXHIGHNY", "KXHIGHNY-26SEP16-B73.5", "KXHIGHNY-26SEP16",
            1_699_900_000, 1_700_000_000, 60, session, cutoff,
        )
        kh.fetch_candlesticks(
            "KXHIGHNY", "KXHIGHNY-26SEP16-B73.5", "KXHIGHNY-26SEP16",
            1_699_900_000, 1_700_000_000, 60, session, cutoff,
        )
        assert session.get.call_count == 2


# ---------------------------------------------------------------------------
# price_at_instant: the boundary rules
# ---------------------------------------------------------------------------

class TestPriceAtInstant:
    def _candle(self, end_ts, bid=45, ask=50):
        return {"end_period_ts": end_ts, "yes_bid_cents": bid, "yes_ask_cents": ask, "volume": 1.0, "open_interest": 1.0}

    def test_candle_ending_exactly_at_t_is_usable(self):
        t = datetime(2025, 9, 16, 4, 59, tzinfo=timezone.utc)
        candles = [self._candle(int(t.timestamp()))]
        result = kh.price_at_instant(candles, t)
        assert result.status == "ok"
        assert result.yes_bid_cents == 45
        assert result.age_hours == 0.0

    def test_candle_ending_one_second_after_t_is_not_usable(self):
        t = datetime(2025, 9, 16, 4, 59, tzinfo=timezone.utc)
        candles = [self._candle(int(t.timestamp()) + 1)]
        result = kh.price_at_instant(candles, t)
        assert result.status == "missing"
        assert result.candle_end_utc is None

    def test_picks_most_recent_eligible_candle(self):
        t = datetime(2025, 9, 16, 4, 59, tzinfo=timezone.utc)
        older = self._candle(int(t.timestamp()) - 3600, bid=10, ask=20)
        newer = self._candle(int(t.timestamp()) - 60, bid=45, ask=50)
        result = kh.price_at_instant([older, newer], t)
        assert result.status == "ok"
        assert result.yes_bid_cents == 45

    def test_stale_beyond_max_staleness(self):
        t = datetime(2025, 9, 16, 4, 59, tzinfo=timezone.utc)
        candles = [self._candle(int(t.timestamp()) - int(3.5 * 3600))]
        result = kh.price_at_instant(candles, t, max_staleness_hours=3.0)
        assert result.status == "stale"
        assert result.age_hours == pytest.approx(3.5, abs=0.01)

    def test_exactly_at_max_staleness_boundary_is_ok(self):
        t = datetime(2025, 9, 16, 4, 59, tzinfo=timezone.utc)
        candles = [self._candle(int(t.timestamp()) - int(3 * 3600))]
        result = kh.price_at_instant(candles, t, max_staleness_hours=3.0)
        assert result.status == "ok"

    def test_missing_bid_or_ask_on_eligible_candle(self):
        t = datetime(2025, 9, 16, 4, 59, tzinfo=timezone.utc)
        candles = [{"end_period_ts": int(t.timestamp()), "yes_bid_cents": None, "yes_ask_cents": 50}]
        result = kh.price_at_instant(candles, t)
        assert result.status == "missing"
        assert result.candle_end_utc is not None  # candle existed, just unusable

    def test_no_candles_at_all(self):
        t = datetime(2025, 9, 16, 4, 59, tzinfo=timezone.utc)
        result = kh.price_at_instant([], t)
        assert result.status == "missing"
        assert result.candle_end_utc is None
        assert result.age_hours is None


# ---------------------------------------------------------------------------
# fetch_event_at_decision: end-to-end with a mocked session
# ---------------------------------------------------------------------------

class TestFetchEventAtDecision:
    def test_full_flow_historical(self):
        cutoff = {"/historical/cutoff": CUTOFF_PAYLOAD}
        target = date(2025, 9, 16)
        decision_time = kh.decision_instant_utc("KXHIGHNY", target)
        candle_payload = {"candlesticks": [_raw_historical_candle(int(decision_time.timestamp()))]}
        session = _mock_session({
            **cutoff,
            "/historical/markets/": candle_payload,  # candlesticks (checked first, more specific)
            "/historical/markets": {"markets": [_raw_market()]},  # event markets listing
        })
        # More specific dispatch: markets listing has no trailing ticker path,
        # candlesticks does. Route explicitly via a custom side_effect instead
        # to avoid substring collision between the two "/historical/markets"
        # URLs.
        def _get(url, params=None, timeout=None):
            if "/historical/cutoff" in url:
                return _response(CUTOFF_PAYLOAD)
            if "/candlesticks" in url:
                return _response(candle_payload)
            if "/historical/markets" in url:
                return _response({"markets": [_raw_market()]})
            raise AssertionError(f"Unexpected URL {url}")

        session.get.side_effect = _get

        rows = kh.fetch_event_at_decision("KXHIGHNY", target, session=session)
        assert len(rows) == 1
        row = rows[0]
        assert row["price"].status == "ok"
        assert row["price"].yes_bid_cents == 33
        assert row["decision_time_utc"] == kh._format_utc(decision_time)
