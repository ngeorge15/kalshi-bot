"""Unit tests for src/research/ensemble_model.py -- single vs. ensemble-mean vs. spread-sigma.

Fixture-driven, no network. All rows are hand-built to match the frozen
input schema (see ensemble_model.py's module docstring); `fetch_event_at_decision`
is monkeypatched at the `src.research.ensemble_model` module level (the name
that module imported into its own namespace) for the one end-to-end test
that exercises `run_backtest`.
"""
from __future__ import annotations

import json
import math
import statistics
from datetime import date, timedelta

import numpy as np
import pytest

from src.data.kalshi_history import PriceAtInstant
from src.paper import protocol as protocol_mod
from src.paper.weather import bracket_probability
from src.research import ensemble_model as em

DEFAULT_MODEL = em.DEFAULT_MODEL_ID  # "ncep_nbm_conus"


# ---------------------------------------------------------------------------
# Fixture builder
# ---------------------------------------------------------------------------

def make_row(
    station: str,
    date_lst: str,
    model_values: dict[str, float],
    observed_max_f: float | None,
    *,
    complete: bool = True,
    reason: str | None = None,
    degenerate: set[str] = frozenset(),
    missing: set[str] = frozenset(),
    spread_override: float | None = "unset",
    n_models_override: int | None = None,
) -> dict:
    """Build one frozen-schema row from a `{model_id: forecast_value}` mapping.

    `degenerate`/`missing` name model_ids whose lead1 value should be marked
    degenerate or omitted entirely (simulating a model absent that day).
    `spread_override` (a sentinel default, since `None` is itself a valid
    override) forces `ensemble_spread_lead1_f` to a specific value instead of
    the population stdev actually implied by `model_values`.
    """
    models = {}
    for model_id, value in model_values.items():
        if model_id in missing:
            continue
        models[model_id] = {
            "forecast_max_lead1_f": value,
            "forecast_max_lead2_f": value,
            "degenerate_lead1": model_id in degenerate,
            "degenerate_lead2": False,
        }
    values = list(model_values.values())
    n = n_models_override if n_models_override is not None else len(values)
    mean = statistics.mean(values) if values else None
    spread = statistics.pstdev(values) if len(values) >= 2 else None
    if spread_override != "unset":
        spread = spread_override
    return {
        "station": station,
        "date_lst": date_lst,
        "models": models,
        "ensemble_mean_lead1_f": mean,
        "ensemble_spread_lead1_f": spread,
        "ensemble_min_lead1_f": min(values) if values else None,
        "ensemble_max_lead1_f": max(values) if values else None,
        "n_models_lead1": n,
        "ensemble_mean_lead2_f": mean,
        "ensemble_spread_lead2_f": spread,
        "n_models_lead2": n,
        "observed_max_f": observed_max_f,
        "complete": complete,
        "reason": reason,
    }


# A clean 4-day, single-station dataset chosen so every fit is exactly
# hand-computable (see test docstrings below for the arithmetic).
BASIC_ROWS = [
    make_row("KNYC", "2025-01-01", {"ncep_nbm_conus": 68.0, "model_b": 70.0, "model_c": 72.0}, 71.0),
    make_row("KNYC", "2025-01-02", {"ncep_nbm_conus": 75.0, "model_b": 74.0, "model_c": 76.0}, 76.0),
    make_row("KNYC", "2025-01-03", {"ncep_nbm_conus": 60.0, "model_b": 63.0, "model_c": 66.0}, 64.0),
    make_row("KNYC", "2025-01-04", {"ncep_nbm_conus": 80.0, "model_b": 79.0, "model_c": 81.0}, 81.0),
]


# ---------------------------------------------------------------------------
# _usability_reason: the single shared gate for all three variants
# ---------------------------------------------------------------------------

class TestUsabilityReason:
    def test_fully_usable_row_returns_none(self):
        row = BASIC_ROWS[0]
        assert em._usability_reason(row, DEFAULT_MODEL, "lead1", 3) is None

    def test_incomplete_row_refused(self):
        row = make_row("KNYC", "2025-01-01", {"ncep_nbm_conus": 68.0, "b": 70.0, "c": 72.0}, 71.0, complete=False)
        assert em._usability_reason(row, DEFAULT_MODEL, "lead1", 3) == "incomplete"

    def test_missing_observed_refused(self):
        row = make_row("KNYC", "2025-01-01", {"ncep_nbm_conus": 68.0, "b": 70.0, "c": 72.0}, None)
        assert em._usability_reason(row, DEFAULT_MODEL, "lead1", 3) == "no_observed"

    def test_below_min_models_refused(self):
        row = make_row("KNYC", "2025-01-01", {"ncep_nbm_conus": 68.0, "b": 70.0}, 71.0)
        assert row["n_models_lead1"] == 2
        assert em._usability_reason(row, DEFAULT_MODEL, "lead1", 3) == "insufficient_models"

    def test_degenerate_single_model_refused(self):
        row = make_row(
            "KNYC", "2025-01-01", {"ncep_nbm_conus": 68.0, "b": 70.0, "c": 72.0}, 71.0,
            degenerate={"ncep_nbm_conus"},
        )
        assert em._usability_reason(row, DEFAULT_MODEL, "lead1", 3) == "single_model_missing"

    def test_missing_single_model_entirely_refused(self):
        row = make_row(
            "KNYC", "2025-01-01", {"ncep_nbm_conus": 68.0, "b": 70.0, "c": 72.0}, 71.0,
            missing={"ncep_nbm_conus"},
        )
        assert em._usability_reason(row, DEFAULT_MODEL, "lead1", 3) == "single_model_missing"

    def test_spread_none_refused_even_with_enough_models(self):
        # A data anomaly, not the normal n<2 case (n=3 here, well above the
        # threshold) -- the gate must still catch a None spread defensively.
        row = make_row(
            "KNYC", "2025-01-01", {"ncep_nbm_conus": 68.0, "b": 70.0, "c": 72.0}, 71.0,
            spread_override=None,
        )
        assert row["n_models_lead1"] == 3
        assert em._usability_reason(row, DEFAULT_MODEL, "lead1", 3) == "no_spread"


# ---------------------------------------------------------------------------
# fit_variants: hand-computable single / ensemble_mean / spread_sigma fits
# ---------------------------------------------------------------------------

class TestFitVariantsBasic:
    """BASIC_ROWS is built so every number below is exact, not approximate-by-luck:

    single residuals (observed - nbm): [71-68, 76-75, 64-60, 81-80] = [3, 1, 4, 1]
      -> mean 2.25, sample stdev 1.5 (a textbook pair of numbers).
    ensemble_mean residuals (observed - ensemble_mean): every day's 3-model
      mean is exactly `observed - 1.0` by construction, so every RAW residual
      is 1.0 -> bias 1.0, raw sample stdev 0.0. `SIGMA_FLOOR_F` (1.0) then
      clamps that 0.0 up to the floor -- itself a demonstration of the floor
      binding on a real (if unusually clean) fit, not just in the dedicated
      floor test below.
    Because the bias is exact, spread_sigma's abs-error target is 0.0 on
      every training day regardless of that day's spread -> a=0, b=0,
      b_se=0, spread_skill_correlation=0 (the "dead" case, exercised here on
      purpose so the near-zero-correlation reporting path has a real fixture).
    """

    def test_single_bias_and_sigma(self):
        fits = em.fit_variants(BASIC_ROWS, "2025-01-01", "2025-01-04")
        fit = fits.single["KNYC"]
        assert fit.n == 4
        assert fit.bias_f == pytest.approx(2.25)
        assert fit.sigma_f == pytest.approx(1.5)

    def test_ensemble_mean_bias_and_sigma_floor_binds_on_the_degenerate_fit(self):
        fits = em.fit_variants(BASIC_ROWS, "2025-01-01", "2025-01-04")
        fit = fits.ensemble_mean["KNYC"]
        assert fit.n == 4
        assert fit.bias_f == pytest.approx(1.0)
        # Raw sample stdev of [1, 1, 1, 1] is exactly 0.0; SIGMA_FLOOR_F clamps it.
        assert fit.sigma_f == pytest.approx(em.SIGMA_FLOOR_F)

    def test_spread_sigma_is_the_dead_case(self):
        fits = em.fit_variants(BASIC_ROWS, "2025-01-01", "2025-01-04")
        spread_fit = fits.spread_sigma
        assert spread_fit.n == 4
        assert spread_fit.a == pytest.approx(0.0, abs=1e-9)
        assert spread_fit.b == pytest.approx(0.0, abs=1e-9)
        assert spread_fit.b_se == pytest.approx(0.0, abs=1e-9)
        assert spread_fit.spread_skill_correlation == pytest.approx(0.0, abs=1e-9)

    def test_no_rows_skipped_on_the_clean_dataset(self):
        fits = em.fit_variants(BASIC_ROWS, "2025-01-01", "2025-01-04")
        assert fits.skip_reasons == {}

    def test_bias_by_station_matches_ensemble_mean_fit(self):
        fits = em.fit_variants(BASIC_ROWS, "2025-01-01", "2025-01-04")
        assert fits.spread_sigma.bias_by_station["KNYC"] == pytest.approx(fits.ensemble_mean["KNYC"].bias_f)


class TestFitVariantsRefusals:
    def test_insufficient_models_row_excluded_but_others_unaffected(self):
        bad = make_row("KNYC", "2025-01-05", {"ncep_nbm_conus": 70.0, "model_b": 72.0}, 71.0)
        fits = em.fit_variants(BASIC_ROWS + [bad], "2025-01-01", "2025-01-05")
        assert fits.skip_reasons["insufficient_models"] == 1
        assert fits.single["KNYC"].n == 4
        assert fits.ensemble_mean["KNYC"].n == 4

    def test_degenerate_single_model_row_excluded_but_others_unaffected(self):
        bad = make_row(
            "KNYC", "2025-01-05", {"ncep_nbm_conus": 70.0, "model_b": 72.0, "model_c": 74.0}, 71.0,
            degenerate={"ncep_nbm_conus"},
        )
        fits = em.fit_variants(BASIC_ROWS + [bad], "2025-01-01", "2025-01-05")
        assert fits.skip_reasons["single_model_missing"] == 1
        assert fits.single["KNYC"].n == 4
        assert fits.ensemble_mean["KNYC"].n == 4

    def test_train_start_after_end_raises(self):
        with pytest.raises(ValueError):
            em.fit_variants(BASIC_ROWS, "2025-01-05", "2025-01-01")

    def test_eval_only_one_side_given_raises(self):
        with pytest.raises(ValueError):
            em.fit_variants(BASIC_ROWS, "2025-01-01", "2025-01-04", eval_start="2025-02-01")

    def test_train_eval_overlap_refused(self):
        with pytest.raises(ValueError, match="overlaps"):
            em.fit_variants(
                BASIC_ROWS, train_start="2025-01-01", train_end="2025-01-10",
                eval_start="2025-01-05", eval_end="2025-01-20",
            )

    def test_non_overlapping_eval_is_accepted(self):
        fits = em.fit_variants(
            BASIC_ROWS, train_start="2025-01-01", train_end="2025-01-04",
            eval_start="2025-02-01", eval_end="2025-02-02",
        )
        assert fits.single["KNYC"].n == 4

    def test_unknown_lead_raises(self):
        with pytest.raises(ValueError):
            em.fit_variants(BASIC_ROWS, "2025-01-01", "2025-01-04", lead="lead3")

    def test_min_models_below_two_raises(self):
        with pytest.raises(ValueError):
            em.fit_variants(BASIC_ROWS, "2025-01-01", "2025-01-04", min_models=1)

    def test_non_positive_sigma_floor_raises(self):
        with pytest.raises(ValueError):
            em.fit_variants(BASIC_ROWS, "2025-01-01", "2025-01-04", sigma_floor_f=0.0)
        with pytest.raises(ValueError):
            em.fit_variants(BASIC_ROWS, "2025-01-01", "2025-01-04", sigma_floor_f=-1.0)

    def test_too_few_usable_rows_for_spread_regression_raises(self):
        two_rows = BASIC_ROWS[:2]
        with pytest.raises(ValueError, match="at least"):
            em.fit_variants(two_rows, "2025-01-01", "2025-01-02")

    def test_all_identical_spreads_raises(self):
        rows = [
            make_row("KNYC", f"2025-01-0{i}", {"ncep_nbm_conus": 70.0, "b": 71.0, "c": 69.0}, 70.0 + i)
            for i in range(1, 5)
        ]
        # Every row has the same 3 model values -> identical spread every day.
        with pytest.raises(ValueError, match="identical"):
            em.fit_variants(rows, "2025-01-01", "2025-01-04")


# ---------------------------------------------------------------------------
# predict: mu/sigma selection per variant, and the sigma floor
# ---------------------------------------------------------------------------

class TestPredict:
    def test_unknown_variant_raises(self):
        fits = em.fit_variants(BASIC_ROWS, "2025-01-01", "2025-01-04")
        with pytest.raises(ValueError):
            em.predict(fits, "bogus", BASIC_ROWS[0])

    def test_single_prediction_matches_fit(self):
        fits = em.fit_variants(BASIC_ROWS, "2025-01-01", "2025-01-04")
        row = BASIC_ROWS[0]  # nbm=68
        mu, sigma = em.predict(fits, "single", row)
        assert mu == pytest.approx(68.0 + 2.25)
        assert sigma == pytest.approx(1.5)

    def test_ensemble_mean_prediction_matches_fit(self):
        fits = em.fit_variants(BASIC_ROWS, "2025-01-01", "2025-01-04")
        row = BASIC_ROWS[0]  # ensemble mean=70
        mu, sigma = em.predict(fits, "ensemble_mean", row)
        assert mu == pytest.approx(70.0 + 1.0)
        assert sigma == pytest.approx(em.SIGMA_FLOOR_F)  # raw stdev 0.0, floor-clamped

    def test_prediction_none_when_station_has_no_fit(self):
        fits = em.fit_variants(BASIC_ROWS, "2025-01-01", "2025-01-04")
        row = make_row("KMDW", "2025-02-01", {"ncep_nbm_conus": 70.0, "b": 71.0, "c": 69.0}, 71.0)
        assert em.predict(fits, "single", row) is None
        assert em.predict(fits, "ensemble_mean", row) is None
        assert em.predict(fits, "spread_sigma", row) is None

    def test_prediction_none_when_spread_is_none(self):
        fits = em.fit_variants(BASIC_ROWS, "2025-01-01", "2025-01-04")
        row = make_row(
            "KNYC", "2025-02-01", {"ncep_nbm_conus": 70.0, "b": 71.0, "c": 69.0}, 71.0, spread_override=None,
        )
        assert em.predict(fits, "spread_sigma", row) is None
        # single/ensemble_mean do not need spread and should still predict.
        assert em.predict(fits, "single", row) is not None
        assert em.predict(fits, "ensemble_mean", row) is not None


class TestSigmaFloorBinding:
    def _fits_with(self, a: float, b: float) -> em.EnsembleFits:
        spread_fit = em.SpreadSigmaFit(
            a=a, b=b, b_se=0.0, spread_skill_correlation=0.0, sigma_floor_f=1.0, n=5,
            bias_by_station={"KNYC": 0.0},
        )
        return em.EnsembleFits(
            model_id=DEFAULT_MODEL, lead="lead1", min_models=3, sigma_floor_f=1.0,
            single={}, ensemble_mean={}, spread_sigma=spread_fit,
            train_start=date(2025, 1, 1), train_end=date(2025, 1, 4), skip_reasons={},
        )

    def test_floor_binds_when_formula_would_go_below_it(self):
        fits = self._fits_with(a=0.1, b=0.01)
        row = make_row("KNYC", "2025-02-01", {"ncep_nbm_conus": 70.0, "b": 70.5, "c": 69.5}, 70.0, spread_override=0.2)
        mu, sigma = em.predict(fits, "spread_sigma", row)
        # a + b*s = 0.1 + 0.01*0.2 = 0.102, far below the 1.0 floor.
        assert sigma == pytest.approx(1.0)

    def test_formula_used_when_above_the_floor(self):
        fits = self._fits_with(a=0.0, b=1.0)
        row = make_row("KNYC", "2025-02-01", {"ncep_nbm_conus": 70.0, "b": 70.5, "c": 69.5}, 70.0, spread_override=5.0)
        mu, sigma = em.predict(fits, "spread_sigma", row)
        assert sigma == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# Synthetic dataset where spread genuinely predicts error: the machinery
# must be able to detect the effect when it exists, or a null result on the
# real dataset is uninterpretable.
# ---------------------------------------------------------------------------

class TestSpreadGenuinelyPredictsError:
    """3 models per row, ensemble mean pinned at 70F. For each of 3 spread
    levels, one row overshoots by `0.5 * spread` and its twin undershoots by
    the same amount -- so the mean training error (the fitted per-station
    bias) is EXACTLY 0.0, and `abs_error` (observed - mu) equals `0.5 *
    spread` exactly, with no bias-subtraction distortion to fold the
    relationship into a non-monotonic shape. This is about as "alive" as a
    spread-skill relationship can be; if `spread_sigma` cannot beat a
    constant sigma here, the machinery itself is broken.
    """

    OFFSET_LEVELS = (1.0, 4.0, 16.0)
    ERROR_FRACTION_OF_SPREAD = 0.5

    @classmethod
    def _row(cls, day: int, offset: float, sign: int) -> dict:
        a, b, c = 70.0 - offset, 70.0, 70.0 + offset
        spread = statistics.pstdev([a, b, c])
        observed = 70.0 + sign * cls.ERROR_FRACTION_OF_SPREAD * spread
        return make_row("KNYC", f"2025-03-{day:02d}", {"ncep_nbm_conus": a, "model_b": b, "model_c": c}, observed)

    def _train_rows(self) -> list[dict]:
        rows = []
        day = 1
        for offset in self.OFFSET_LEVELS:
            for sign in (1, -1):
                rows.append(self._row(day, offset, sign))
                day += 1
        return rows

    def test_fit_recovers_a_strong_positive_spread_skill_relationship(self):
        fits = em.fit_variants(self._train_rows(), "2025-03-01", "2025-03-06")
        spread_fit = fits.spread_sigma
        assert fits.ensemble_mean["KNYC"].bias_f == pytest.approx(0.0, abs=1e-9)
        assert spread_fit.b == pytest.approx(self.ERROR_FRACTION_OF_SPREAD, rel=1e-6)
        assert spread_fit.b > 0
        assert spread_fit.spread_skill_correlation > 0.999

    def test_spread_sigma_beats_constant_sigma_out_of_sample(self):
        fits = em.fit_variants(self._train_rows(), "2025-03-01", "2025-03-06")

        # Held-out days, same construction, unseen offsets: one calm, one stormy.
        calm = self._row(10, 0.3, 1)
        stormy = self._row(11, 20.0, 1)

        edges = list(np.arange(50.0, 100.0 + 2.0, 2.0))
        brackets = [(None, edges[0])] + list(zip(edges, edges[1:])) + [(edges[-1], None)]

        def total_brier(variant: str, row: dict) -> float:
            mu, sigma = em.predict(fits, variant, row)
            observed = row["observed_max_f"]
            total = 0.0
            for lo, hi in brackets:
                p = bracket_probability(mu, sigma, lo, hi)
                in_bracket = (lo is None or observed >= lo) and (hi is None or observed < hi)
                total += (p - (1.0 if in_bracket else 0.0)) ** 2
            return total

        for row in (calm, stormy):
            spread_sigma_brier = total_brier("spread_sigma", row)
            constant_sigma_brier = total_brier("ensemble_mean", row)
            assert spread_sigma_brier < constant_sigma_brier, (
                f"spread_sigma ({spread_sigma_brier}) did not beat constant sigma "
                f"({constant_sigma_brier}) on {row['date_lst']}"
            )


# ---------------------------------------------------------------------------
# format_report: prints in the required order and states the dead case in words
# ---------------------------------------------------------------------------

def _dummy_comparison(n_events=2, improvement=0.01, ci_low=0.001, ci_high=0.02):
    return {
        "n_events": n_events, "n_markets": n_events, "model_brier": 0.1, "market_brier": 0.11,
        "brier_improvement": improvement, "per_event": [], "ci_low": ci_low, "ci_high": ci_high,
        "n_resamples": 100, "confidence": 0.95,
    }


def _dummy_report(correlation: float, spread_sigma_ci_low: float | None):
    from src.research.weather_backtest import calibration_bins
    calib_rows = [{"p": 0.3, "outcome": 0.0}, {"p": 0.7, "outcome": 1.0}]
    return {
        "fit_diagnostics": {
            "a": 0.0, "b": 0.6, "b_se": 0.05, "spread_skill_correlation": correlation,
            "n_train_pairs": 10, "sigma_floor_f": 1.0,
        },
        "comparisons": {
            name: _dummy_comparison(ci_low=(spread_sigma_ci_low if name == "spread_sigma_vs_market" else 0.001))
            for name in em.COMPARISONS
        },
        "calibration": {variant: calibration_bins(calib_rows) for variant in (*em.VARIANTS, "market")},
    }


class TestFormatReport:
    def test_sections_appear_in_order(self):
        report = _dummy_report(correlation=0.6, spread_sigma_ci_low=0.001)
        text = em.format_report(report)
        idx1 = text.index("1. Spread-skill diagnostic")
        idx2 = text.index("2. Paired Brier comparisons")
        idx3 = text.index("3. Calibration bins")
        idx4 = text.index("4. Verdict")
        assert idx1 < idx2 < idx3 < idx4

    def test_near_zero_correlation_states_the_dead_case_verbatim(self):
        report = _dummy_report(correlation=0.02, spread_sigma_ci_low=None)
        text = em.format_report(report)
        assert "a spread that does not predict error cannot improve a sigma" in text

    def test_verdict_when_spread_sigma_beats_market(self):
        report = _dummy_report(correlation=0.6, spread_sigma_ci_low=0.002)
        text = em.format_report(report)
        assert "spread_sigma beats the market" in text

    def test_verdict_does_not_overclaim_when_market_ci_includes_zero(self):
        report = _dummy_report(correlation=0.6, spread_sigma_ci_low=None)
        text = em.format_report(report)
        assert "No variant's paired Brier improvement over the market excludes zero" in text
        assert "only beating the market matters" in text


# ---------------------------------------------------------------------------
# run_backtest: one small end-to-end pass, market fetch monkeypatched
# ---------------------------------------------------------------------------

class TestRunBacktestIntegration:
    def _fake_event_rows(self, event_ticker, date_lst, observed, bounds):
        rows = []
        for i, (lo, hi) in enumerate(bounds):
            in_bracket = (lo is None or observed >= lo) and (hi is None or observed < hi)
            rows.append({
                "event_ticker": event_ticker, "ticker": f"{event_ticker}-{i}", "date_lst": date_lst,
                "lower_bound_f": lo, "upper_bound_f": hi,
                "price": PriceAtInstant(status="ok", yes_bid_cents=40, yes_ask_cents=60),
                "result": "yes" if in_bracket else "no",
            })
        return rows

    def test_end_to_end_report_structure(self, monkeypatch):
        bounds = [(None, 74.5), (74.5, 76.5), (76.5, 78.5), (78.5, None)]

        def fake_fetch(series, target_date, session=None):
            date_lst = target_date.isoformat() if hasattr(target_date, "isoformat") else target_date
            return self._fake_event_rows(f"EVT-{date_lst}", date_lst, observed=75.5, bounds=bounds)

        monkeypatch.setattr(em, "fetch_event_at_decision", fake_fetch)

        eval_row = make_row("KNYC", "2025-02-01", {"ncep_nbm_conus": 74.0, "model_b": 75.0, "model_c": 76.0}, 75.5)
        report = em.run_backtest(
            BASIC_ROWS + [eval_row], ["KNYC"],
            train_start="2025-01-01", train_end="2025-01-04",
            eval_start="2025-02-01", eval_end="2025-02-01",
        )
        assert report["eval_skip_reasons"] == {}
        for name in em.COMPARISONS:
            assert report["comparisons"][name]["n_events"] == 1
        for variant in (*em.VARIANTS, "market"):
            assert sum(b["n"] for b in report["calibration"][variant]) == len(bounds)
        text = em.format_report(report)
        assert "4. Verdict" in text

    def test_no_ensemble_row_for_eval_day_is_skipped(self, monkeypatch):
        monkeypatch.setattr(em, "fetch_event_at_decision", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not be called")))
        report = em.run_backtest(
            BASIC_ROWS, ["KNYC"],
            train_start="2025-01-01", train_end="2025-01-04",
            eval_start="2025-02-01", eval_end="2025-02-01",
        )
        assert report["eval_skip_reasons"]["no_ensemble_row"] == 1


# ---------------------------------------------------------------------------
# Guards: TRAIN_END and settlement-source switch, mirroring weather_backtest's
# ---------------------------------------------------------------------------

def _declare_valid_protocol(**overrides):
    kwargs = dict(
        hypothesis="ensemble_model: multi-model spread as a state-dependent sigma",
        primary_metric="event_clustered_paired_brier_improvement",
        secondary_metrics=(),
        min_events=10,
        decision_threshold=0.01,
        stopping_rule="stop after the fixed evaluation window",
        run_kind="replay",
        declared_by="test-suite",
        declared_at="2025-06-01T00:00:00Z",
    )
    kwargs.update(overrides)
    return protocol_mod.declare(**kwargs)


class TestEnforcePeriodGuards:
    def test_no_guard_needed_within_train_period(self):
        assert em._enforce_period_guards(em.TRAIN_END, None, False) is None

    def test_past_train_end_without_protocol_raises(self):
        with pytest.raises(ValueError, match="predeclared"):
            em._enforce_period_guards(em.TRAIN_END + timedelta(days=1), None, False)

    def test_past_switch_date_without_allow_flag_raises(self, tmp_path):
        protocol = _declare_valid_protocol()
        path = tmp_path / "protocol.json"
        protocol_mod.save(protocol, path)
        with pytest.raises(ValueError, match="settlement-source switch"):
            em._enforce_period_guards(em.SETTLEMENT_SOURCE_SWITCH_DATE + timedelta(days=1), path, False)

    def test_missing_hypothesis_marker_rejected(self, tmp_path):
        protocol = _declare_valid_protocol(hypothesis="unrelated experiment")
        path = tmp_path / "protocol.json"
        protocol_mod.save(protocol, path)
        with pytest.raises(ValueError, match="marker"):
            em._enforce_period_guards(em.TRAIN_END + timedelta(days=10), path, False)

    def test_valid_protocol_accepted(self, tmp_path):
        protocol = _declare_valid_protocol()
        path = tmp_path / "protocol.json"
        protocol_mod.save(protocol, path)
        eval_end = em.TRAIN_END + timedelta(days=10)
        assert eval_end <= em.SETTLEMENT_SOURCE_SWITCH_DATE
        result = em._enforce_period_guards(eval_end, path, False)
        assert result is not None
