"""Event-clustered uncertainty for paired Brier score comparisons.

`src/paper/broker.py`'s `report()` compares a model's Brier score against the
market's Brier score on the same markets. Its own evidence note admits the
flaw this module addresses: correlated markets are not independent. On
Kalshi, one weather event (one city, one date) is offered as many bracket
markets, so twenty-two brackets for one city-day are close to ONE
independent observation, not twenty-two. Treating them as twenty-two makes
any Brier improvement look far more certain than the data supports.

This module does not establish, prove, or demonstrate an edge. It quantifies
uncertainty, honestly, by resampling whole events rather than individual
markets. A wider interval is not a worse result — it is the correct one when
the underlying observations are correlated.

Row format: each row is a dict with:
    event_key: str — the Kalshi event grouping key (see `src/paper/events.py`).
    model_brier: float — the model's per-market squared Brier error,
        (yes_probability - outcome) ** 2.
    market_brier: float — the market's per-market squared Brier error,
        (market_yes_probability - outcome) ** 2.
These are the same per-market values `PaperBroker.report()` already computes
before averaging; this module only changes how they are aggregated into an
uncertainty estimate.
"""
from __future__ import annotations

import logging
import math
from typing import Any

import numpy as np

from src.paper.events import cluster_by_event, cluster_summary

logger = logging.getLogger(__name__)

# Fixed default so callers who omit `seed` still get reproducible output;
# never rely on numpy's global RNG state, which is process-wide and mutable.
DEFAULT_SEED = 0

# 2000 resamples is a conventional bootstrap default (Efron & Tibshirani):
# enough for stable 95% percentile estimates without being slow.
DEFAULT_N_RESAMPLES = 2000

DEFAULT_CONFIDENCE = 0.95

# Below this many independent units (events for the clustered estimator,
# markets for the naive one), resampling with replacement cannot estimate
# variability at all: every resample is a copy of the same single unit, so a
# reported interval would misleadingly show zero width instead of "unknown".
MIN_UNITS_FOR_BOOTSTRAP = 2

# A per-market Brier value here is a squared probability error,
# (p - outcome) ** 2 with p in [0, 1] and outcome in {0, 1}, so it is always
# in [0, 1]. A value outside that range, or a NaN/infinity, means the caller
# built the row wrong upstream — reject it rather than compute a nonsense
# improvement from it.
MIN_BRIER = 0.0
MAX_BRIER = 1.0


def _validate_brier(value: Any, field_name: str) -> float:
    """Validate one Brier value: a finite real number in [MIN_BRIER, MAX_BRIER].

    Args:
        value: The value to validate.
        field_name: Name to include in the error message, e.g. "model_brier".

    Returns:
        The validated value, unchanged.

    Raises:
        ValueError: If value is not a real number (booleans excluded), is
            NaN or infinite, or falls outside [MIN_BRIER, MAX_BRIER].
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a real number, got {value!r}")
    if not math.isfinite(value):
        raise ValueError(f"{field_name} must be finite, got {value!r}")
    if not (MIN_BRIER <= value <= MAX_BRIER):
        raise ValueError(
            f"{field_name} must be within [{MIN_BRIER}, {MAX_BRIER}] (a squared probability error), "
            f"got {value!r}")
    return value


def _validate_rows(rows: list[dict[str, Any]]) -> None:
    """Validate model_brier and market_brier on every row.

    Args:
        rows: Prediction/outcome rows (see module docstring for the exact
            format).

    Raises:
        ValueError: From `_validate_brier`, naming the first invalid field
            found and the row's position.
    """
    for index, row in enumerate(rows):
        try:
            _validate_brier(row.get("model_brier"), "model_brier")
            _validate_brier(row.get("market_brier"), "market_brier")
        except ValueError as exc:
            raise ValueError(f"row {index}: {exc}") from exc


def _validate_bootstrap_params(n_resamples: int, confidence: float) -> None:
    """Validate n_resamples and confidence before any bootstrap draw.

    Args:
        n_resamples: Number of bootstrap resamples requested.
        confidence: Requested interval coverage.

    Raises:
        ValueError: If n_resamples is not a positive integer, or confidence
            is not strictly between 0 and 1. A 100% interval would need to
            span the full resample range and a 0% interval is undefined, so
            neither endpoint is accepted.
    """
    if isinstance(n_resamples, bool) or not isinstance(n_resamples, int) or n_resamples < 1:
        raise ValueError(f"n_resamples must be a positive integer, got {n_resamples!r}")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not (0 < confidence < 1):
        raise ValueError(f"confidence must be strictly between 0 and 1, got {confidence!r}")


def paired_brier_by_event(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Collapse each event to one paired observation, then compare.

    Within each event, average model_brier and market_brier across its
    markets first; only then compare model to market across events. This is
    the headline estimator — it never lets an event with many brackets
    outweigh an event with one.

    Args:
        rows: Prediction/outcome rows (see module docstring for the exact
            format).

    Returns:
        A dict with `n_events`, `n_markets`, `model_brier`, `market_brier`,
        `brier_improvement` (market_brier - model_brier, mean across
        events), and `per_event` (a list of per-event
        `{"event_key", "n_markets", "model_brier", "market_brier",
        "brier_improvement"}` dicts, sorted by event_key). All scalar
        statistics are `None` on empty input rather than NaN.

    Raises:
        ValueError: Propagated from `cluster_by_event` for invalid
            event_key values, or if any row's model_brier/market_brier is
            non-numeric, NaN, infinite, or outside [0, 1].
    """
    _validate_rows(rows)
    clusters = cluster_by_event(rows)
    n_markets = len(rows)
    n_events = len(clusters)
    if n_events == 0:
        return {"n_events": 0, "n_markets": 0, "model_brier": None, "market_brier": None,
                "brier_improvement": None, "per_event": []}
    per_event = []
    for cluster in clusters:
        cluster_rows = cluster["rows"]
        model_mean = sum(r["model_brier"] for r in cluster_rows) / cluster["n_markets"]
        market_mean = sum(r["market_brier"] for r in cluster_rows) / cluster["n_markets"]
        per_event.append({"event_key": cluster["event_key"], "n_markets": cluster["n_markets"],
                           "model_brier": model_mean, "market_brier": market_mean,
                           "brier_improvement": market_mean - model_mean})
    model_brier = sum(e["model_brier"] for e in per_event) / n_events
    market_brier = sum(e["market_brier"] for e in per_event) / n_events
    return {"n_events": n_events, "n_markets": n_markets, "model_brier": model_brier,
            "market_brier": market_brier, "brier_improvement": market_brier - model_brier,
            "per_event": per_event}


def cluster_bootstrap_ci(rows: list[dict[str, Any]], n_resamples: int = DEFAULT_N_RESAMPLES,
                          confidence: float = DEFAULT_CONFIDENCE, seed: int = DEFAULT_SEED) -> dict[str, Any]:
    """Bootstrap a confidence interval by resampling whole events.

    This is the core correction for correlated bracket markets: each
    resample draws n_events events with replacement (never individual
    markets) and recomputes the paired-by-event improvement on that
    resample. Resampling markets instead of events would reproduce the bug
    this module exists to fix.

    Args:
        rows: Prediction/outcome rows (see module docstring for the exact
            format).
        n_resamples: Number of bootstrap resamples to draw.
        confidence: Interval coverage, e.g. 0.95 for a 95% interval.
        seed: Seed for a local `numpy.random.Generator`. The same seed and
            input always produce the same interval; the global numpy RNG is
            never used.

    Returns:
        A dict with `estimate` (the point estimate from
        `paired_brier_by_event`), `ci_low`, `ci_high`, `n_events`,
        `n_markets`, `n_resamples`, and `confidence`. `ci_low`/`ci_high` are
        `None` when there are fewer than `MIN_UNITS_FOR_BOOTSTRAP` events —
        resampling one event with replacement cannot estimate variability,
        so reporting a zero-width interval would be a fabricated number, not
        an honest one. `estimate` is `None` only on empty input.

    Raises:
        ValueError: If n_resamples is not a positive integer or confidence
            is not strictly between 0 and 1; propagated from
            `paired_brier_by_event` for invalid event_key or brier values.
    """
    _validate_bootstrap_params(n_resamples, confidence)
    paired = paired_brier_by_event(rows)
    n_events, n_markets = paired["n_events"], paired["n_markets"]
    base = {"n_events": n_events, "n_markets": n_markets, "n_resamples": n_resamples, "confidence": confidence}
    if n_events == 0:
        return {"estimate": None, "ci_low": None, "ci_high": None, **base}
    point_estimate = paired["brier_improvement"]
    if n_events < MIN_UNITS_FOR_BOOTSTRAP:
        return {"estimate": point_estimate, "ci_low": None, "ci_high": None, **base}
    per_event_improvement = np.array([e["brier_improvement"] for e in paired["per_event"]])
    rng = np.random.default_rng(seed)
    resample_indices = rng.integers(0, n_events, size=(n_resamples, n_events))
    resample_means = per_event_improvement[resample_indices].mean(axis=1)
    ci_low, ci_high = _percentile_ci(resample_means, confidence)
    return {"estimate": point_estimate, "ci_low": ci_low, "ci_high": ci_high, **base}


def effective_sample_size(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Report how many independent observations the data actually supports.

    Args:
        rows: Prediction/outcome rows (see module docstring for the exact
            format).

    Returns:
        A dict with `n_markets` (the naive count), `n_events` (the
        statistically honest count), and `naive_to_effective_ratio`
        (n_markets / n_events — how many times too generous the naive count
        is). The ratio is `None` when there are no events.

    Raises:
        ValueError: Propagated from `cluster_by_event` for invalid
            event_key values.
    """
    summary = cluster_summary(rows)
    n_markets, n_events = summary["n_markets"], summary["n_events"]
    ratio = None if n_events == 0 else n_markets / n_events
    return {"n_markets": n_markets, "n_events": n_events, "naive_to_effective_ratio": ratio}


def naive_vs_clustered(rows: list[dict[str, Any]], n_resamples: int = DEFAULT_N_RESAMPLES,
                        confidence: float = DEFAULT_CONFIDENCE, seed: int = DEFAULT_SEED) -> dict[str, Any]:
    """Compute the naive per-market CI and the clustered per-event CI side by side.

    The naive interval resamples individual markets with replacement,
    exactly the (incorrect) assumption that every bracket is an independent
    observation. It exists here only so its overstatement is visible next to
    the honest, clustered one — never as a recommended estimator on its own.

    Args:
        rows: Prediction/outcome rows (see module docstring for the exact
            format).
        n_resamples: Number of bootstrap resamples to draw for each CI.
        confidence: Interval coverage, e.g. 0.95 for a 95% interval.
        seed: Seed for the local `numpy.random.Generator`, reused for both
            the naive and clustered resamples so a single seed makes the
            whole comparison reproducible.

    Returns:
        A dict with `naive` (as `cluster_bootstrap_ci` but resampling
        markets), `clustered` (as `cluster_bootstrap_ci`), `naive_ci_width`,
        `clustered_ci_width`, and `clustered_to_naive_width_ratio`
        (`clustered_ci_width / naive_ci_width`, e.g. 3.0 means "the naive CI
        is 3x too narrow"). Widths and the ratio are `None` wherever an
        underlying CI bound is `None`.

    Raises:
        ValueError: If n_resamples is not a positive integer or confidence
            is not strictly between 0 and 1; propagated from
            `cluster_by_event`/`paired_brier_by_event` for invalid event_key
            or brier values.
    """
    naive = _naive_market_bootstrap_ci(rows, n_resamples, confidence, seed)
    clustered = cluster_bootstrap_ci(rows, n_resamples, confidence, seed)
    naive_width = _width(naive)
    clustered_width = _width(clustered)
    ratio = None
    if naive_width is not None and clustered_width is not None and naive_width > 0:
        ratio = clustered_width / naive_width
    return {"naive": naive, "clustered": clustered, "naive_ci_width": naive_width,
            "clustered_ci_width": clustered_width, "clustered_to_naive_width_ratio": ratio}


def _naive_market_bootstrap_ci(rows: list[dict[str, Any]], n_resamples: int, confidence: float,
                                seed: int) -> dict[str, Any]:
    """Bootstrap a CI by resampling individual markets (the biased baseline).

    Kept private: this is the estimator `naive_vs_clustered` exists to show
    is too narrow, not a standalone recommendation.

    Raises:
        ValueError: If n_resamples is not a positive integer, confidence is
            not strictly between 0 and 1, or any row's model_brier/
            market_brier is non-numeric, NaN, infinite, or outside [0, 1].
    """
    _validate_bootstrap_params(n_resamples, confidence)
    _validate_rows(rows)
    n_markets = len(rows)
    base = {"n_markets": n_markets, "n_resamples": n_resamples, "confidence": confidence}
    if n_markets == 0:
        return {"estimate": None, "ci_low": None, "ci_high": None, **base}
    model = np.array([r["model_brier"] for r in rows])
    market = np.array([r["market_brier"] for r in rows])
    point_estimate = float(market.mean() - model.mean())
    if n_markets < MIN_UNITS_FOR_BOOTSTRAP:
        return {"estimate": point_estimate, "ci_low": None, "ci_high": None, **base}
    rng = np.random.default_rng(seed)
    resample_indices = rng.integers(0, n_markets, size=(n_resamples, n_markets))
    resample_means = market[resample_indices].mean(axis=1) - model[resample_indices].mean(axis=1)
    ci_low, ci_high = _percentile_ci(resample_means, confidence)
    return {"estimate": point_estimate, "ci_low": ci_low, "ci_high": ci_high, **base}


def _percentile_ci(resample_means: np.ndarray, confidence: float) -> tuple[float, float]:
    """Return the percentile-method (ci_low, ci_high) bounds from bootstrap resample means."""
    alpha = 1 - confidence
    lower_pct, upper_pct = 100 * alpha / 2, 100 * (1 - alpha / 2)
    ci_low, ci_high = np.percentile(resample_means, [lower_pct, upper_pct])
    return float(ci_low), float(ci_high)


def _width(ci_result: dict[str, Any]) -> float | None:
    """Return ci_high - ci_low, or None if either bound is undefined."""
    if ci_result["ci_low"] is None or ci_result["ci_high"] is None:
        return None
    return ci_result["ci_high"] - ci_result["ci_low"]
