"""Tests for src/validation/metrics.py — validation metric helpers."""
import numpy as np
import pytest


def test_brier_score_perfect_predictions():
    from src.validation.metrics import brier_score

    assert brier_score([1.0, 0.0, 1.0], [1, 0, 1]) == 0.0


def test_brier_score_random_predictions():
    from src.validation.metrics import brier_score

    assert brier_score([0.5, 0.5, 0.5, 0.5], [1, 0, 1, 0]) == pytest.approx(0.25)


def test_brier_score_empty_raises():
    from src.validation.metrics import brier_score

    with pytest.raises(ValueError):
        brier_score([], [])


def test_accuracy_all_correct():
    from src.validation.metrics import accuracy

    assert accuracy([0.9, 0.1, 0.8], [1, 0, 1]) == 1.0


def test_accuracy_half_correct():
    from src.validation.metrics import accuracy

    assert accuracy([0.9, 0.9], [1, 0]) == 0.5


def test_calibration_error_perfectly_calibrated():
    from src.validation.metrics import calibration_error

    rng = np.random.RandomState(0)
    probs = rng.uniform(0, 1, 1000)
    labels = (rng.uniform(0, 1, 1000) < probs).astype(int)
    assert calibration_error(probs, labels, n_bins=10) < 0.1


def test_calibration_error_bounds():
    from src.validation.metrics import calibration_error

    ece = calibration_error([0.9, 0.9, 0.9], [0, 0, 0])
    assert 0.0 <= ece <= 1.0
