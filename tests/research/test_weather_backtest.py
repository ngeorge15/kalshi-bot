"""Unit tests for src/research/weather_backtest.py — the NBM-vs-Kalshi backtest engine.

No real network calls: `fetch_nbm_previous_runs`, `fetch_cli_daily_highs`, and
`fetch_event_at_decision` are monkeypatched at the `src.research.weather_backtest`
module level (the names that module imported into its own namespace).
"""
from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta, timezone
from statistics import NormalDist

import pytest

from src.data.kalshi_history import PriceAtInstant
from src.paper import protocol as protocol_mod
from src.paper.fees import trading_fee_cents
from src.research import weather_backtest as wb


# ---------------------------------------------------------------------------
# select_forecast_values: lead1/lead2 selection at the latency boundary
# ---------------------------------------------------------------------------

class TestSelectForecastValues:
    def test_unknown_variant_raises(self):
        with pytest.raises(ValueError):
            wb.select_forecast_values("bogus", {}, [], datetime.now(timezone.utc))

    def test_lead2_always_uses_lead2(self):
        valid = datetime(2025, 1, 1, 5, tzinfo=timezone.utc)
        hourly = {valid: {"lead1": 40.0, "lead2": 42.0}}
        values = wb.select_forecast_values("lead2", hourly, [valid], valid - timedelta(days=10))
        assert values == [42.0]

    def test_lead2_missing_raises(self):
        valid = datetime(2025, 1, 1, 5, tzinfo=timezone.utc)
        hourly = {valid: {"lead1": 40.0, "lead2": None}}
        with pytest.raises(wb.MissingForecastError):
            wb.select_forecast_values("lead2", hourly, [valid], valid)

    def test_lead1_used_exactly_at_latency_boundary(self):
        valid = datetime(2025, 1, 2, 0, 0, tzinfo=timezone.utc)
        # lead1 "available at" = valid - 24h + 2h = valid - 22h.
        decision_time = valid - timedelta(hours=22)
        hourly = {valid: {"lead1": 40.0, "lead2": 42.0}}
        values = wb.select_forecast_values("lead1_guarded", hourly, [valid], decision_time)
        assert values == [40.0]

    def test_lead1_not_yet_available_one_second_before_boundary_falls_back_to_lead2(self):
        valid = datetime(2025, 1, 2, 0, 0, tzinfo=timezone.utc)
        decision_time = valid - timedelta(hours=22) - timedelta(seconds=1)
        hourly = {valid: {"lead1": 40.0, "lead2": 42.0}}
        values = wb.select_forecast_values("lead1_guarded", hourly, [valid], decision_time)
        assert values == [42.0]

    def test_lead1_guarded_falls_back_when_lead1_missing_but_available(self):
        valid = datetime(2025, 1, 2, 0, 0, tzinfo=timezone.utc)
        decision_time = valid  # long past the availability boundary
        hourly = {valid: {"lead1": None, "lead2": 42.0}}
        values = wb.select_forecast_values("lead1_guarded", hourly, [valid], decision_time)
        assert values == [42.0]

    def test_lead1_guarded_raises_when_neither_available(self):
        valid = datetime(2025, 1, 2, 0, 0, tzinfo=timezone.utc)
        hourly = {valid: {"lead1": None, "lead2": None}}
        with pytest.raises(wb.MissingForecastError):
            wb.select_forecast_values("lead1_guarded", hourly, [valid], valid)

    def test_missing_hour_entirely_treated_as_missing(self):
        valid = datetime(2025, 1, 2, 0, 0, tzinfo=timezone.utc)
        with pytest.raises(wb.MissingForecastError):
            wb.select_forecast_values("lead1_guarded", {}, [valid], valid)


# ---------------------------------------------------------------------------
# hourly_forecast_frame / forecast_max_for_day
# ---------------------------------------------------------------------------

class TestForecastMaxForDay:
    def test_padding_window_and_max(self, monkeypatch):
        requested = {}

        def fake_fetch(station_code, start_date, end_date, session=None):
            requested["start"], requested["end"] = start_date, end_date
            day = date(2025, 6, 15)
            offset = wb.STATION_STANDARD_UTC_OFFSET_HOURS[station_code]
            start = wb._local_day_start(day, offset)
            rows = []
            for h in range(24):
                valid = start + timedelta(hours=h)
                rows.append({"valid_utc": valid.isoformat().replace("+00:00", "Z"), "lead1_f": 50.0 + h, "lead2_f": 10.0 - h * 0.1})
            return rows

        monkeypatch.setattr(wb, "fetch_nbm_previous_runs", fake_fetch)
        result = wb.forecast_max_for_day("KNYC", date(2025, 6, 15), "lead2")
        assert requested["start"] == date(2025, 6, 14)
        assert requested["end"] == date(2025, 6, 16)
        assert result == 10.0  # lead2 constant across all hours

    def test_lead1_guarded_max_picks_highest_selected_value(self, monkeypatch):
        day = date(2025, 6, 15)
        offset = wb.STATION_STANDARD_UTC_OFFSET_HOURS["KNYC"]
        start = wb._local_day_start(day, offset)

        def fake_fetch(station_code, start_date, end_date, session=None):
            rows = []
            for h in range(24):
                valid = start + timedelta(hours=h)
                # lead1 rises through the day; lead2 constant and lower.
                rows.append({"valid_utc": valid.isoformat().replace("+00:00", "Z"), "lead1_f": 50.0 + h, "lead2_f": 5.0 - h * 0.1})
            return rows

        monkeypatch.setattr(wb, "fetch_nbm_previous_runs", fake_fetch)
        # decision_time far enough after start that every hour's lead1 is
        # "available" under the latency rule.
        result = wb.forecast_max_for_day(
            "KNYC", day, "lead1_guarded", decision_time=start + timedelta(hours=48),
        )
        assert result == 50.0 + 23

    def test_missing_hour_raises(self, monkeypatch):
        monkeypatch.setattr(wb, "fetch_nbm_previous_runs", lambda *a, **k: [])
        with pytest.raises(wb.MissingForecastError):
            wb.forecast_max_for_day("KNYC", date(2025, 6, 15), "lead2")


# ---------------------------------------------------------------------------
# fit_bias_sigma: train-only, overlap refusal
# ---------------------------------------------------------------------------

class TestFitBiasSigma:
    def _patch_constant_forecast(self, monkeypatch, value=70.0):
        day_anchor = None

        def fake_fetch(station_code, start_date, end_date, session=None):
            offset = wb.STATION_STANDARD_UTC_OFFSET_HOURS[station_code]
            rows = []
            d = start_date
            while d <= end_date:
                start = wb._local_day_start(d, offset)
                for h in range(24):
                    valid = start + timedelta(hours=h)
                    rows.append({"valid_utc": valid.isoformat().replace("+00:00", "Z"), "lead1_f": value - h * 0.01, "lead2_f": value - h * 0.01})
                d += timedelta(days=1)
            return rows

        monkeypatch.setattr(wb, "fetch_nbm_previous_runs", fake_fetch)

    def test_train_start_after_end_raises(self):
        with pytest.raises(ValueError):
            wb.fit_bias_sigma(["KNYC"], "lead2", date(2025, 1, 5), date(2025, 1, 1))

    def test_eval_only_one_side_given_raises(self):
        with pytest.raises(ValueError):
            wb.fit_bias_sigma(
                ["KNYC"], "lead2", date(2025, 1, 1), date(2025, 1, 2), eval_start=date(2025, 2, 1),
            )

    def test_overlap_between_train_and_eval_refused(self, monkeypatch):
        self._patch_constant_forecast(monkeypatch)
        monkeypatch.setattr(
            wb, "fetch_cli_daily_highs",
            lambda station, start, end, session=None: [
                {"date_lst": "2025-01-01", "max_f": 69.0, "source": "x"},
                {"date_lst": "2025-01-02", "max_f": 71.0, "source": "x"},
            ],
        )
        with pytest.raises(ValueError, match="overlaps"):
            wb.fit_bias_sigma(
                ["KNYC"], "lead2",
                train_start=date(2025, 1, 1), train_end=date(2025, 1, 10),
                eval_start=date(2025, 1, 5), eval_end=date(2025, 1, 20),
            )

    def test_non_overlapping_eval_is_accepted_and_bias_sigma_computed(self, monkeypatch):
        self._patch_constant_forecast(monkeypatch, value=70.0)
        monkeypatch.setattr(
            wb, "fetch_cli_daily_highs",
            lambda station, start, end, session=None: [
                {"date_lst": "2025-01-01", "max_f": 69.0, "source": "x"},
                {"date_lst": "2025-01-02", "max_f": 71.0, "source": "x"},
            ],
        )
        fits = wb.fit_bias_sigma(
            ["KNYC"], "lead2",
            train_start=date(2025, 1, 1), train_end=date(2025, 1, 2),
            eval_start=date(2025, 2, 1), eval_end=date(2025, 2, 2),
        )
        fit = fits["KNYC"]
        assert fit.n == 2
        assert fit.bias_f == pytest.approx(0.0)
        assert fit.sigma_f == pytest.approx(math.sqrt(2))

    def test_station_with_fewer_than_two_days_is_skipped(self, monkeypatch):
        self._patch_constant_forecast(monkeypatch, value=70.0)
        monkeypatch.setattr(
            wb, "fetch_cli_daily_highs",
            lambda station, start, end, session=None: [
                {"date_lst": "2025-01-01", "max_f": 69.0, "source": "x"},
            ],
        )
        fits = wb.fit_bias_sigma(["KNYC"], "lead2", date(2025, 1, 1), date(2025, 1, 1))
        assert "KNYC" not in fits


# ---------------------------------------------------------------------------
# bracket_probability partition (leak-style self check: full partition sums to 1)
# ---------------------------------------------------------------------------

def test_bracket_probabilities_sum_to_one_across_a_full_partition():
    from src.paper.weather import bracket_probability

    mu, sigma = 78.3, 3.1
    # Mirrors the real KXHIGHNY 6-bracket shape from research/kalshi-public-data.md:
    # less(cap=75), between(75,76), between(77,78), between(79,80), between(81,82), greater(floor=82)
    bounds = [
        (None, 74.5),
        (74.5, 76.5),
        (76.5, 78.5),
        (78.5, 80.5),
        (80.5, 82.5),
        (82.5, None),
    ]
    total = sum(bracket_probability(mu, sigma, lo, hi) for lo, hi in bounds)
    assert total == pytest.approx(1.0, abs=1e-9)


# ---------------------------------------------------------------------------
# evaluate_sides: EV/fee arithmetic
# ---------------------------------------------------------------------------

class TestEvaluateSides:
    def test_yes_and_no_ev_match_hand_computed_fees(self):
        p_yes = 0.6
        yes_bid, yes_ask = 45, 50
        sides = wb.evaluate_sides(p_yes, yes_bid, yes_ask, "quadratic", 1.0)
        expected_yes_fee = trading_fee_cents(yes_ask, 1, fee_type="quadratic", fee_multiplier=1.0, is_maker=False)
        expected_no_fee = trading_fee_cents(100 - yes_bid, 1, fee_type="quadratic", fee_multiplier=1.0, is_maker=False)
        assert sides["yes"]["fee_cents"] == expected_yes_fee
        assert sides["yes"]["cost_cents"] == yes_ask
        assert sides["yes"]["ev_cents"] == pytest.approx(p_yes * 100 - yes_ask - expected_yes_fee)
        assert sides["no"]["fee_cents"] == expected_no_fee
        assert sides["no"]["cost_cents"] == 100 - yes_bid
        assert sides["no"]["ev_cents"] == pytest.approx((1 - p_yes) * 100 - (100 - yes_bid) - expected_no_fee)

    def test_side_excluded_when_cost_at_boundary_zero_or_hundred(self):
        # yes_ask == 100 -> yes cost 100, untradeable; yes_bid == 0 -> no cost 100, untradeable.
        sides = wb.evaluate_sides(0.5, 0, 100, "quadratic", 1.0)
        assert sides["yes"] is None
        assert sides["no"] is None

    def test_side_included_at_minimum_and_maximum_tradeable_cost(self):
        sides = wb.evaluate_sides(0.5, 99, 1, "quadratic", 1.0)
        assert sides["yes"] is not None  # cost 1
        assert sides["no"] is not None  # cost 1


# ---------------------------------------------------------------------------
# select_top_trades_for_event: per-event position cap
# ---------------------------------------------------------------------------

class TestSelectTopTradesForEvent:
    def test_caps_at_max_positions_highest_ev_first(self):
        candidates = [
            {"ev_cents": 5.0, "id": "a"},
            {"ev_cents": 20.0, "id": "b"},
            {"ev_cents": 12.0, "id": "c"},
        ]
        top1 = wb.select_top_trades_for_event(candidates, 1)
        assert [c["id"] for c in top1] == ["b"]
        top2 = wb.select_top_trades_for_event(candidates, 2)
        assert [c["id"] for c in top2] == ["b", "c"]

    def test_fewer_candidates_than_cap_returns_all(self):
        candidates = [{"ev_cents": 1.0, "id": "a"}]
        assert wb.select_top_trades_for_event(candidates, 5) == candidates

    def test_zero_cap_returns_nothing(self):
        candidates = [{"ev_cents": 1.0, "id": "a"}]
        assert wb.select_top_trades_for_event(candidates, 0) == []


# ---------------------------------------------------------------------------
# settle_pnl_cents: YES/NO wins and losses
# ---------------------------------------------------------------------------

class TestSettlePnlCents:
    def test_yes_win(self):
        assert wb.settle_pnl_cents("yes", cost_cents=30, fee_cents=2, result="yes") == 100 - 30 - 2

    def test_yes_loss(self):
        assert wb.settle_pnl_cents("yes", cost_cents=30, fee_cents=2, result="no") == 0 - 30 - 2

    def test_no_win(self):
        assert wb.settle_pnl_cents("no", cost_cents=70, fee_cents=2, result="no") == 100 - 70 - 2

    def test_no_loss(self):
        assert wb.settle_pnl_cents("no", cost_cents=70, fee_cents=2, result="yes") == 0 - 70 - 2


# ---------------------------------------------------------------------------
# Guards: missing protocol, tampered protocol, post-Aug-13 settlement switch
# ---------------------------------------------------------------------------

def _declare_valid_protocol(**overrides):
    kwargs = dict(
        hypothesis="weather_backtest: archived NBM guidance beats Kalshi bracket prices",
        primary_metric="event_clustered_paired_brier_improvement",
        secondary_metrics=(),
        min_events=10,
        decision_threshold=0.01,
        stopping_rule="stop after the fixed 2025-07-01..2025-12-31 evaluation window",
        run_kind="replay",
        declared_by="test-suite",
        declared_at="2025-06-01T00:00:00Z",
    )
    kwargs.update(overrides)
    return protocol_mod.declare(**kwargs)


class TestEnforcePeriodGuards:
    def test_no_guard_needed_within_train_period(self):
        result = wb._enforce_period_guards(wb.TRAIN_END, None, False)
        assert result is None

    def test_past_train_end_without_protocol_raises(self):
        with pytest.raises(ValueError, match="predeclared"):
            wb._enforce_period_guards(wb.TRAIN_END + timedelta(days=1), None, False)

    def test_past_switch_date_without_allow_flag_raises_even_with_protocol(self, tmp_path):
        protocol = _declare_valid_protocol()
        path = tmp_path / "protocol.json"
        protocol_mod.save(protocol, path)
        with pytest.raises(ValueError, match="settlement-source switch"):
            wb._enforce_period_guards(
                wb.SETTLEMENT_SOURCE_SWITCH_DATE + timedelta(days=1), path, False,
            )

    def test_valid_protocol_within_train_and_switch_window_is_accepted(self, tmp_path):
        protocol = _declare_valid_protocol()
        path = tmp_path / "protocol.json"
        protocol_mod.save(protocol, path)
        eval_end = wb.TRAIN_END + timedelta(days=10)
        assert eval_end <= wb.SETTLEMENT_SOURCE_SWITCH_DATE
        result = wb._enforce_period_guards(eval_end, path, False)
        assert result is not None
        assert result.content_hash == protocol.content_hash

    def test_tampered_protocol_file_is_rejected(self, tmp_path):
        protocol = _declare_valid_protocol()
        path = tmp_path / "protocol.json"
        protocol_mod.save(protocol, path)
        # Hand-edit one field on disk without recomputing content_hash.
        raw = json.loads(path.read_text())
        raw["decision_threshold"] = 0.0001
        path.write_text(json.dumps(raw))
        with pytest.raises(ValueError, match="failed verification"):
            wb._enforce_period_guards(wb.TRAIN_END + timedelta(days=10), path, False)

    def test_wrong_run_kind_rejected(self, tmp_path):
        protocol = _declare_valid_protocol(run_kind="synthetic")
        path = tmp_path / "protocol.json"
        protocol_mod.save(protocol, path)
        with pytest.raises(ValueError, match="run_kind"):
            wb._enforce_period_guards(wb.TRAIN_END + timedelta(days=10), path, False)

    def test_missing_hypothesis_marker_rejected(self, tmp_path):
        protocol = _declare_valid_protocol(hypothesis="unrelated experiment about something else")
        path = tmp_path / "protocol.json"
        protocol_mod.save(protocol, path)
        with pytest.raises(ValueError, match="marker"):
            wb._enforce_period_guards(wb.TRAIN_END + timedelta(days=10), path, False)

    def test_missing_protocol_file_path_rejected(self, tmp_path):
        missing = tmp_path / "nope.json"
        with pytest.raises(ValueError):
            wb._enforce_period_guards(wb.TRAIN_END + timedelta(days=10), missing, False)

    def test_past_switch_date_with_allow_flag_and_valid_protocol_accepted(self, tmp_path):
        protocol = _declare_valid_protocol()
        path = tmp_path / "protocol.json"
        protocol_mod.save(protocol, path)
        eval_end = wb.SETTLEMENT_SOURCE_SWITCH_DATE + timedelta(days=1)
        result = wb._enforce_period_guards(eval_end, path, True)
        assert result is not None


# ---------------------------------------------------------------------------
# calibration_bins / max_drawdown_cents / event_clustered_bootstrap_ci
# ---------------------------------------------------------------------------

class TestCalibrationBins:
    def test_buckets_by_predicted_probability(self):
        rows = [{"p": 0.05, "outcome": 0.0}, {"p": 0.12, "outcome": 1.0}, {"p": 0.95, "outcome": 1.0}]
        bins = wb.calibration_bins(rows, n_bins=10)
        assert len(bins) == 10
        assert bins[0]["n"] == 1  # 0.05 -> bin [0.0, 0.1)
        assert bins[1]["n"] == 1  # 0.12 -> bin [0.1, 0.2)
        assert bins[9]["n"] == 1  # 0.95 -> bin [0.9, 1.0)

    def test_p_exactly_one_lands_in_last_bin_not_out_of_range(self):
        bins = wb.calibration_bins([{"p": 1.0, "outcome": 1.0}], n_bins=10)
        assert bins[9]["n"] == 1
        assert sum(b["n"] for b in bins) == 1

    def test_empty_bucket_reports_none_not_nan(self):
        bins = wb.calibration_bins([{"p": 0.05, "outcome": 0.0}], n_bins=10)
        assert bins[5]["n"] == 0
        assert bins[5]["mean_predicted_p"] is None
        assert bins[5]["empirical_frequency"] is None


class TestMaxDrawdownCents:
    def test_no_drawdown_when_monotonically_increasing(self):
        trades = [
            {"date_lst": "2025-01-01", "event_key": "e1", "ticker": "t1", "side": "yes", "pnl_cents": 100},
            {"date_lst": "2025-01-02", "event_key": "e2", "ticker": "t2", "side": "yes", "pnl_cents": 50},
        ]
        assert wb.max_drawdown_cents(trades) == 0.0

    def test_drawdown_after_a_peak(self):
        trades = [
            {"date_lst": "2025-01-01", "event_key": "e1", "ticker": "t1", "side": "yes", "pnl_cents": 100},
            {"date_lst": "2025-01-02", "event_key": "e2", "ticker": "t2", "side": "yes", "pnl_cents": -150},
            {"date_lst": "2025-01-03", "event_key": "e3", "ticker": "t3", "side": "yes", "pnl_cents": 20},
        ]
        # Peak after day1 = 100; trough after day2 = -50 -> drawdown = -150.
        assert wb.max_drawdown_cents(trades) == -150.0

    def test_empty_trades(self):
        assert wb.max_drawdown_cents([]) == 0.0


class TestEventClusteredBootstrapCi:
    def test_empty_input(self):
        result = wb.event_clustered_bootstrap_ci({})
        assert result["estimate"] is None
        assert result["ci_low"] is None
        assert result["n_events"] == 0

    def test_single_event_has_no_ci(self):
        result = wb.event_clustered_bootstrap_ci({"e1": 100.0})
        assert result["estimate"] == 100.0
        assert result["ci_low"] is None
        assert result["ci_high"] is None

    def test_multi_event_has_a_ci_bracketing_the_mean(self):
        result = wb.event_clustered_bootstrap_ci({"e1": 100.0, "e2": -50.0, "e3": 20.0}, n_resamples=500, seed=0)
        assert result["estimate"] == pytest.approx((100.0 - 50.0 + 20.0) / 3)
        assert result["ci_low"] <= result["estimate"] <= result["ci_high"]


# ---------------------------------------------------------------------------
# run_fetch_prices: thin JSONL-writing wrapper
# ---------------------------------------------------------------------------

class TestRunFetchPrices:
    def test_writes_one_row_per_market_and_serializes_price(self, monkeypatch, tmp_path):
        price = PriceAtInstant(status="ok", yes_bid_cents=10, yes_ask_cents=12, candle_end_utc="x", age_hours=0.1)
        rows = [
            {"ticker": "A", "event_ticker": "E1", "date_lst": "2025-01-01", "price": price},
            {"ticker": "B", "event_ticker": "E1", "date_lst": "2025-01-01", "price": price},
        ]
        monkeypatch.setattr(wb, "fetch_event_at_decision", lambda series, target_date, session=None, **k: rows)
        out = tmp_path / "out.jsonl"
        count = wb.run_fetch_prices(["KNYC"], date(2025, 1, 1), date(2025, 1, 1), out)
        assert count == 2
        lines = out.read_text().strip().splitlines()
        assert len(lines) == 2
        parsed = json.loads(lines[0])
        assert parsed["price"]["status"] == "ok"
        assert parsed["price"]["yes_bid_cents"] == 10

    def test_start_after_end_raises(self, tmp_path):
        with pytest.raises(ValueError):
            wb.run_fetch_prices(["KNYC"], date(2025, 1, 2), date(2025, 1, 1), tmp_path / "out.jsonl")


# ---------------------------------------------------------------------------
# run_backtest: end-to-end with fully mocked fetchers
# ---------------------------------------------------------------------------

class TestRunBacktestEndToEnd:
    STATION = "KNYC"
    SERIES = "KXHIGHNY"
    TRAIN_START = date(2024, 12, 1)
    TRAIN_END = date(2024, 12, 2)
    EVAL_DATE = date(2025, 1, 1)

    def _patch_forecast(self, monkeypatch, value=70.0):
        def fake_fetch(station_code, start_date, end_date, session=None):
            offset = wb.STATION_STANDARD_UTC_OFFSET_HOURS[station_code]
            rows = []
            d = start_date
            while d <= end_date:
                start = wb._local_day_start(d, offset)
                for h in range(24):
                    valid = start + timedelta(hours=h)
                    rows.append({"valid_utc": valid.isoformat().replace("+00:00", "Z"), "lead1_f": value - h * 0.01, "lead2_f": value - h * 0.01})
                d += timedelta(days=1)
            return rows

        monkeypatch.setattr(wb, "fetch_nbm_previous_runs", fake_fetch)

    def _patch_cli(self, monkeypatch):
        monkeypatch.setattr(
            wb, "fetch_cli_daily_highs",
            lambda station, start, end, session=None: [
                {"date_lst": "2024-12-01", "max_f": 69.0, "source": "x"},
                {"date_lst": "2024-12-02", "max_f": 71.0, "source": "x"},
            ],
        )

    def _make_price(self, bid, ask):
        return PriceAtInstant(
            status="ok", yes_bid_cents=bid, yes_ask_cents=ask,
            candle_end_utc="2025-01-01T04:59:00Z", age_hours=0.0, volume=10.0, open_interest=10.0,
        )

    def _patch_market(self, monkeypatch, mu, sigma):
        event_ticker = "KXHIGHNY-25JAN01"

        def fake_fetch_event(series, target_date, session=None, **kwargs):
            assert series == self.SERIES
            assert target_date == self.EVAL_DATE
            return [
                {
                    "ticker": f"{event_ticker}-T70", "event_ticker": event_ticker, "station": self.STATION,
                    "date_lst": "2025-01-01", "lower_bound_f": None, "upper_bound_f": 69.5,
                    "result": "no", "settlement_source": "nws_cli", "close_time": "...", "status": "finalized",
                    "decision_time_utc": "2025-01-01T04:59:00Z",
                    "price": self._make_price(34, 38),
                },
                {
                    "ticker": f"{event_ticker}-B69.5", "event_ticker": event_ticker, "station": self.STATION,
                    "date_lst": "2025-01-01", "lower_bound_f": 69.5, "upper_bound_f": 71.5,
                    "result": "yes", "settlement_source": "nws_cli", "close_time": "...", "status": "finalized",
                    "decision_time_utc": "2025-01-01T04:59:00Z",
                    "price": self._make_price(25, 30),
                },
                {
                    "ticker": f"{event_ticker}-T71", "event_ticker": event_ticker, "station": self.STATION,
                    "date_lst": "2025-01-01", "lower_bound_f": 71.5, "upper_bound_f": None,
                    "result": "no", "settlement_source": "nws_cli", "close_time": "...", "status": "finalized",
                    "decision_time_utc": "2025-01-01T04:59:00Z",
                    "price": self._make_price(12, 16),
                },
            ]

        monkeypatch.setattr(wb, "fetch_event_at_decision", fake_fetch_event)
        return event_ticker

    def test_single_favorable_bracket_trades_and_settles(self, monkeypatch):
        self._patch_forecast(monkeypatch, value=70.0)
        self._patch_cli(monkeypatch)
        # bias=0, sigma=sqrt(2) from the two training days (69, 71 vs constant forecast 70).
        mu, sigma = 70.0, math.sqrt(2)
        event_ticker = self._patch_market(monkeypatch, mu, sigma)

        report = wb.run_backtest(
            station_codes=[self.STATION], variant="lead2",
            train_start=self.TRAIN_START, train_end=self.TRAIN_END,
            eval_start=self.EVAL_DATE, eval_end=self.EVAL_DATE,
            min_edge=0.05,
        )

        assert report["n_trades"] == 1
        trade = report["trades"][0]
        assert trade["side"] == "yes"
        assert trade["ticker"] == f"{event_ticker}-B69.5"

        nd = NormalDist(mu=mu, sigma=sigma)
        p_yes = nd.cdf(71.5) - nd.cdf(69.5)
        expected_fee = trading_fee_cents(30, 1, fee_type="quadratic", fee_multiplier=1.0, is_maker=False)
        expected_ev = p_yes * 100 - 30 - expected_fee
        assert expected_ev / 100.0 >= 0.05  # sanity: this is really the favorable side
        assert trade["ev_cents"] == pytest.approx(expected_ev)

        expected_pnl_per_contract = 100 - 30 - expected_fee  # settled "yes", side "yes" -> win
        assert trade["pnl_cents"] == expected_pnl_per_contract * wb.DEFAULT_CONTRACTS_PER_TRADE
        assert report["total_pnl_cents"] == expected_pnl_per_contract * wb.DEFAULT_CONTRACTS_PER_TRADE
        assert report["hit_rate"] == 1.0
        assert report["n_events_traded"] == 1
        assert report["skip_reasons"].get("stale", 0) == 0
        assert report["skip_reasons"].get("missing", 0) == 0
        # No brier/EV candidate should have been generated for the two
        # unfavorable brackets given how their prices were set.
        assert len(report["trades"]) == 1

    def test_report_is_json_serializable_with_allow_nan_false(self, monkeypatch):
        self._patch_forecast(monkeypatch, value=70.0)
        self._patch_cli(monkeypatch)
        mu, sigma = 70.0, math.sqrt(2)
        self._patch_market(monkeypatch, mu, sigma)

        report = wb.run_backtest(
            station_codes=[self.STATION], variant="lead2",
            train_start=self.TRAIN_START, train_end=self.TRAIN_END,
            eval_start=self.EVAL_DATE, eval_end=self.EVAL_DATE,
            min_edge=0.05,
        )
        # Must not raise -- allow_nan=False rejects any accidental NaN/Infinity.
        text = json.dumps(report, allow_nan=False, default=str)
        assert isinstance(text, str)

    def test_higher_min_edge_yields_no_trades(self, monkeypatch):
        self._patch_forecast(monkeypatch, value=70.0)
        self._patch_cli(monkeypatch)
        mu, sigma = 70.0, math.sqrt(2)
        self._patch_market(monkeypatch, mu, sigma)

        report = wb.run_backtest(
            station_codes=[self.STATION], variant="lead2",
            train_start=self.TRAIN_START, train_end=self.TRAIN_END,
            eval_start=self.EVAL_DATE, eval_end=self.EVAL_DATE,
            min_edge=0.99,
        )
        assert report["n_trades"] == 0
        assert report["total_pnl_cents"] == 0.0
        assert report["hit_rate"] is None

    def test_no_station_fit_is_skipped(self, monkeypatch):
        self._patch_forecast(monkeypatch, value=70.0)
        # Empty CLI truth -> no fit for KNYC.
        monkeypatch.setattr(wb, "fetch_cli_daily_highs", lambda *a, **k: [])

        def fail_fetch_event(*a, **k):
            raise AssertionError("fetch_event_at_decision should not be called without a station fit")

        monkeypatch.setattr(wb, "fetch_event_at_decision", fail_fetch_event)

        report = wb.run_backtest(
            station_codes=[self.STATION], variant="lead2",
            train_start=self.TRAIN_START, train_end=self.TRAIN_END,
            eval_start=self.EVAL_DATE, eval_end=self.EVAL_DATE,
            min_edge=0.05,
        )
        assert report["n_trades"] == 0
        assert report["skip_reasons"]["no_station_fit"] == 1

    def test_eval_past_train_end_without_protocol_raises(self, monkeypatch):
        self._patch_forecast(monkeypatch, value=70.0)
        self._patch_cli(monkeypatch)
        with pytest.raises(ValueError):
            wb.run_backtest(
                station_codes=[self.STATION], variant="lead2",
                train_start=self.TRAIN_START, train_end=self.TRAIN_END,
                eval_start=wb.TRAIN_END + timedelta(days=1),
                eval_end=wb.TRAIN_END + timedelta(days=1),
                min_edge=0.05,
            )


class TestDegenerateLeadIsNotTraded:
    """A constant fill-value lead must never reach a trade."""

    def _day(self, lead1, lead2):
        start = wb._local_day_start(date(2025, 6, 15), -5)
        needed = [start + timedelta(hours=h) for h in range(24)]
        hourly = {v: {"lead1": lead1(i), "lead2": lead2(i)} for i, v in enumerate(needed)}
        return hourly, needed, start - timedelta(minutes=1)

    def test_lead2_variant_refuses_a_constant_lead2(self):
        hourly, needed, decision = self._day(lambda i: 70.0 + i * 0.1, lambda i: 32.0)
        with pytest.raises(wb.MissingForecastError):
            wb.select_forecast_values("lead2", hourly, needed, decision)

    def test_guarded_variant_does_not_fall_back_to_a_constant_lead2(self):
        # lead1 is not yet published for the day's last hours, and the lead2
        # that would otherwise cover them is a fill value.
        hourly, needed, decision = self._day(lambda i: 70.0 + i * 0.1, lambda i: 32.0)
        for valid in needed[-2:]:
            hourly[valid]["lead1"] = None
        with pytest.raises(wb.MissingForecastError):
            wb.select_forecast_values("lead1_guarded", hourly, needed, decision)

    def test_a_varying_lead2_is_still_usable(self):
        hourly, needed, decision = self._day(lambda i: 70.0 + i * 0.1, lambda i: 60.0 + i * 0.1)
        values = wb.select_forecast_values("lead2", hourly, needed, decision)
        assert max(values) == pytest.approx(62.3)


class TestArchiveStartGuard:
    """The NBM archive's first month is not usable data."""

    def test_train_range_before_the_usable_archive_is_refused(self):
        with pytest.raises(ValueError, match="before the usable NBM archive"):
            wb.run_backtest(["KNYC"], "lead1_guarded", "2024-11-01", "2025-06-30",
                            "2025-07-01", "2025-12-31", 0.05)

    def test_eval_range_before_the_usable_archive_is_refused(self):
        with pytest.raises(ValueError, match="before the usable NBM archive"):
            wb.run_backtest(["KNYC"], "lead1_guarded", "2024-12-01", "2024-12-15",
                            "2024-11-05", "2024-11-20", 0.05)
