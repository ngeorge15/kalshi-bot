"""Does multi-model spread beat a single fitted sigma? See `research/multi-model-forecasting.md`.

`src/research/weather_backtest.py` scored Brier 0.1222 against the market's
0.1036 (`research/weather-backtest-validation.md`) with a model that is:

    one deterministic NBM forecast -> fitted bias -> Normal(bias-corrected
    forecast, one CONSTANT fitted sigma) -> integrate over each bracket.

The constant sigma is the specific defect this module attacks: forecast
uncertainty is state-dependent (a calm day and a six-model-disagreement day
do not carry the same uncertainty), and a constant sigma cannot express that.
This module implements three nested variants, fit and compared on the SAME
row set so the comparison is apples-to-apples:

1. **`single`** -- the control. Reproduces the existing approach for one
   named model (default `ncep_nbm_conus`): constant bias, constant sigma.
2. **`ensemble_mean`** -- same constant sigma, but the point forecast is the
   multi-model ensemble mean. Isolates the benefit of combining models.
3. **`spread_sigma`** -- the real proposal. Point forecast is the ensemble
   mean (as in #2); sigma is fit as a function of ensemble spread:
   `sigma(s) = max(sigma_floor, a + b * s)` (the standard spread-skill / EMOS
   form), `a`/`b` fit by OLS on training residuals.

Separating #2 from #3 is the point of this module: if the gain turns out to
come from the mean rather than the sigma, the story that follows is
different, and only measuring both separately tells us which.

**Input schema (frozen, produced by a separate multi-model ingestion module
-- not built here and not imported here; this module only consumes rows
matching this shape, passed in by the caller):**

    {
      "station": str, "date_lst": "YYYY-MM-DD",
      "models": {model_id: {"forecast_max_lead1_f": float | None,
                             "forecast_max_lead2_f": float | None,
                             "degenerate_lead1": bool, "degenerate_lead2": bool}},
      "ensemble_mean_lead1_f": float | None, "ensemble_spread_lead1_f": float | None,
      "ensemble_min_lead1_f": float | None, "ensemble_max_lead1_f": float | None,
      "n_models_lead1": int,
      "ensemble_mean_lead2_f": float | None, "ensemble_spread_lead2_f": float | None,
      "n_models_lead2": int,
      "observed_max_f": float | None, "complete": bool, "reason": str | None,
    }

**Fairness design choice (not specified by the frozen schema, decided here):**
a single row-usability gate (`_usability_reason`) is applied identically to
ALL THREE variants, both at fit time and at evaluation time. A day where the
named `single` model is degenerate is excluded from `ensemble_mean` and
`spread_sigma` too, even though they do not need that model's value. This
costs a handful of training/eval days but buys something more important:
every paired comparison below (any variant vs. any other variant, and vs.
the market) runs over the exact same set of city-days. Without this, a
variant that "wins" might just have gotten an easier day set, and the
result would say nothing about which mechanism is doing the work.

**Non-negotiables, exactly as `weather_backtest.fit_bias_sigma` already
enforces bias/sigma:**

* Every fit below uses TRAIN-only rows and refuses any train/test date
  overlap (`fit_variants`, mirroring `fit_bias_sigma`'s overlap check).
* `sigma_floor_f` (see `SIGMA_FLOOR_F`) is always > 0 and is applied to
  ALL THREE variants' sigma, not just `spread_sigma`'s formula: a day (or a
  whole small training slice) of freak model agreement cannot collapse any
  variant to a near-zero-variance Normal, which Brier score punishes
  brutally the moment it is wrong, and which `NormalDist` itself refuses to
  construct at exactly zero.
* `min_models` (default `MIN_MODELS_DEFAULT`) refuses any row whose ensemble
  had fewer models than that, by name (`"insufficient_models"` in
  `skip_reasons`), never silently.
* Bracket integration reuses `src.paper.weather.bracket_probability` --
  the same helper `weather_backtest.py` uses -- rather than a second
  CDF-difference implementation.
* Every comparison below is paired (same event/city-day for every variant)
  and its confidence interval resamples whole events, not individual
  brackets, via `weather_backtest.event_clustered_bootstrap_ci` applied to
  each pair's per-event Brier improvement (from
  `src.paper.uncertainty.paired_brier_by_event`) -- this repo has already
  shipped the bug of treating correlated brackets as independent once
  (`src/paper/uncertainty.py`'s own docstring); it is not repeated here.

Usage::

    from src.research.ensemble_model import fit_variants, run_backtest

    fits = fit_variants(rows, train_start="2025-03-01", train_end="2025-12-31")
    report = run_backtest(rows, station_codes=["KNYC", "KMDW", "KMIA", "KAUS"],
                           train_start="2025-03-01", train_end="2025-12-31",
                           eval_start="2026-01-01", eval_end="2026-03-31")
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import statistics
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterator

import numpy as np

from src.data.kalshi_history import SERIES_STATIONS, fetch_event_at_decision
from src.paper import protocol as protocol_mod
from src.paper.uncertainty import (
    DEFAULT_CONFIDENCE,
    DEFAULT_N_RESAMPLES,
    DEFAULT_SEED,
    paired_brier_by_event,
)
from src.paper.weather import bracket_probability
from src.research.weather_backtest import (
    SETTLEMENT_SOURCE_SWITCH_DATE,
    TRAIN_END,
    calibration_bins,
    event_clustered_bootstrap_ci,
)

logger = logging.getLogger(__name__)

STATION_TO_SERIES: dict[str, str] = {station: series for series, station in SERIES_STATIONS.items()}

LEADS: tuple[str, ...] = ("lead1", "lead2")

# lead1_guarded outscored lead2 in weather_backtest's validation run (0.1222
# vs 0.1307 Brier, research/weather-backtest-validation.md); default to the
# lead the frozen schema's "lead1" fields represent for the same reason.
DEFAULT_LEAD = "lead1"

DEFAULT_MODEL_ID = "ncep_nbm_conus"

VARIANTS: tuple[str, ...] = ("single", "ensemble_mean", "spread_sigma")

# Spread computed from 2 models is barely a spread at all -- it is one
# difference, with no way to tell a real disagreement from one model's
# idiosyncratic noise. Default of 3 requires at least a majority-of-a-pair
# disagreement before spread is trusted as a signal.
MIN_MODELS_DEFAULT = 3

# The oracle control in research/weather-backtest-validation.md -- using the
# OBSERVED daily high as the "forecast" -- still needed sigma=0.8F to score
# well; that is the measurement-noise floor of the CLI daily-high figure
# itself, not a fitted forecast uncertainty. No fitted sigma here is allowed
# to imply less spread than the observation it is being compared against
# already carries. 1.0F sets a small safety margin above that empirical
# floor. A sigma at or below this on a real market bracket is a de facto
# 0-or-1 probability, which Brier score punishes brutally the moment it is
# wrong -- exactly the overconfidence failure mode this floor exists to rule
# out by construction, not by hoping the fit never gets there.
SIGMA_FLOOR_F = 1.0

# OLS with 2 free parameters (a, b) needs residual degrees of freedom
# (n - 2 > 0) to produce ANY standard error for b, let alone a useful one.
# 3 is the bare minimum to compute a number, not a recommendation to trust
# a 3-point fit.
MIN_SPREAD_FIT_N = 3

# Below this |correlation|, the spread-skill relationship is not
# distinguishable from noise for this report's own purposes. Not a formal
# significance test (that would need the n and the actual sampling
# distribution) -- just the bar this module's own language holds itself to
# before calling the spread-sigma idea alive.
SPREAD_SKILL_DEAD_THRESHOLD = 0.10

DEFAULT_CONTRACTS_NOTE = None  # this module scores probabilities only; it places no trades.

# This backtest replays historical Kalshi market data, exactly as
# weather_backtest.py's does; only a "replay" protocol governs it.
REQUIRED_PROTOCOL_RUN_KIND = "replay"

# Distinct from weather_backtest.py's own marker so a protocol written for
# one backtest can never be mistaken for verifying the other.
PROTOCOL_HYPOTHESIS_MARKER = "ensemble_model"

# (a, b): brier of variant `a` is compared against brier of variant `b`;
# `brier_improvement = b - a`, so positive means `a` beats `b`. Ordered to
# read top-to-bottom as the module docstring's narrative: each variant vs.
# the market first (the only comparison that matters), then the two
# step-by-step comparisons that separate "better mean" from "better sigma".
COMPARISONS: dict[str, tuple[str, str]] = {
    "single_vs_market": ("single", "market"),
    "ensemble_mean_vs_market": ("ensemble_mean", "market"),
    "spread_sigma_vs_market": ("spread_sigma", "market"),
    "ensemble_mean_vs_single": ("ensemble_mean", "single"),
    "spread_sigma_vs_ensemble_mean": ("spread_sigma", "ensemble_mean"),
    "spread_sigma_vs_single": ("spread_sigma", "single"),
}


# --- Small local date helpers (duplicated across this repo's research
# modules by convention -- see src/data/weather/archive.py's own comment) --

def _as_date(value: str | date) -> date:
    return value if isinstance(value, date) else date.fromisoformat(value)


def _date_range(start: date, end: date) -> Iterator[date]:
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def _finite(value: object) -> bool:
    return (
        value is not None
        and not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
    )


# --- 1. Row accessors against the frozen schema -----------------------------

def _model_forecast(row: dict, model_id: str, lead: str) -> float | None:
    """The named model's `lead` forecast, or `None` if absent/degenerate/non-finite."""
    models = row.get("models") or {}
    info = models.get(model_id)
    if not info or info.get(f"degenerate_{lead}"):
        return None
    value = info.get(f"forecast_max_{lead}_f")
    return value if _finite(value) else None


def _ensemble_mean(row: dict, lead: str) -> float | None:
    value = row.get(f"ensemble_mean_{lead}_f")
    return value if _finite(value) else None


def _ensemble_spread(row: dict, lead: str) -> float | None:
    value = row.get(f"ensemble_spread_{lead}_f")
    return value if _finite(value) else None


def _n_models(row: dict, lead: str) -> int:
    n = row.get(f"n_models_{lead}")
    return n if isinstance(n, int) and not isinstance(n, bool) else 0


def _usability_reason(row: dict, model_id: str, lead: str, min_models: int) -> str | None:
    """Why `row` cannot be used by ANY of the three variants, or `None` if it can.

    See the module docstring's "Fairness design choice": this one gate is
    shared by all three variants (and by both fitting and evaluation) so
    every comparison below runs over an identical row set.
    """
    if not row.get("complete", False):
        return "incomplete"
    if not _finite(row.get("observed_max_f")):
        return "no_observed"
    if _n_models(row, lead) < min_models:
        return "insufficient_models"
    if _model_forecast(row, model_id, lead) is None:
        return "single_model_missing"
    if _ensemble_mean(row, lead) is None:
        return "no_ensemble_mean"
    if _ensemble_spread(row, lead) is None:
        return "no_spread"
    return None


# --- 2. Fits, train-only ------------------------------------------------------

@dataclass(frozen=True)
class ConstantSigmaFit:
    bias_f: float
    sigma_f: float
    n: int


@dataclass(frozen=True)
class SpreadSigmaFit:
    a: float
    b: float
    b_se: float
    spread_skill_correlation: float
    sigma_floor_f: float
    n: int
    bias_by_station: dict[str, float]


@dataclass(frozen=True)
class EnsembleFits:
    model_id: str
    lead: str
    min_models: int
    sigma_floor_f: float
    single: dict[str, ConstantSigmaFit]
    ensemble_mean: dict[str, ConstantSigmaFit]
    spread_sigma: SpreadSigmaFit
    train_start: date
    train_end: date
    skip_reasons: dict[str, int]


def _fit_constant_sigma_by_station(
    usable_rows: list[dict], forecast_fn: Any, sigma_floor_f: float
) -> dict[str, ConstantSigmaFit]:
    """Per-station bias/sigma of `(observed - forecast_fn(row))`, mirroring `fit_bias_sigma`.

    `SIGMA_FLOOR_F`'s justification (see its own comment) is not specific to
    the spread-sigma formula: a sample standard deviation can legitimately
    come out at or near zero on a small or unusually well-agreed training
    slice, and a zero-variance Normal is a bug (`NormalDist` raises on
    `sigma == 0`) that would otherwise crash the first prediction that uses
    it, not a graceful "very confident" forecast. The floor is applied here
    too, to `single` and `ensemble_mean` alike, for that reason.
    """
    errors_by_station: dict[str, list[float]] = defaultdict(list)
    for row in usable_rows:
        forecast = forecast_fn(row)
        if forecast is None:
            continue
        errors_by_station[row["station"]].append(row["observed_max_f"] - forecast)
    fits: dict[str, ConstantSigmaFit] = {}
    for station, errors in errors_by_station.items():
        if len(errors) < 2:
            logger.warning("Station %s has only %d training day(s); skipping fit", station, len(errors))
            continue
        fits[station] = ConstantSigmaFit(
            bias_f=statistics.mean(errors), sigma_f=max(sigma_floor_f, statistics.stdev(errors)), n=len(errors)
        )
    return fits


def _fit_spread_sigma(
    usable_rows: list[dict], lead: str, ensemble_mean_fits: dict[str, ConstantSigmaFit], sigma_floor_f: float
) -> SpreadSigmaFit:
    """OLS-fit `abs_error = a + b * spread` on bias-corrected ensemble-mean residuals.

    The bias correction reused here is `ensemble_mean_fits`'s own per-station
    bias -- `spread_sigma`'s point forecast is identical to `ensemble_mean`'s
    (see module docstring); only the sigma differs.
    """
    bias_by_station = {station: fit.bias_f for station, fit in ensemble_mean_fits.items()}
    spreads: list[float] = []
    abs_errors: list[float] = []
    for row in usable_rows:
        bias = bias_by_station.get(row["station"])
        mean = _ensemble_mean(row, lead)
        spread = _ensemble_spread(row, lead)
        if bias is None or mean is None or spread is None:
            continue
        abs_errors.append(abs(row["observed_max_f"] - (mean + bias)))
        spreads.append(spread)

    n = len(spreads)
    if n < MIN_SPREAD_FIT_N:
        raise ValueError(
            f"Only {n} usable (spread, error) pair(s) for the spread-sigma regression; need at "
            f"least {MIN_SPREAD_FIT_N} to fit a and b with an estimable standard error"
        )
    x = np.asarray(spreads, dtype=float)
    y = np.asarray(abs_errors, dtype=float)
    xbar, ybar = float(x.mean()), float(y.mean())
    sxx = float(np.sum((x - xbar) ** 2))
    if sxx == 0.0:
        raise ValueError(
            f"All {n} training spread values are identical ({x[0]}F); cannot fit a spread-sigma "
            "slope from zero variation in the predictor"
        )
    syy = float(np.sum((y - ybar) ** 2))
    sxy = float(np.sum((x - xbar) * (y - ybar)))
    b = sxy / sxx
    a = ybar - b * xbar
    residuals = y - (a + b * x)
    sse = float(np.sum(residuals ** 2))
    residual_variance = sse / (n - 2) if n > 2 else 0.0
    b_se = math.sqrt(residual_variance / sxx) if residual_variance > 0 else 0.0
    correlation = 0.0 if syy == 0.0 else sxy / math.sqrt(sxx * syy)
    return SpreadSigmaFit(
        a=a, b=b, b_se=b_se, spread_skill_correlation=correlation,
        sigma_floor_f=sigma_floor_f, n=n, bias_by_station=bias_by_station,
    )


def fit_variants(
    rows: list[dict],
    train_start: str | date,
    train_end: str | date,
    eval_start: str | date | None = None,
    eval_end: str | date | None = None,
    model_id: str = DEFAULT_MODEL_ID,
    lead: str = DEFAULT_LEAD,
    min_models: int = MIN_MODELS_DEFAULT,
    sigma_floor_f: float = SIGMA_FLOOR_F,
) -> EnsembleFits:
    """Fit `single`, `ensemble_mean`, and `spread_sigma` on `[train_start, train_end]` only.

    Args:
        rows: Rows matching the frozen schema (module docstring); any dates
            outside `[train_start, train_end]` are ignored here (evaluation
            reads `rows` again itself).
        train_start, train_end: TRAIN date range (inclusive).
        eval_start, eval_end: If given (both required together), checked for
            zero overlap with the train range -- mirrors
            `weather_backtest.fit_bias_sigma`'s guard exactly.
        model_id: The single named model for the `single` variant.
        lead: One of `LEADS`.
        min_models: Minimum `n_models_lead*` for a row to be usable at all.
        sigma_floor_f: Floor for `spread_sigma`'s fitted sigma; must be > 0.

    Returns:
        An `EnsembleFits` bundling all three variants' fits plus the
        training-row `skip_reasons` tally.

    Raises:
        ValueError: For an invalid lead/min_models/sigma_floor_f, an
            inverted date range, a train/eval overlap, or too few usable
            training rows to fit the spread-sigma regression at all.
    """
    if lead not in LEADS:
        raise ValueError(f"Unknown lead {lead!r}; expected one of {LEADS}")
    if min_models < 2:
        raise ValueError(f"min_models must be >= 2 (a spread from 1 model is not a spread); got {min_models}")
    if sigma_floor_f <= 0:
        raise ValueError(f"sigma_floor_f must be > 0 (a zero-variance Normal is a bug, not a forecast); got {sigma_floor_f}")

    train_start_d, train_end_d = _as_date(train_start), _as_date(train_end)
    if train_start_d > train_end_d:
        raise ValueError(f"train_start {train_start_d} must be on or before train_end {train_end_d}")
    if (eval_start is None) != (eval_end is None):
        raise ValueError("eval_start and eval_end must be given together, or not at all")
    if eval_start is not None:
        eval_start_d, eval_end_d = _as_date(eval_start), _as_date(eval_end)
        if eval_start_d > eval_end_d:
            raise ValueError(f"eval_start {eval_start_d} must be on or before eval_end {eval_end_d}")
        if not (train_end_d < eval_start_d or eval_end_d < train_start_d):
            raise ValueError(
                f"Train range [{train_start_d}, {train_end_d}] overlaps the evaluation range "
                f"[{eval_start_d}, {eval_end_d}]; refusing to fit on data that includes eval days"
            )

    skip_reasons: Counter = Counter()
    usable_rows: list[dict] = []
    for row in rows:
        row_date = _as_date(row["date_lst"])
        if not (train_start_d <= row_date <= train_end_d):
            continue
        reason = _usability_reason(row, model_id, lead, min_models)
        if reason is not None:
            skip_reasons[reason] += 1
            continue
        usable_rows.append(row)

    single_fits = _fit_constant_sigma_by_station(usable_rows, lambda r: _model_forecast(r, model_id, lead), sigma_floor_f)
    ensemble_mean_fits = _fit_constant_sigma_by_station(usable_rows, lambda r: _ensemble_mean(r, lead), sigma_floor_f)
    spread_fit = _fit_spread_sigma(usable_rows, lead, ensemble_mean_fits, sigma_floor_f)

    return EnsembleFits(
        model_id=model_id, lead=lead, min_models=min_models, sigma_floor_f=sigma_floor_f,
        single=single_fits, ensemble_mean=ensemble_mean_fits, spread_sigma=spread_fit,
        train_start=train_start_d, train_end=train_end_d, skip_reasons=dict(skip_reasons),
    )


# --- 3. Prediction, reusing bracket_probability for integration --------------

def predict(fits: EnsembleFits, variant: str, row: dict) -> tuple[float, float] | None:
    """`(mu, sigma)` for `row` under `variant`, or `None` if `row` lacks what `variant` needs.

    `sigma` for `spread_sigma` is always `>= fits.spread_sigma.sigma_floor_f`
    (`SIGMA_FLOOR_F`'s non-negotiable floor, enforced here at prediction
    time, not just documented).
    """
    if variant not in VARIANTS:
        raise ValueError(f"Unknown variant {variant!r}; expected one of {VARIANTS}")
    station = row.get("station")
    lead = fits.lead
    if variant == "single":
        forecast = _model_forecast(row, fits.model_id, lead)
        fit = fits.single.get(station)
        if forecast is None or fit is None:
            return None
        return forecast + fit.bias_f, fit.sigma_f
    if variant == "ensemble_mean":
        mean = _ensemble_mean(row, lead)
        fit = fits.ensemble_mean.get(station)
        if mean is None or fit is None:
            return None
        return mean + fit.bias_f, fit.sigma_f
    # spread_sigma
    mean = _ensemble_mean(row, lead)
    spread = _ensemble_spread(row, lead)
    bias = fits.spread_sigma.bias_by_station.get(station)
    if mean is None or spread is None or bias is None:
        return None
    sigma = max(fits.spread_sigma.sigma_floor_f, fits.spread_sigma.a + fits.spread_sigma.b * spread)
    return mean + bias, sigma


# --- 4. Guardrail: training window only ---------------------------------------

def _enforce_period_guards(
    eval_end: date, protocol_path: str | Path | None, allow_weather_company_settlement: bool
) -> protocol_mod.Protocol | None:
    """Refuse an evaluation window this backtest is not allowed to run.

    Mirrors `weather_backtest._enforce_period_guards` exactly (same
    `TRAIN_END`/`SETTLEMENT_SOURCE_SWITCH_DATE`, imported rather than
    redefined), but requires its own `PROTOCOL_HYPOTHESIS_MARKER` so a
    protocol written for one backtest can never verify the other.
    """
    if eval_end > SETTLEMENT_SOURCE_SWITCH_DATE and not allow_weather_company_settlement:
        raise ValueError(
            f"eval_end {eval_end} is after the settlement-source switch "
            f"({SETTLEMENT_SOURCE_SWITCH_DATE}, NWS CLI -> The Weather Company); pass "
            "allow_weather_company_settlement=True only after confirming settled values "
            "from the new source agree with NWS CLI for this station."
        )
    if eval_end <= TRAIN_END:
        return None
    if protocol_path is None:
        raise ValueError(
            f"eval_end {eval_end} is after TRAIN_END ({TRAIN_END}); evaluating past it requires "
            "a predeclared, verified --protocol. None was given."
        )
    try:
        declared = protocol_mod.load(protocol_path)
    except (FileNotFoundError, OSError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"Could not load a protocol from {protocol_path}: {exc}") from exc
    verification = protocol_mod.verify(declared, protocol_path)
    if not verification.valid:
        raise ValueError(f"Protocol at {protocol_path} failed verification: {verification.message}")
    if declared.run_kind != REQUIRED_PROTOCOL_RUN_KIND:
        raise ValueError(
            f"Protocol at {protocol_path} has run_kind {declared.run_kind!r}; this backtest "
            f"requires {REQUIRED_PROTOCOL_RUN_KIND!r} (it replays historical market data)."
        )
    if PROTOCOL_HYPOTHESIS_MARKER not in declared.hypothesis:
        raise ValueError(
            f"Protocol at {protocol_path}'s hypothesis does not contain the required marker "
            f"{PROTOCOL_HYPOTHESIS_MARKER!r}; it does not verifiably govern this backtest: "
            f"{declared.hypothesis!r}"
        )
    return declared


# --- 5. Orchestration: paired, event-clustered comparison against the market -

def _paired_comparison(
    rows_: list[dict], n_resamples: int, confidence: float, seed: int
) -> dict[str, Any]:
    """Paired-by-event Brier comparison plus its event-clustered CI.

    Reuses `paired_brier_by_event` (correct per-event averaging) for the
    point estimate and `event_clustered_bootstrap_ci` (event-level
    resampling) for the CI on its per-event `brier_improvement` values --
    the same two building blocks `weather_backtest.py` already ships,
    combined here rather than a third CI routine written from scratch.
    """
    if not rows_:
        return {
            "n_events": 0, "n_markets": 0, "model_brier": None, "market_brier": None,
            "brier_improvement": None, "per_event": [], "ci_low": None, "ci_high": None,
            "n_resamples": n_resamples, "confidence": confidence,
        }
    paired = paired_brier_by_event(rows_)
    values_by_event = {e["event_key"]: e["brier_improvement"] for e in paired["per_event"]}
    ci = event_clustered_bootstrap_ci(values_by_event, n_resamples, confidence, seed)
    return {**paired, "ci_low": ci["ci_low"], "ci_high": ci["ci_high"],
            "n_resamples": n_resamples, "confidence": confidence}


def run_backtest(
    rows: list[dict],
    station_codes: list[str],
    train_start: str | date,
    train_end: str | date,
    eval_start: str | date,
    eval_end: str | date,
    model_id: str = DEFAULT_MODEL_ID,
    lead: str = DEFAULT_LEAD,
    min_models: int = MIN_MODELS_DEFAULT,
    sigma_floor_f: float = SIGMA_FLOOR_F,
    protocol_path: str | Path | None = None,
    allow_weather_company_settlement: bool = False,
    session: Any = None,
    n_resamples: int = DEFAULT_N_RESAMPLES,
    confidence: float = DEFAULT_CONFIDENCE,
    seed: int = DEFAULT_SEED,
) -> dict[str, Any]:
    """Fit on TRAIN, score all three variants against Kalshi prices on EVAL. See module docstring.

    Args:
        rows: Rows matching the frozen schema, covering both TRAIN and EVAL
            dates (indexed here by `(station, date_lst)`; a `(station, date)`
            in `[eval_start, eval_end]` with no matching row is skipped
            under `"no_ensemble_row"`).
        station_codes: e.g. `["KNYC", "KMDW", "KMIA", "KAUS"]`.
        train_start, train_end, eval_start, eval_end: Date ranges; see
            `fit_variants` for the train/eval overlap refusal.
        model_id, lead, min_models, sigma_floor_f: See `fit_variants`.
        protocol_path, allow_weather_company_settlement: See
            `_enforce_period_guards`.
        session: Optional `requests.Session` (tests), passed to
            `fetch_event_at_decision`.
        n_resamples, confidence, seed: Bootstrap parameters, shared across
            every comparison in `COMPARISONS`.

    Returns:
        A JSON-serializable (`allow_nan=False` safe) report dict with
        `fit_diagnostics`, `station_fits`, `comparisons` (one entry per
        `COMPARISONS` key), and `calibration` (one entry per variant plus
        `"market"`).

    Raises:
        ValueError: From `fit_variants`, `_enforce_period_guards`, or an
            invalid eval date range.
    """
    eval_start_d, eval_end_d = _as_date(eval_start), _as_date(eval_end)
    if eval_start_d > eval_end_d:
        raise ValueError(f"eval_start {eval_start_d} must be on or before eval_end {eval_end_d}")
    verified_protocol = _enforce_period_guards(eval_end_d, protocol_path, allow_weather_company_settlement)

    fits = fit_variants(
        rows, train_start, train_end, eval_start_d, eval_end_d,
        model_id=model_id, lead=lead, min_models=min_models, sigma_floor_f=sigma_floor_f,
    )

    rows_by_key = {(row["station"], row["date_lst"]): row for row in rows}

    eval_skip_reasons: Counter = Counter()
    calibration_rows: dict[str, list[dict]] = {variant: [] for variant in (*VARIANTS, "market")}
    pair_rows: dict[str, list[dict]] = {name: [] for name in COMPARISONS}

    for target_date in _date_range(eval_start_d, eval_end_d):
        for station in station_codes:
            row = rows_by_key.get((station, target_date.isoformat()))
            if row is None:
                eval_skip_reasons["no_ensemble_row"] += 1
                continue
            reason = _usability_reason(row, model_id, lead, min_models)
            if reason is not None:
                eval_skip_reasons[reason] += 1
                continue
            predictions: dict[str, tuple[float, float]] = {}
            for variant in VARIANTS:
                pred = predict(fits, variant, row)
                if pred is None:
                    predictions = None
                    break
                predictions[variant] = pred
            if predictions is None:
                eval_skip_reasons["prediction_unavailable"] += 1
                continue

            series = STATION_TO_SERIES.get(station)
            if series is None:
                raise ValueError(f"No Kalshi series mapped for station {station!r}")
            event_rows = fetch_event_at_decision(series, target_date, session=session)
            if not event_rows:
                eval_skip_reasons["no_market_data"] += 1
                continue
            event_key = event_rows[0]["event_ticker"]

            for market_row in event_rows:
                price = market_row["price"]
                if price.status != "ok":
                    eval_skip_reasons[price.status] += 1
                    continue
                outcome = 1.0 if market_row["result"] == "yes" else (0.0 if market_row["result"] == "no" else None)
                if outcome is None:
                    eval_skip_reasons["unsettled_result"] += 1
                    continue

                market_p = (price.yes_bid_cents + price.yes_ask_cents) / 200.0
                lower, upper = market_row["lower_bound_f"], market_row["upper_bound_f"]
                brier = {"market": (market_p - outcome) ** 2}
                calibration_rows["market"].append({"p": market_p, "outcome": outcome})
                for variant in VARIANTS:
                    mu, sigma = predictions[variant]
                    p_yes = bracket_probability(mu, sigma, lower, upper)
                    brier[variant] = (p_yes - outcome) ** 2
                    calibration_rows[variant].append({"p": p_yes, "outcome": outcome})

                for name, (a, b) in COMPARISONS.items():
                    pair_rows[name].append({"event_key": event_key, "model_brier": brier[a], "market_brier": brier[b]})

    comparisons = {name: _paired_comparison(pair_rows[name], n_resamples, confidence, seed) for name in COMPARISONS}
    calibration = {variant: calibration_bins(calibration_rows[variant]) for variant in (*VARIANTS, "market")}

    return {
        "model_id": model_id, "lead": lead, "min_models": min_models, "sigma_floor_f": sigma_floor_f,
        "train_start": fits.train_start.isoformat(), "train_end": fits.train_end.isoformat(),
        "eval_start": eval_start_d.isoformat(), "eval_end": eval_end_d.isoformat(),
        "protocol_verified": verified_protocol is not None,
        "fit_diagnostics": {
            "a": fits.spread_sigma.a, "b": fits.spread_sigma.b, "b_se": fits.spread_sigma.b_se,
            "spread_skill_correlation": fits.spread_sigma.spread_skill_correlation,
            "n_train_pairs": fits.spread_sigma.n, "sigma_floor_f": fits.spread_sigma.sigma_floor_f,
        },
        "station_fits": {
            "single": {station: asdict(fit) for station, fit in fits.single.items()},
            "ensemble_mean": {station: asdict(fit) for station, fit in fits.ensemble_mean.items()},
        },
        "train_skip_reasons": fits.skip_reasons,
        "eval_skip_reasons": dict(eval_skip_reasons),
        "comparisons": comparisons,
        "calibration": calibration,
    }


# --- 6. Report ------------------------------------------------------------------

def format_report(report: dict[str, Any]) -> str:
    """Human-readable report text, in the order the module's deliverable requires:

    1. Spread-skill correlation and fitted `b` (decides whether the idea can
       work at all).
    2. Paired Brier for each variant vs. each other and vs. the market, each
       with a clustered CI.
    3. Calibration bins per variant.
    4. A one-line verdict that does not conflate beating `single` with
       beating the market.
    """
    lines: list[str] = []
    diag = report["fit_diagnostics"]
    corr = diag["spread_skill_correlation"]

    lines.append("=" * 78)
    lines.append("1. Spread-skill diagnostic (decides whether spread_sigma can work at all)")
    lines.append("=" * 78)
    lines.append(
        f"spread_skill_correlation={corr:.4f}   b={diag['b']:.4f} (se={diag['b_se']:.4f})   "
        f"a={diag['a']:.4f}   n={diag['n_train_pairs']}   sigma_floor_f={diag['sigma_floor_f']:.2f}"
    )
    if abs(corr) < SPREAD_SKILL_DEAD_THRESHOLD:
        lines.append(
            f"Spread-skill correlation ({corr:.4f}) is near zero: a spread that does not predict "
            "error cannot improve a sigma."
        )

    lines.append("")
    lines.append("-" * 78)
    lines.append("2. Paired Brier comparisons (event-clustered CI)")
    lines.append("-" * 78)
    lines.append(f"{'comparison':>32} {'n_events':>9} {'improvement':>12} {'ci_low':>9} {'ci_high':>9}")
    for name in COMPARISONS:
        c = report["comparisons"][name]
        improvement = "n/a" if c["brier_improvement"] is None else f"{c['brier_improvement']:.5f}"
        ci_low = "n/a" if c["ci_low"] is None else f"{c['ci_low']:.5f}"
        ci_high = "n/a" if c["ci_high"] is None else f"{c['ci_high']:.5f}"
        lines.append(f"{name:>32} {c['n_events']:>9} {improvement:>12} {ci_low:>9} {ci_high:>9}")

    lines.append("")
    lines.append("-" * 78)
    lines.append("3. Calibration bins per variant")
    lines.append("-" * 78)
    for variant in (*VARIANTS, "market"):
        lines.append(f"  {variant}:")
        for b in report["calibration"][variant]:
            if b["n"] == 0:
                continue
            lines.append(
                f"    [{b['bin_low']:.1f},{b['bin_high']:.1f}) n={b['n']:>4} "
                f"mean_p={b['mean_predicted_p']:.3f} empirical={b['empirical_frequency']:.3f}"
            )

    lines.append("")
    lines.append("-" * 78)
    lines.append("4. Verdict")
    lines.append("-" * 78)
    market_cmp = report["comparisons"]["spread_sigma_vs_market"]
    if market_cmp["ci_low"] is not None and market_cmp["ci_low"] > 0:
        lines.append(
            f"spread_sigma beats the market: paired Brier improvement {market_cmp['brier_improvement']:.5f} "
            f"with a {market_cmp['confidence']:.0%} clustered CI ({market_cmp['ci_low']:.5f}, "
            f"{market_cmp['ci_high']:.5f}) excluding zero."
        )
    else:
        lines.append(
            "No variant's paired Brier improvement over the market excludes zero at this confidence. "
            "Beating `single` is not beating the market, and only beating the market matters."
        )

    return "\n".join(lines)


# --- 7. CLI -----------------------------------------------------------------

def load_rows_jsonl(path: str | Path) -> list[dict]:
    """Read one frozen-schema row per line from a JSONL file produced by the ingestion module."""
    rows = []
    with Path(path).open() as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _split_stations(value: str) -> list[str]:
    return [s.strip().upper() for s in value.split(",") if s.strip()]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m src.research.ensemble_model",
        description="Compare single-model, ensemble-mean, and spread-sigma weather forecasts "
                     "against Kalshi bracket prices.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    default_stations = ",".join(sorted(STATION_TO_SERIES))

    run_p = sub.add_parser("run", help="Fit on TRAIN, score all three variants on EVAL, write a JSON report.")
    run_p.add_argument("--rows", required=True, help="Path to a JSONL file of frozen-schema rows")
    run_p.add_argument("--stations", default=default_stations, help="Comma-separated station codes")
    run_p.add_argument("--train-start", required=True)
    run_p.add_argument("--train-end", required=True)
    run_p.add_argument("--eval-start", required=True)
    run_p.add_argument("--eval-end", required=True)
    run_p.add_argument("--model-id", default=DEFAULT_MODEL_ID)
    run_p.add_argument("--lead", choices=LEADS, default=DEFAULT_LEAD)
    run_p.add_argument("--min-models", type=int, default=MIN_MODELS_DEFAULT)
    run_p.add_argument("--sigma-floor", type=float, default=SIGMA_FLOOR_F)
    run_p.add_argument("--protocol", default=None, help="Path to a predeclared, saved protocol JSON")
    run_p.add_argument("--allow-weather-company-settlement", action="store_true")
    run_p.add_argument("--output", required=True, help="Output JSON report path")

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args(argv)
    if args.command == "run":
        rows = load_rows_jsonl(args.rows)
        report = run_backtest(
            rows, _split_stations(args.stations), args.train_start, args.train_end,
            args.eval_start, args.eval_end, model_id=args.model_id, lead=args.lead,
            min_models=args.min_models, sigma_floor_f=args.sigma_floor,
            protocol_path=args.protocol,
            allow_weather_company_settlement=args.allow_weather_company_settlement,
        )
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2, allow_nan=False, default=str))
        print(format_report(report))
        print(f"\nWrote full report to {out}")


if __name__ == "__main__":
    main()
