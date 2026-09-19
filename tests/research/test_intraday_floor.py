"""Unit tests for src/research/intraday_floor.py — the arithmetically-dead-bracket test.

No real network calls: `fetch_event_markets`, `fetch_candlesticks`, and
`get_historical_cutoff` are monkeypatched at the `src.research.intraday_floor`
module level (the names that module imported into its own namespace);
`observations.fetch_asos_observations` is monkeypatched on the shared
`src.data.weather.observations` module object `intraday_floor` imports.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest
import requests

from src.data.weather import observations
from src.paper import protocol as protocol_mod
from src.paper.fees import trading_fee_cents
from src.research import intraday_floor as ifl

STATION = "KNYC"
SERIES = "KXHIGHNY"
EVENT_TICKER = "KXHIGHNY-25JAN01"


# ---------------------------------------------------------------------------
# Small builders
# ---------------------------------------------------------------------------

def _market(ticker, lower, upper, *, result="no", status="finalized",
            open_time="2025-01-01T00:00:00Z", event_ticker=EVENT_TICKER):
    return {
        "ticker": ticker, "event_ticker": event_ticker, "station": STATION, "date_lst": "2025-01-01",
        "lower_bound_f": lower, "upper_bound_f": upper, "result": result,
        "settlement_source": "nws_cli", "close_time": "2025-01-02T04:59:00Z",
        "status": status, "open_time": open_time,
    }


def _candle(end_period_ts, bid_cents, ask_cents=None, volume=10.0, open_interest=10.0):
    return {
        "end_period_ts": end_period_ts, "yes_bid_cents": bid_cents,
        "yes_ask_cents": ask_cents if ask_cents is not None else min(bid_cents + 2, 99),
        "volume": volume, "open_interest": open_interest,
    }


def _running_row(valid_utc, temp_f, running_max_f, n_obs):
    return {"valid_utc": valid_utc, "temp_f": temp_f, "running_max_f": running_max_f, "n_obs": n_obs}


# ---------------------------------------------------------------------------
# dead_brackets_at: boundary tests
# ---------------------------------------------------------------------------

class TestDeadBracketsAt:
    def test_exactly_at_margin_is_not_dead(self):
        # running_max=80, margin=2 -> threshold=78; upper==78 is not "below" it.
        markets = [_market("T1", 76.5, 78.0)]
        assert ifl.dead_brackets_at(markets, 80.0, 2.0) == []

    def test_just_inside_threshold_is_dead(self):
        markets = [_market("T1", 76.0, 77.9)]
        assert ifl.dead_brackets_at(markets, 80.0, 2.0) == markets

    def test_just_outside_threshold_is_not_dead(self):
        markets = [_market("T1", 76.5, 78.1)]
        assert ifl.dead_brackets_at(markets, 80.0, 2.0) == []

    def test_unbounded_top_bracket_is_never_dead(self):
        markets = [_market("T1", 40.5, None)]
        assert ifl.dead_brackets_at(markets, 1000.0, 0.0) == []

    def test_mixed_markets_returns_only_dead_ones(self):
        dead = _market("DEAD", 70.5, 71.5)
        alive = _market("ALIVE", 89.5, 95.5)  # upper=95.5 is not below threshold=88
        top = _market("TOP", 90.5, None)
        assert ifl.dead_brackets_at([dead, alive, top], 90.0, 2.0) == [dead]


# ---------------------------------------------------------------------------
# _simulate_margin: dedup, no-look-ahead, fee-once, primary deliverable, min bid
# ---------------------------------------------------------------------------

class TestSimulateMargin:
    T0 = datetime(2025, 1, 1, 15, 53, tzinfo=timezone.utc)  # first hourly obs, local-ish afternoon

    def _running_two_hours(self, temp1=80.0, temp2=82.0):
        row1 = _running_row(self.T0.isoformat().replace("+00:00", "Z"), temp1, temp1, 1)
        t2 = self.T0 + timedelta(hours=1)
        row2 = _running_row(t2.isoformat().replace("+00:00", "Z"), temp2, max(temp1, temp2), 2)
        return [row1, row2]

    def _running_one_hour(self, temp=80.0):
        return [_running_row(self.T0.isoformat().replace("+00:00", "Z"), temp, temp, 1)]

    def test_dedup_trades_once_then_reports_repeat_candidate(self):
        running = self._running_two_hours(temp1=80.0, temp2=82.0)
        market = _market("DEAD1", 70.5, 71.5)  # dead from the very first instant (80 - 2 > 71.5)
        decision1 = self.T0 + timedelta(minutes=ifl.OBSERVATION_LATENCY_MINUTES)
        decision2 = running[1]["valid_utc"]
        decision2 = ifl._parse_utc(decision2) + timedelta(minutes=ifl.OBSERVATION_LATENCY_MINUTES)
        candles = {
            "DEAD1": [
                _candle(int(decision1.timestamp()), bid_cents=5),
                _candle(int(decision2.timestamp()), bid_cents=6),
            ],
        }
        result = ifl._simulate_margin(
            [market], candles, running, margin=2.0, station=STATION, date_lst="2025-01-01",
            event_key=EVENT_TICKER, contracts=10, fee_type="quadratic", fee_multiplier=1.0,
        )
        assert len(result["trades"]) == 1
        assert result["trades"][0]["bid_cents"] == 5  # the FIRST qualifying instant, not the second
        assert len(result["dead_records"]) == 1
        assert result["n_repeat_candidates"] == 1

    def test_fee_charged_once_per_order_not_per_contract(self):
        running = self._running_two_hours()
        market = _market("DEAD1", 70.5, 71.5, result="no")
        decision1 = self.T0 + timedelta(minutes=ifl.OBSERVATION_LATENCY_MINUTES)
        candles = {"DEAD1": [_candle(int(decision1.timestamp()), bid_cents=2)]}
        result = ifl._simulate_margin(
            [market], candles, running, margin=2.0, station=STATION, date_lst="2025-01-01",
            event_key=EVENT_TICKER, contracts=10, fee_type="quadratic", fee_multiplier=1.0,
        )
        trade = result["trades"][0]
        no_cost = 100 - 2
        expected_fee = trading_fee_cents(no_cost, 10, fee_type="quadratic", fee_multiplier=1.0, is_maker=False)
        assert trade["fee_cents"] == expected_fee
        # result "no" -> the NO buy wins: 100*10 - 98*10 - fee
        assert trade["pnl_cents"] == pytest.approx(100 * 10 - no_cost * 10 - expected_fee)

    def test_bid_below_minimum_is_refused_not_traded(self):
        running = self._running_one_hour()
        market = _market("DEAD1", 70.5, 71.5)
        decision1 = self.T0 + timedelta(minutes=ifl.OBSERVATION_LATENCY_MINUTES)
        candles = {"DEAD1": [_candle(int(decision1.timestamp()), bid_cents=0)]}
        result = ifl._simulate_margin(
            [market], candles, running, margin=2.0, station=STATION, date_lst="2025-01-01",
            event_key=EVENT_TICKER, contracts=10, fee_type="quadratic", fee_multiplier=1.0,
        )
        assert result["trades"] == []
        assert result["skip_reasons"]["bid_below_minimum"] == 1
        # Still recorded as a dead bracket -- the primary deliverable doesn't need a tradeable price.
        assert len(result["dead_records"]) == 1

    def test_dead_bracket_settling_yes_is_recorded_regardless_of_price(self):
        running = self._running_two_hours()
        # Dead by observation, yet Kalshi settled it YES -- the falsification case.
        market = _market("DEAD_BUT_YES", 70.5, 71.5, result="yes", status="finalized")
        candles = {"DEAD_BUT_YES": []}  # no usable price at all -- must not suppress the primary record
        result = ifl._simulate_margin(
            [market], candles, running, margin=2.0, station=STATION, date_lst="2025-01-01",
            event_key=EVENT_TICKER, contracts=10, fee_type="quadratic", fee_multiplier=1.0,
        )
        assert result["trades"] == []
        assert len(result["dead_records"]) == 1
        record = result["dead_records"][0]
        assert record["settled_yes"] is True
        assert record["ticker"] == "DEAD_BUT_YES"

    def test_market_not_yet_open_is_never_evaluated(self):
        running = self._running_two_hours(temp1=80.0, temp2=82.0)
        # open_time is after both decision instants -- the bracket must never be
        # scored dead, even though it arithmetically would be.
        late_open = (self.T0 + timedelta(hours=5)).isoformat().replace("+00:00", "Z")
        market = _market("NOT_OPEN_YET", 70.5, 71.5, open_time=late_open)
        candles = {"NOT_OPEN_YET": [_candle(0, bid_cents=5)]}
        result = ifl._simulate_margin(
            [market], candles, running, margin=2.0, station=STATION, date_lst="2025-01-01",
            event_key=EVENT_TICKER, contracts=10, fee_type="quadratic", fee_multiplier=1.0,
        )
        assert result["trades"] == []
        assert result["dead_records"] == []

    def test_missing_open_time_is_skipped_not_assumed_open(self):
        running = self._running_two_hours()
        market = _market("NO_OPEN_TIME", 70.5, 71.5, open_time=None)
        candles = {"NO_OPEN_TIME": [_candle(0, bid_cents=5)]}
        result = ifl._simulate_margin(
            [market], candles, running, margin=2.0, station=STATION, date_lst="2025-01-01",
            event_key=EVENT_TICKER, contracts=10, fee_type="quadratic", fee_multiplier=1.0,
        )
        assert result["trades"] == []
        assert result["skip_reasons"]["missing_open_time"] >= 1

    def test_candle_after_decision_instant_is_never_used(self):
        running = self._running_two_hours()
        market = _market("DEAD1", 70.5, 71.5, result="no")
        decision1 = self.T0 + timedelta(minutes=ifl.OBSERVATION_LATENCY_MINUTES)
        future_ts = int((decision1 + timedelta(hours=1)).timestamp())
        past_ts = int(decision1.timestamp())
        candles = {
            "DEAD1": [
                _candle(past_ts, bid_cents=3),
                _candle(future_ts, bid_cents=99),  # must never be picked for this instant
            ],
        }
        result = ifl._simulate_margin(
            [market], candles, running, margin=2.0, station=STATION, date_lst="2025-01-01",
            event_key=EVENT_TICKER, contracts=10, fee_type="quadratic", fee_multiplier=1.0,
        )
        assert result["trades"][0]["bid_cents"] == 3

    def test_no_dead_brackets_at_all_is_a_clean_empty_day(self):
        running = self._running_two_hours(temp1=40.0, temp2=41.0)
        market = _market("ALIVE", 70.5, 71.5)  # nowhere near dead
        result = ifl._simulate_margin(
            [market], {"ALIVE": []}, running, margin=2.0, station=STATION, date_lst="2025-01-01",
            event_key=EVENT_TICKER, contracts=10, fee_type="quadratic", fee_multiplier=1.0,
        )
        assert result == {
            "trades": [], "dead_records": [], "n_repeat_candidates": 0, "skip_reasons": result["skip_reasons"],
        }
        assert not result["skip_reasons"]

    def test_unsettled_result_is_not_traded(self):
        running = self._running_one_hour()
        market = _market("PENDING", 70.5, 71.5, result=None, status="active")
        decision1 = self.T0 + timedelta(minutes=ifl.OBSERVATION_LATENCY_MINUTES)
        candles = {"PENDING": [_candle(int(decision1.timestamp()), bid_cents=5)]}
        result = ifl._simulate_margin(
            [market], candles, running, margin=2.0, station=STATION, date_lst="2025-01-01",
            event_key=EVENT_TICKER, contracts=10, fee_type="quadratic", fee_multiplier=1.0,
        )
        assert result["trades"] == []
        assert result["skip_reasons"]["unsettled_result"] == 1
        # Still a dead-bracket observation (arithmetic doesn't need settlement) --
        # but not flagged settled_yes since it isn't settled at all.
        assert result["dead_records"][0]["settled_yes"] is False


# ---------------------------------------------------------------------------
# scan_event_day: whole-day skip reasons
# ---------------------------------------------------------------------------

class TestScanEventDay:
    DAY = date(2025, 1, 1)

    def _patch_cutoff(self, monkeypatch):
        monkeypatch.setattr(
            ifl, "get_historical_cutoff",
            lambda *a, **k: {"market_settled_ts": datetime(2025, 6, 1, tzinfo=timezone.utc)},
        )

    def test_no_observations_is_skipped(self, monkeypatch):
        self._patch_cutoff(monkeypatch)
        monkeypatch.setattr(observations, "fetch_asos_observations", lambda *a, **k: [])
        result = ifl.scan_event_day(SERIES, self.DAY)
        assert result["skip_reason"] == "no_observations"
        assert result["by_margin"] == {}

    def test_no_market_data_is_skipped(self, monkeypatch):
        self._patch_cutoff(monkeypatch)
        offset = ifl.STATION_STANDARD_UTC_OFFSET_HOURS[STATION]
        start = ifl._local_day_start(self.DAY, offset)
        obs = [{"valid_utc": start.isoformat().replace("+00:00", "Z"), "temp_f": 70.0, "station_code": STATION}]
        monkeypatch.setattr(observations, "fetch_asos_observations", lambda *a, **k: obs)
        monkeypatch.setattr(ifl, "fetch_event_markets", lambda *a, **k: [])
        result = ifl.scan_event_day(SERIES, self.DAY)
        assert result["skip_reason"] == "no_market_data"

    def test_unknown_series_raises(self):
        with pytest.raises(ValueError):
            ifl.scan_event_day("BOGUS-SERIES", self.DAY)


# ---------------------------------------------------------------------------
# _build_report / format_report: primary deliverable wiring
# ---------------------------------------------------------------------------

class TestBuildReport:
    def test_falsification_flag_and_rate_at_headline_margin(self):
        dead_yes = {
            "event_key": EVENT_TICKER, "ticker": "DEAD_BUT_YES", "station": STATION, "date_lst": "2025-01-01",
            "safety_margin_f": 2.0, "decision_instant_utc": "2025-01-01T16:03:00Z",
            "running_max_f": 80.0, "n_obs": 1, "lower_bound_f": 70.5, "upper_bound_f": 71.5,
            "status": "finalized", "result": "yes", "settled_yes": True,
        }
        per_margin_dead = {m: [] for m in ifl.SAFETY_MARGINS_F}
        per_margin_dead[2.0] = [dead_yes]
        per_margin_trades = {m: [] for m in ifl.SAFETY_MARGINS_F}
        per_margin_repeat = {m: 0 for m in ifl.SAFETY_MARGINS_F}
        per_margin_skip = {m: {} for m in ifl.SAFETY_MARGINS_F}
        report = ifl._build_report(
            per_margin_trades, per_margin_dead, per_margin_repeat, per_margin_skip,
            bound_checks=[], day_skip_reasons={}, n_city_days_scanned=1,
            station_codes=[STATION], start_date=date(2025, 1, 1), end_date=date(2025, 1, 1),
            contracts=10, fee_type="quadratic", fee_multiplier=1.0, period_interval=60,
            safety_margins=ifl.SAFETY_MARGINS_F, protocol_verified=False,
            n_resamples=100, confidence=0.9, seed=0,
        )
        headline = report["by_margin"]["2.0"]
        assert headline["n_dead_brackets"] == 1
        assert headline["n_dead_settled_yes"] == 1
        assert headline["dead_settled_yes_rate"] == 1.0
        assert report["falsified_at_headline_margin"] is True
        text = ifl.format_report(report)
        assert "FALSIFIED" in text
        assert "DEAD_BUT_YES" in text

    def test_clean_report_has_no_falsification(self):
        per_margin_dead = {m: [] for m in ifl.SAFETY_MARGINS_F}
        per_margin_trades = {m: [] for m in ifl.SAFETY_MARGINS_F}
        per_margin_repeat = {m: 0 for m in ifl.SAFETY_MARGINS_F}
        per_margin_skip = {m: {} for m in ifl.SAFETY_MARGINS_F}
        report = ifl._build_report(
            per_margin_trades, per_margin_dead, per_margin_repeat, per_margin_skip,
            bound_checks=[], day_skip_reasons={}, n_city_days_scanned=0,
            station_codes=[STATION], start_date=date(2025, 1, 1), end_date=date(2025, 1, 1),
            contracts=10, fee_type="quadratic", fee_multiplier=1.0, period_interval=60,
            safety_margins=ifl.SAFETY_MARGINS_F, protocol_verified=False,
            n_resamples=100, confidence=0.9, seed=0,
        )
        assert report["falsified_at_headline_margin"] is False
        assert report["by_margin"]["2.0"]["dead_settled_yes_rate"] is None
        text = ifl.format_report(report)
        assert "FALSIFIED" not in text
        assert ifl.DEPTH_LIMITATION_NOTE in text


# ---------------------------------------------------------------------------
# _enforce_period_guards: training window only
# ---------------------------------------------------------------------------

def _declare_valid_protocol(**overrides):
    kwargs = dict(
        hypothesis="intraday_floor: ASOS-dead weather brackets never settle YES",
        primary_metric="dead_settled_yes_rate",
        secondary_metrics=(),
        min_events=10,
        decision_threshold=0.0,
        stopping_rule="stop after the fixed training-window evaluation",
        run_kind="replay",
        declared_by="test-suite",
        declared_at="2025-06-01T00:00:00Z",
    )
    kwargs.update(overrides)
    return protocol_mod.declare(**kwargs)


class TestEnforcePeriodGuards:
    def test_no_guard_needed_within_train_period(self):
        assert ifl._enforce_period_guards(ifl.TRAIN_END, None) is None

    def test_past_train_end_without_protocol_raises(self):
        with pytest.raises(ValueError, match="predeclared"):
            ifl._enforce_period_guards(ifl.TRAIN_END + timedelta(days=1), None)

    def test_valid_protocol_past_train_end_is_accepted(self, tmp_path):
        protocol = _declare_valid_protocol()
        path = tmp_path / "protocol.json"
        protocol_mod.save(protocol, path)
        result = ifl._enforce_period_guards(ifl.TRAIN_END + timedelta(days=10), path)
        assert result is not None
        assert result.content_hash == protocol.content_hash

    def test_tampered_protocol_file_is_rejected(self, tmp_path):
        protocol = _declare_valid_protocol()
        path = tmp_path / "protocol.json"
        protocol_mod.save(protocol, path)
        raw = json.loads(path.read_text())
        raw["decision_threshold"] = 999.0
        path.write_text(json.dumps(raw))
        with pytest.raises(ValueError, match="failed verification"):
            ifl._enforce_period_guards(ifl.TRAIN_END + timedelta(days=10), path)

    def test_wrong_run_kind_rejected(self, tmp_path):
        protocol = _declare_valid_protocol(run_kind="synthetic")
        path = tmp_path / "protocol.json"
        protocol_mod.save(protocol, path)
        with pytest.raises(ValueError, match="run_kind"):
            ifl._enforce_period_guards(ifl.TRAIN_END + timedelta(days=10), path)

    def test_missing_marker_rejected(self, tmp_path):
        protocol = _declare_valid_protocol(hypothesis="unrelated experiment")
        path = tmp_path / "protocol.json"
        protocol_mod.save(protocol, path)
        with pytest.raises(ValueError, match="marker"):
            ifl._enforce_period_guards(ifl.TRAIN_END + timedelta(days=10), path)

    def test_missing_protocol_file_raises(self, tmp_path):
        with pytest.raises(ValueError):
            ifl._enforce_period_guards(ifl.TRAIN_END + timedelta(days=10), tmp_path / "nope.json")


# ---------------------------------------------------------------------------
# Staleness bucketing
# ---------------------------------------------------------------------------

class TestStalenessBucketLabel:
    def test_negative_and_small_values_go_in_first_bucket(self):
        assert ifl._staleness_bucket_label(-5.0) == "<15m"
        assert ifl._staleness_bucket_label(0.0) == "<15m"
        assert ifl._staleness_bucket_label(14.9) == "<15m"

    def test_boundaries(self):
        assert ifl._staleness_bucket_label(15.0) == "15-30m"
        assert ifl._staleness_bucket_label(30.0) == "30-60m"
        assert ifl._staleness_bucket_label(60.0) == "60-180m"
        assert ifl._staleness_bucket_label(180.0) == ">=180m"
        assert ifl._staleness_bucket_label(1000.0) == ">=180m"


# ---------------------------------------------------------------------------
# run_backtest: end-to-end wiring with fully mocked fetchers
# ---------------------------------------------------------------------------

class TestRunBacktestEndToEnd:
    DAY = date(2025, 1, 1)

    def _patch_all(self, monkeypatch, obs_rows, raw_markets, candles_by_ticker):
        monkeypatch.setattr(
            ifl, "get_historical_cutoff",
            lambda *a, **k: {"market_settled_ts": datetime(2025, 6, 1, tzinfo=timezone.utc)},
        )
        monkeypatch.setattr(observations, "fetch_asos_observations", lambda *a, **k: obs_rows)
        monkeypatch.setattr(ifl, "fetch_event_markets", lambda *a, **k: raw_markets)

        def fake_candles(series, ticker, event_ticker, start_ts, end_ts, period_interval, session=None, cutoff=None):
            return candles_by_ticker.get(ticker, [])

        monkeypatch.setattr(ifl, "fetch_candlesticks", fake_candles)

    def test_full_pipeline_produces_a_trade_and_primary_deliverable(self, monkeypatch):
        offset = ifl.STATION_STANDARD_UTC_OFFSET_HOURS[STATION]
        day_start = ifl._local_day_start(self.DAY, offset)
        obs_valid = day_start + timedelta(hours=10, minutes=53)
        decision_instant = obs_valid + timedelta(minutes=ifl.OBSERVATION_LATENCY_MINUTES)

        obs_rows = [{
            "valid_utc": obs_valid.isoformat().replace("+00:00", "Z"), "temp_f": 90.0, "station_code": STATION,
        }]
        raw_markets = [
            {
                "ticker": "KXHIGHNY-25JAN01-B70.5", "event_ticker": EVENT_TICKER,
                "strike_type": "between", "floor_strike": 70, "cap_strike": 71,
                "result": "no", "status": "finalized",
                "rules_primary": "National Weather Service's Climatological Report (Daily)",
                "close_time": "2025-01-02T04:59:00Z",
                "open_time": day_start.isoformat().replace("+00:00", "Z"),
            },
        ]
        candles_by_ticker = {
            "KXHIGHNY-25JAN01-B70.5": [_candle(int(decision_instant.timestamp()), bid_cents=3)],
        }
        self._patch_all(monkeypatch, obs_rows, raw_markets, candles_by_ticker)

        report = ifl.run_backtest([STATION], self.DAY, self.DAY)
        headline = report["by_margin"]["2.0"]
        assert headline["n_dead_brackets"] == 1
        assert headline["n_trades"] == 1
        trade = headline["trades"][0]
        assert trade["bid_cents"] == 3
        expected_fee = trading_fee_cents(97, 10, fee_type="quadratic", fee_multiplier=1.0, is_maker=False)
        assert trade["fee_cents"] == expected_fee
        assert trade["pnl_cents"] == pytest.approx(100 * 10 - 97 * 10 - expected_fee)
        assert report["falsified_at_headline_margin"] is False
        assert report["n_city_days_scanned"] == 1

    def test_no_market_data_day_is_tallied_and_produces_no_trades(self, monkeypatch):
        offset = ifl.STATION_STANDARD_UTC_OFFSET_HOURS[STATION]
        day_start = ifl._local_day_start(self.DAY, offset)
        obs_rows = [{
            "valid_utc": (day_start + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
            "temp_f": 50.0, "station_code": STATION,
        }]
        self._patch_all(monkeypatch, obs_rows, [], {})
        report = ifl.run_backtest([STATION], self.DAY, self.DAY)
        assert report["n_city_days_scanned"] == 0
        assert report["day_skip_reasons"]["no_market_data"] == 1
        assert report["by_margin"]["2.0"]["n_trades"] == 0

    def test_transport_failure_is_a_counted_skip_not_a_crash(self, monkeypatch):
        """One upstream 503 must not discard every city-day already scanned.

        IEM returns 503 under sustained load, which is exactly what a
        multi-station year-long scan applies. A run that lost days to that
        has to say so in the report -- a silently shortened scan is
        indistinguishable from a clean one.
        """
        monkeypatch.setattr(
            ifl, "get_historical_cutoff",
            lambda *a, **k: {"market_settled_ts": datetime(2025, 6, 1, tzinfo=timezone.utc)},
        )

        def _boom(*args, **kwargs):
            raise requests.exceptions.HTTPError("503 Server Error: Service Unavailable")

        monkeypatch.setattr(ifl, "scan_event_day", _boom)
        report = ifl.run_backtest([STATION], self.DAY, self.DAY)
        assert report["day_skip_reasons"]["fetch_error"] == 1
        assert report["n_city_days_scanned"] == 0
        assert report["by_margin"]["2.0"]["n_trades"] == 0

    def test_logic_errors_still_crash_rather_than_counting_as_a_skip(self, monkeypatch):
        """The tolerance above is deliberately narrow: only transport errors.

        A bug in the scan must not disguise itself as an upstream outage.
        """
        monkeypatch.setattr(
            ifl, "get_historical_cutoff",
            lambda *a, **k: {"market_settled_ts": datetime(2025, 6, 1, tzinfo=timezone.utc)},
        )

        def _bug(*args, **kwargs):
            raise KeyError("yes_bid_cents")

        monkeypatch.setattr(ifl, "scan_event_day", _bug)
        with pytest.raises(KeyError):
            ifl.run_backtest([STATION], self.DAY, self.DAY)

    def test_start_after_end_raises(self):
        with pytest.raises(ValueError):
            ifl.run_backtest([STATION], date(2025, 1, 5), date(2025, 1, 1))

    def test_unmapped_station_raises(self, monkeypatch):
        monkeypatch.setattr(
            ifl, "get_historical_cutoff",
            lambda *a, **k: {"market_settled_ts": datetime(2025, 6, 1, tzinfo=timezone.utc)},
        )
        with pytest.raises(ValueError, match="No Kalshi series mapped"):
            ifl.run_backtest(["ZZZZ"], self.DAY, self.DAY)
