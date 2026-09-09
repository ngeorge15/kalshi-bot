"""Event-clustered uncertainty invariants for paired Brier score comparisons.

The headline property under test: resampling whole events (not individual
markets) must produce visibly wider intervals when observations within an
event are correlated — that is the entire point of this module.
"""
import numpy as np
import pytest

from src.paper.uncertainty import (
    cluster_bootstrap_ci,
    effective_sample_size,
    naive_vs_clustered,
    paired_brier_by_event,
)


def row(event_key, model_brier, market_brier):
    return {"event_key": event_key, "model_brier": model_brier, "market_brier": market_brier}


def one_market_per_event(n=50):
    # n_events == n_markets: clustering carries no extra information, so
    # clustered and naive bootstraps should coincide.
    rng = np.random.default_rng(7)
    # Zero-padded keys so lexicographic cluster sorting matches insertion
    # order — needed for the exact-equivalence check against naive resampling.
    return [row(f"EVT-{i:03d}", float(0.20 + rng.normal(0, 0.02)), float(0.25 + rng.normal(0, 0.02)))
            for i in range(n)]


def one_dominant_event(n_markets_per_event=50):
    # Two wildly different events, each internally noisy but with a large
    # between-event gap. Only 2 independent units exist, so a cluster
    # bootstrap over events must show far more uncertainty than a bootstrap
    # that pools all 100 markets as if independent.
    rng = np.random.default_rng(11)
    rows = []
    for _ in range(n_markets_per_event):
        rows.append(row("A", float(0.10 + rng.normal(0, 0.01)), float(0.16 + rng.normal(0, 0.01))))
    for _ in range(n_markets_per_event):
        rows.append(row("B", float(0.30 + rng.normal(0, 0.01)), float(0.10 + rng.normal(0, 0.01))))
    return rows


# --- paired_brier_by_event -------------------------------------------------

def test_paired_brier_by_event_weights_by_event_not_by_market():
    # Event A has 9 markets all with improvement 0.0; event B has 1 market
    # with improvement 1.0. Weighting by market would give ~0.1; weighting
    # by event (correct) gives the plain average of the two events: 0.5.
    rows = [row("A", 0.5, 0.5) for _ in range(9)] + [row("B", 0.0, 1.0)]
    result = paired_brier_by_event(rows)
    assert result["n_events"] == 2
    assert result["n_markets"] == 10
    assert result["brier_improvement"] == pytest.approx(0.5)


def test_paired_brier_by_event_empty_returns_none():
    result = paired_brier_by_event([])
    assert result == {"n_events": 0, "n_markets": 0, "model_brier": None, "market_brier": None,
                       "brier_improvement": None, "per_event": []}


def test_paired_brier_by_event_rejects_invalid_event_key():
    with pytest.raises(ValueError, match="event_key"):
        paired_brier_by_event([row("", 0.1, 0.2)])


# --- cluster_bootstrap_ci ----------------------------------------------------

def test_headline_clustered_ci_is_dramatically_wider_than_naive():
    rows = one_dominant_event(n_markets_per_event=50)
    comparison = naive_vs_clustered(rows, n_resamples=2000, seed=123)
    assert comparison["naive_ci_width"] is not None
    assert comparison["clustered_ci_width"] is not None
    # "Dramatically wider": require at least 3x, matching the overstatement
    # this module exists to expose (naive treats 100 markets as independent;
    # the data actually supports 2 independent events).
    assert comparison["clustered_to_naive_width_ratio"] >= 3.0
    assert comparison["clustered_ci_width"] > comparison["naive_ci_width"]


def test_one_market_per_event_naive_and_clustered_are_close():
    rows = one_market_per_event(n=50)
    comparison = naive_vs_clustered(rows, n_resamples=2000, seed=99)
    # With exactly one market per event, resampling events IS resampling
    # markets, so the two estimators should coincide (up to float rounding).
    assert comparison["clustered_to_naive_width_ratio"] == pytest.approx(1.0, abs=1e-9)
    assert comparison["naive"]["ci_low"] == pytest.approx(comparison["clustered"]["ci_low"], abs=1e-9)
    assert comparison["naive"]["ci_high"] == pytest.approx(comparison["clustered"]["ci_high"], abs=1e-9)


def test_cluster_bootstrap_ci_empty_input_returns_none():
    result = cluster_bootstrap_ci([], seed=0)
    assert result["estimate"] is None
    assert result["ci_low"] is None
    assert result["ci_high"] is None
    assert result["n_events"] == 0
    assert result["n_markets"] == 0


def test_cluster_bootstrap_ci_single_event_returns_none_ci_not_none_estimate():
    rows = [row("ONLY", 0.1 + 0.001 * i, 0.2 - 0.001 * i) for i in range(22)]
    result = cluster_bootstrap_ci(rows, seed=0)
    assert result["estimate"] is not None
    assert result["ci_low"] is None
    assert result["ci_high"] is None
    assert result["n_events"] == 1
    assert result["n_markets"] == 22


def test_cluster_bootstrap_ci_all_identical_scores_no_nan_or_inf():
    rows = [row(f"EVT-{i}", 0.2, 0.2) for i in range(10)]
    result = cluster_bootstrap_ci(rows, seed=0)
    for key in ("estimate", "ci_low", "ci_high"):
        value = result[key]
        assert value is not None
        assert np.isfinite(value)
    assert result["estimate"] == pytest.approx(0.0)
    assert result["ci_low"] == pytest.approx(0.0)
    assert result["ci_high"] == pytest.approx(0.0)


def test_cluster_bootstrap_ci_is_deterministic_given_seed():
    rows = one_dominant_event(n_markets_per_event=20)
    first = cluster_bootstrap_ci(rows, seed=42, n_resamples=500)
    second = cluster_bootstrap_ci(rows, seed=42, n_resamples=500)
    assert first == second


def test_cluster_bootstrap_ci_different_seeds_can_differ():
    # Needs enough distinct events that resample means form a continuum
    # rather than a handful of fixed combinations (two events would let both
    # seeds land on the same min/max by chance).
    rows = one_market_per_event(n=50)
    first = cluster_bootstrap_ci(rows, seed=1, n_resamples=500)
    second = cluster_bootstrap_ci(rows, seed=2, n_resamples=500)
    assert first["ci_low"] != second["ci_low"] or first["ci_high"] != second["ci_high"]


def test_cluster_bootstrap_ci_confidence_level_widens_interval():
    rows = one_dominant_event(n_markets_per_event=30)
    narrow = cluster_bootstrap_ci(rows, seed=5, n_resamples=2000, confidence=0.5)
    wide = cluster_bootstrap_ci(rows, seed=5, n_resamples=2000, confidence=0.99)
    assert (wide["ci_high"] - wide["ci_low"]) > (narrow["ci_high"] - narrow["ci_low"])


# --- effective_sample_size ---------------------------------------------------

def test_effective_sample_size_reports_the_gap():
    rows = [row("ONLY", 0.1, 0.2) for _ in range(22)]
    result = effective_sample_size(rows)
    assert result == {"n_markets": 22, "n_events": 1, "naive_to_effective_ratio": 22.0}


def test_effective_sample_size_empty():
    result = effective_sample_size([])
    assert result == {"n_markets": 0, "n_events": 0, "naive_to_effective_ratio": None}


# --- naive_vs_clustered structure -------------------------------------------

def test_naive_vs_clustered_empty_input_all_none():
    result = naive_vs_clustered([])
    assert result["naive"]["ci_low"] is None
    assert result["clustered"]["ci_low"] is None
    assert result["naive_ci_width"] is None
    assert result["clustered_ci_width"] is None
    assert result["clustered_to_naive_width_ratio"] is None


# --- input validation: NaN / infinite / out-of-range brier, and bootstrap params ---

def test_nan_model_brier_rejected():
    rows = one_market_per_event(n=5) + [row("EVT-BAD", float("nan"), 0.2)]
    with pytest.raises(ValueError, match="model_brier"):
        paired_brier_by_event(rows)
    with pytest.raises(ValueError, match="model_brier"):
        cluster_bootstrap_ci(rows)
    with pytest.raises(ValueError, match="model_brier"):
        naive_vs_clustered(rows)


def test_nan_market_brier_rejected():
    rows = [row("EVT-BAD", 0.1, float("nan"))]
    with pytest.raises(ValueError, match="market_brier"):
        paired_brier_by_event(rows)


def test_infinite_brier_rejected():
    rows = [row("EVT-BAD", float("inf"), 0.2)]
    with pytest.raises(ValueError, match="finite"):
        paired_brier_by_event(rows)
    rows_neg_inf = [row("EVT-BAD", float("-inf"), 0.2)]
    with pytest.raises(ValueError, match="finite"):
        paired_brier_by_event(rows_neg_inf)


def test_brier_above_one_rejected():
    rows = [row("EVT-BAD", 99.0, 0.2)]
    with pytest.raises(ValueError, match=r"\[0.0, 1.0\]"):
        paired_brier_by_event(rows)


def test_brier_below_zero_rejected():
    rows = [row("EVT-BAD", -0.5, 0.2)]
    with pytest.raises(ValueError, match=r"\[0.0, 1.0\]"):
        paired_brier_by_event(rows)


def test_non_numeric_brier_rejected():
    rows = [row("EVT-BAD", "0.2", 0.2)]
    with pytest.raises(ValueError, match="model_brier"):
        paired_brier_by_event(rows)


def test_n_resamples_zero_rejected():
    rows = one_market_per_event(n=10)
    with pytest.raises(ValueError, match="n_resamples"):
        cluster_bootstrap_ci(rows, n_resamples=0)


def test_n_resamples_negative_rejected():
    rows = one_market_per_event(n=10)
    with pytest.raises(ValueError, match="n_resamples"):
        cluster_bootstrap_ci(rows, n_resamples=-5)


def test_n_resamples_non_integer_rejected():
    rows = one_market_per_event(n=10)
    with pytest.raises(ValueError, match="n_resamples"):
        cluster_bootstrap_ci(rows, n_resamples=2.5)


def test_confidence_at_one_rejected():
    rows = one_market_per_event(n=10)
    with pytest.raises(ValueError, match="confidence"):
        cluster_bootstrap_ci(rows, confidence=1.0)


def test_confidence_at_zero_rejected():
    rows = one_market_per_event(n=10)
    with pytest.raises(ValueError, match="confidence"):
        cluster_bootstrap_ci(rows, confidence=0.0)


def test_confidence_out_of_range_rejected():
    rows = one_market_per_event(n=10)
    with pytest.raises(ValueError, match="confidence"):
        cluster_bootstrap_ci(rows, confidence=1.5)
    with pytest.raises(ValueError, match="confidence"):
        cluster_bootstrap_ci(rows, confidence=-0.1)


def test_bootstrap_params_validated_for_naive_and_naive_vs_clustered_too():
    rows = one_market_per_event(n=10)
    with pytest.raises(ValueError, match="n_resamples"):
        naive_vs_clustered(rows, n_resamples=0)
    with pytest.raises(ValueError, match="confidence"):
        naive_vs_clustered(rows, confidence=1.0)
