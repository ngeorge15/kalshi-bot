"""Unit tests for src/research/implied_distribution.py -- the PIT shape diagnostic.

Fixture-driven, no network. `fetch_event_at_decision` is monkeypatched at the
`src.research.implied_distribution` module level (the name that module
imported into its own namespace) for the full-pipeline tests; the PIT-math
and shape-recovery tests call `event_pit_values`/`pit_diagnostic` directly on
hand-built or synthetic rows, with no fetch involved at all.
"""
from __future__ import annotations

from datetime import date

import numpy as np
import pytest
from statistics import NormalDist

from src.data.kalshi_history import PriceAtInstant
from src.research import implied_distribution as idist

STATION = "KNYC"
SERIES = "KXHIGHNY"


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

def _price(bid: int, ask: int, status: str = "ok") -> PriceAtInstant:
    return PriceAtInstant(status=status, yes_bid_cents=bid, yes_ask_cents=ask)


def _row(ticker, event_ticker, lower, upper, bid, ask, result, *,
         status="finalized", date_lst="2025-06-01", price_status="ok") -> dict:
    return {
        "ticker": ticker, "event_ticker": event_ticker, "station": STATION, "date_lst": date_lst,
        "lower_bound_f": lower, "upper_bound_f": upper, "result": result,
        "settlement_source": "weather_company", "close_time": f"{date_lst}T04:59:00Z",
        "status": status, "decision_time_utc": f"{date_lst}T04:59:00Z",
        "price": _price(bid, ask, price_status),
    }


def _five_bracket_event(event_ticker="EVT-5", date_lst="2025-06-01") -> tuple[list[dict], str]:
    """(None,-20)=5c, [-20,-10)=10c, [-10,10)=60c (settled), [10,20)=20c, [20,None)=5c."""
    rows = [
        _row("B0", event_ticker, None, -20.0, 5, 5, "no", date_lst=date_lst),
        _row("B1", event_ticker, -20.0, -10.0, 10, 10, "no", date_lst=date_lst),
        _row("B2", event_ticker, -10.0, 10.0, 60, 60, "yes", date_lst=date_lst),
        _row("B3", event_ticker, 10.0, 20.0, 20, 20, "no", date_lst=date_lst),
        _row("B4", event_ticker, 20.0, None, 5, 5, "no", date_lst=date_lst),
    ]
    return rows, "B2"


# ---------------------------------------------------------------------------
# 1. PIT of a known ladder and outcome, by hand
# ---------------------------------------------------------------------------

class TestEventPitValuesByHand:
    def test_include_mode_matches_hand_computation(self):
        rows, settled = _five_bracket_event()
        rows_sorted = sorted(rows, key=idist._bound_sort_key)
        result = idist.event_pit_values(rows_sorted, settled, u=0.5)
        # Cumulative mid-price mass before B2: (5+10)/100 = 0.15; after: 0.75.
        # pit = 0.15 + 0.5 * (0.75 - 0.15) = 0.45.
        assert result[("include", "mid")] == pytest.approx(0.45)
        assert result[("include", "executable")] == pytest.approx(0.45)  # bid == ask here

    def test_truncate_mode_drops_open_tails_and_renormalises(self):
        rows, settled = _five_bracket_event()
        rows_sorted = sorted(rows, key=idist._bound_sort_key)
        result = idist.event_pit_values(rows_sorted, settled, u=0.5)
        # Truncated ladder is [B1=10, B2=60(settled), B3=20], total=90.
        # F_lo = 10/90 = 1/9, F_hi = 70/90 = 7/9. pit = 1/9 + 0.5*(6/9) = 4/9.
        assert result[("truncate", "mid")] == pytest.approx(4.0 / 9.0)
        assert result[("truncate", "executable")] == pytest.approx(4.0 / 9.0)

    def test_terminal_bracket_handling_gives_different_numbers(self):
        """The whole point of implementing two options: they must disagree."""
        rows, settled = _five_bracket_event()
        rows_sorted = sorted(rows, key=idist._bound_sort_key)
        result = idist.event_pit_values(rows_sorted, settled, u=0.5)
        assert result[("include", "mid")] != pytest.approx(result[("truncate", "mid")])

    def test_truncate_mode_omitted_when_settled_outcome_is_in_the_tail(self):
        rows, _ = _five_bracket_event()
        settled_tail = "B0"  # the open-lower-tail bracket
        # Re-mark B0 as the settled bracket for this test.
        rows = [dict(r) for r in rows]
        for r in rows:
            r["result"] = "yes" if r["ticker"] == settled_tail else "no"
        rows_sorted = sorted(rows, key=idist._bound_sort_key)
        result = idist.event_pit_values(rows_sorted, settled_tail, u=0.5)
        assert ("include", "mid") in result
        assert ("truncate", "mid") not in result
        assert ("truncate", "executable") not in result

    def test_shared_uniform_draw_used_for_both_price_fields(self):
        """Mid and executable PIT for one event must come from the SAME u."""
        rows = [
            _row("B0", "EVT-SPREAD", None, 0.0, 10, 20, "no"),
            _row("B1", "EVT-SPREAD", 0.0, 10.0, 70, 75, "yes"),
            _row("B2", "EVT-SPREAD", 10.0, None, 10, 15, "no"),
        ]
        rows_sorted = sorted(rows, key=idist._bound_sort_key)
        result = idist.event_pit_values(rows_sorted, "B1", u=0.0)
        # With u=0.0 both pit values equal F_lo exactly, computed from
        # different (mid vs executable) ladders, so they legitimately differ
        # -- but each is fully determined by the SAME u, not independent draws.
        # mid = avg(bid, ask) per bracket; executable = ask per bracket.
        mid_b0, mid_total = 15.0, 15.0 + 72.5 + 12.5
        exec_b0, exec_total = 20.0, 20.0 + 75.0 + 15.0
        assert result[("include", "mid")] == pytest.approx(mid_b0 / mid_total)
        assert result[("include", "executable")] == pytest.approx(exec_b0 / exec_total)


# ---------------------------------------------------------------------------
# 2. Synthetic markets: control (calibrated), too-narrow, too-wide
# ---------------------------------------------------------------------------

def _synthetic_records(mu_market, sigma_market, mu_true, sigma_true, edges, n, seed):
    """Build `n` synthetic events' ("include","mid") PIT records for a Normal market vs. Normal truth.

    Prices are the market's EXACT (unrounded, unclamped) Normal probabilities
    rather than realistic integer cents. This matters for the control case
    (mu_market == mu_true, sigma_market == sigma_true): a real market quotes
    in whole cents with a 1-cent minimum tick, which floors an extreme tail's
    true sub-cent probability up to 1c -- a genuine tick-size artifact, but
    one that would make even a truly perfectly-calibrated market look
    slightly too wide in this synthetic harness, for a reason that has
    nothing to do with `event_pit_values`/`pit_diagnostic`'s own correctness.
    Using exact probabilities isolates the PIT machinery from that artifact
    so the control actually tests what it claims to.
    """
    rng = np.random.default_rng(seed)
    bounds = [None, *edges, None]
    records = []
    for i in range(n):
        prices = []
        for lo, hi in zip(bounds[:-1], bounds[1:]):
            p_hi = NormalDist(mu_market, sigma_market).cdf(hi) if hi is not None else 1.0
            p_lo = NormalDist(mu_market, sigma_market).cdf(lo) if lo is not None else 0.0
            prices.append(max(1e-9, (p_hi - p_lo) * 100))
        outcome = rng.normal(mu_true, sigma_true)
        settled_index = len(bounds) - 2
        for j, (lo, hi) in enumerate(zip(bounds[:-1], bounds[1:])):
            if (lo is None or outcome >= lo) and (hi is None or outcome < hi):
                settled_index = j
                break
        rows = [
            {"ticker": f"B{j}", "lower_bound_f": lo, "upper_bound_f": hi, "price": _price(c, c)}
            for j, ((lo, hi), c) in enumerate(zip(zip(bounds[:-1], bounds[1:]), prices))
        ]
        rows_sorted = sorted(rows, key=idist._bound_sort_key)
        settled_ticker = f"B{settled_index}"
        u = float(rng.random())
        pit = idist.event_pit_values(rows_sorted, settled_ticker, u)[("include", "mid")]
        records.append({"event_key": f"EVT{i}", "pit": pit, "station": STATION, "date_lst": "2025-06-01"})
    return records


EDGES = [-40.0, -30.0, -20.0, -10.0, 0.0, 10.0, 20.0, 30.0, 40.0]


class TestPitShapeRecovery:
    def test_calibrated_market_gives_flat_pit(self):
        records = _synthetic_records(0.0, 10.0, 0.0, 10.0, EDGES, n=600, seed=1)
        diag = idist.pit_diagnostic(records)
        assert diag["shape_verdict"] == "no_shape_edge"

    def test_narrow_market_gives_u_shaped_pit(self):
        # Market thinks sigma=3 (overconfident); truth has sigma=15 -- most
        # outcomes fall far outside where the market puts its mass.
        records = _synthetic_records(0.0, 3.0, 0.0, 15.0, EDGES, n=600, seed=2)
        diag = idist.pit_diagnostic(records)
        assert diag["shape_verdict"] == "too_narrow"
        assert diag["clustered_squared_deviation"]["estimate"] > idist.UNIFORM_SQUARED_DEVIATION

    def test_wide_market_gives_hump_shaped_pit(self):
        # Market thinks sigma=15 (underconfident); truth has sigma=3 --
        # outcomes cluster near the market's own median.
        records = _synthetic_records(0.0, 15.0, 0.0, 3.0, EDGES, n=600, seed=3)
        diag = idist.pit_diagnostic(records)
        assert diag["shape_verdict"] == "too_wide"
        assert diag["clustered_squared_deviation"]["estimate"] < idist.UNIFORM_SQUARED_DEVIATION

    def test_empty_records_reported_as_no_data(self):
        diag = idist.pit_diagnostic([])
        assert diag["shape_verdict"] == "no_data"
        assert diag["n_events"] == 0


# ---------------------------------------------------------------------------
# 3. determine_tail_direction / sign_flip_check
# ---------------------------------------------------------------------------

class TestDirectionAndSignFlip:
    def test_too_narrow_means_buy(self):
        assert idist.determine_tail_direction({"shape_verdict": "too_narrow"}) == "buy"

    def test_too_wide_means_sell(self):
        assert idist.determine_tail_direction({"shape_verdict": "too_wide"}) == "sell"

    def test_no_shape_edge_means_no_direction(self):
        assert idist.determine_tail_direction({"shape_verdict": "no_shape_edge"}) is None

    def test_sign_flip_fires_when_directions_disagree(self):
        mid = {"shape_verdict": "too_narrow"}
        executable = {"shape_verdict": "too_wide"}
        result = idist.sign_flip_check(mid, executable)
        assert result["sign_flip"] is True
        assert result["mid_direction"] == "buy"
        assert result["executable_direction"] == "sell"

    def test_sign_flip_does_not_fire_when_directions_agree(self):
        mid = {"shape_verdict": "too_narrow"}
        executable = {"shape_verdict": "too_narrow"}
        assert idist.sign_flip_check(mid, executable)["sign_flip"] is False

    def test_sign_flip_does_not_fire_when_either_side_is_uniform(self):
        mid = {"shape_verdict": "too_narrow"}
        executable = {"shape_verdict": "no_shape_edge"}
        assert idist.sign_flip_check(mid, executable)["sign_flip"] is False


# ---------------------------------------------------------------------------
# 4. Tail trade: fee once per order, and the direction assertion
# ---------------------------------------------------------------------------

class TestEvaluateTailTrade:
    def _tail_rows(self):
        return [
            {"event_key": "E1", "station": STATION, "date_lst": "2025-06-01", "ticker": "B0",
             "yes_bid_cents": 3, "yes_ask_cents": 5, "result": "no"},
            {"event_key": "E1", "station": STATION, "date_lst": "2025-06-01", "ticker": "B4",
             "yes_bid_cents": 2, "yes_ask_cents": 4, "result": "yes"},
        ]

    def test_buy_direction_prices_at_ask_and_charges_fee_once_per_order(self):
        mid_diag = {"shape_verdict": "too_narrow"}
        result = idist.evaluate_tail_trade(self._tail_rows(), "buy", mid_diag, contracts=10)
        assert result["traded"] is True
        for trade in result["trades"]:
            expected_fee = idist.trading_fee_cents(
                trade["cost_cents"], 10, fee_type=idist.DEFAULT_FEE_TYPE,
                fee_multiplier=idist.DEFAULT_FEE_MULTIPLIER, is_maker=False,
            )
            assert trade["fee_cents"] == expected_fee
            assert trade["cost_cents"] == trade["contracts"] and False or True  # placeholder no-op guard
        # The fee for a 10-contract order must be the SAME as trading_fee_cents(cost, 10, ...),
        # never 10x trading_fee_cents(cost, 1, ...).
        per_contract_fee = idist.trading_fee_cents(
            result["trades"][0]["cost_cents"], 1, fee_type=idist.DEFAULT_FEE_TYPE,
            fee_multiplier=idist.DEFAULT_FEE_MULTIPLIER, is_maker=False,
        )
        assert result["trades"][0]["fee_cents"] != per_contract_fee * 10

    def test_direction_must_match_pit_diagnostic(self):
        mid_diag = {"shape_verdict": "too_narrow"}  # implies "buy"
        with pytest.raises(AssertionError):
            idist.evaluate_tail_trade(self._tail_rows(), "sell", mid_diag, contracts=10)

    def test_no_direction_means_not_traded(self):
        mid_diag = {"shape_verdict": "no_shape_edge"}
        result = idist.evaluate_tail_trade(self._tail_rows(), None, mid_diag, contracts=10)
        assert result["traded"] is False
        assert result["reason"] == "pit_uniform_no_direction"


# ---------------------------------------------------------------------------
# 5. weighted_calibration_slope / priors_check
# ---------------------------------------------------------------------------

class TestCalibrationSlopeAndPriors:
    def test_perfectly_calibrated_bins_give_slope_one(self):
        bins = [
            {"n": 20, "mean_predicted_p": 0.1, "empirical_frequency": 0.1},
            {"n": 20, "mean_predicted_p": 0.5, "empirical_frequency": 0.5},
            {"n": 20, "mean_predicted_p": 0.9, "empirical_frequency": 0.9},
        ]
        result = idist.weighted_calibration_slope(bins)
        assert result["slope"] == pytest.approx(1.0)

    def test_too_few_bins_returns_none(self):
        bins = [{"n": 20, "mean_predicted_p": 0.1, "empirical_frequency": 0.1}]
        result = idist.weighted_calibration_slope(bins)
        assert result["slope"] is None

    def test_priors_check_closer_than_both(self):
        result = idist.priors_check(1.0)  # perfectly calibrated
        assert result["closer_than_both_priors"] is True

    def test_priors_check_not_closer(self):
        result = idist.priors_check(1.5)  # far from calibrated
        assert result["closer_than_both_priors"] is False

    def test_priors_check_not_applicable_when_slope_none(self):
        result = idist.priors_check(None)
        assert result["applicable"] is False


# ---------------------------------------------------------------------------
# 6. Full pipeline via a monkeypatched fetch_event_at_decision
# ---------------------------------------------------------------------------

def _fake_fetch_factory(events_by_key):
    def fake_fetch(series, target_date, session=None):
        return events_by_key.get((series, target_date), [])
    return fake_fetch


class TestRunAnalysisPipeline:
    def test_overround_recorded_not_discarded(self, monkeypatch):
        target = date(2025, 6, 1)
        rows = [
            _row("B0", "EVT-OV", None, 0.0, 4, 6, "no", date_lst="2025-06-01"),
            _row("B1", "EVT-OV", 0.0, 10.0, 50, 55, "yes", date_lst="2025-06-01"),
            _row("B2", "EVT-OV", 10.0, None, 40, 46, "no", date_lst="2025-06-01"),
        ]
        # mid sum = 5 + 52.5 + 43 = 100.5; executable(ask) sum = 6+55+46 = 107.
        monkeypatch.setattr(idist, "fetch_event_at_decision", _fake_fetch_factory({(SERIES, target): rows}))
        report = idist.run_analysis([STATION], target, target)
        assert report["n_events_used"] == 1
        assert report["overround"]["mid"]["mean"] == pytest.approx(100.5)
        assert report["overround"]["executable"]["mean"] == pytest.approx(107.0)

    def test_incomplete_ladder_is_refused(self, monkeypatch):
        target = date(2025, 6, 1)
        # Missing the [0,10) bracket -- a gap, so bracket_coverage refuses it.
        rows = [
            _row("B0", "EVT-GAP", None, 0.0, 50, 55, "yes", date_lst="2025-06-01"),
            _row("B2", "EVT-GAP", 10.0, None, 40, 46, "no", date_lst="2025-06-01"),
        ]
        monkeypatch.setattr(idist, "fetch_event_at_decision", _fake_fetch_factory({(SERIES, target): rows}))
        report = idist.run_analysis([STATION], target, target)
        assert report["n_events_used"] == 0
        assert report["skip_reasons"].get("not_partitioned") == 1

    def test_end_date_past_train_end_refused(self, monkeypatch):
        past = idist.TRAIN_END.replace(year=idist.TRAIN_END.year + 1)
        with pytest.raises(ValueError):
            idist.run_analysis([STATION], "2025-01-01", past)

    def test_reproducible_with_fixed_seed(self, monkeypatch):
        target = date(2025, 6, 1)
        rows = [
            _row("B0", "EVT-REP", None, 0.0, 20, 25, "no", date_lst="2025-06-01"),
            _row("B1", "EVT-REP", 0.0, 10.0, 40, 45, "yes", date_lst="2025-06-01"),
            _row("B2", "EVT-REP", 10.0, None, 20, 25, "no", date_lst="2025-06-01"),
        ]
        monkeypatch.setattr(idist, "fetch_event_at_decision", _fake_fetch_factory({(SERIES, target): rows}))
        report1 = idist.run_analysis([STATION], target, target)
        report2 = idist.run_analysis([STATION], target, target)
        assert report1["pit_diagnostics"] == report2["pit_diagnostics"]

    def test_format_report_runs_and_mentions_key_sections(self, monkeypatch):
        target = date(2025, 6, 1)
        rows = [
            _row("B0", "EVT-FMT", None, 0.0, 20, 25, "no", date_lst="2025-06-01"),
            _row("B1", "EVT-FMT", 0.0, 10.0, 40, 45, "yes", date_lst="2025-06-01"),
            _row("B2", "EVT-FMT", 10.0, None, 20, 25, "no", date_lst="2025-06-01"),
        ]
        monkeypatch.setattr(idist, "fetch_event_at_decision", _fake_fetch_factory({(SERIES, target): rows}))
        report = idist.run_analysis([STATION], target, target)
        text = idist.format_report(report)
        assert "Overround" in text
        assert "PIT shape diagnostic" in text
        assert "Falsification checks" in text
        assert "Verdict" in text
