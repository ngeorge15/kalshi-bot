"""Validation metrics for prediction model evaluation.

Provides Brier score, accuracy, and calibration error functions used by
the walk-forward validator and the analytics layer (Phase 5).
"""

import numpy as np


def brier_score(probs: np.ndarray, labels: np.ndarray) -> float:
    """Compute Brier score: mean squared error between predicted probs and labels.

    Lower is better. Perfect model = 0.0. Random (0.5 always) = 0.25.
    Calibrated probabilities will score lower than raw model output.

    Args:
        probs: Predicted probabilities array, shape (n,), values in [0.0, 1.0].
        labels: Binary labels array, shape (n,), values 0 or 1.

    Returns:
        Float in [0.0, 1.0]. Typical good model: 0.15-0.22.

    Raises:
        ValueError: If probs is empty.
    """
    probs = np.asarray(probs, dtype=float)
    labels = np.asarray(labels, dtype=float)
    if len(probs) == 0:
        raise ValueError("Cannot compute Brier score on empty arrays")
    return float(np.mean((probs - labels) ** 2))


def accuracy(probs: np.ndarray, labels: np.ndarray, threshold: float = 0.5) -> float:
    """Compute accuracy: fraction of correct classifications.

    Args:
        probs: Predicted probabilities array.
        labels: Binary labels array.
        threshold: Classification threshold. Default 0.5.

    Returns:
        Float in [0.0, 1.0].
    """
    probs = np.asarray(probs, dtype=float)
    labels = np.asarray(labels, dtype=float)
    predictions = (probs >= threshold).astype(float)
    return float(np.mean(predictions == labels))


def calibration_error(
    probs: np.ndarray,
    labels: np.ndarray,
    n_bins: int = 10,
) -> float:
    """Compute Expected Calibration Error (ECE).

    ECE measures whether predicted probabilities match empirical frequencies.
    Lower is better. Well-calibrated model < 0.05.

    Args:
        probs: Predicted probabilities, shape (n,).
        labels: Binary labels, shape (n,).
        n_bins: Number of bins for the reliability diagram. Default 10.

    Returns:
        Float ECE value. Typical range 0.01-0.15.
    """
    probs = np.asarray(probs, dtype=float)
    labels = np.asarray(labels, dtype=float)
    bins = np.linspace(0.0, 1.0, n_bins + 1)
    ece = 0.0
    n = len(probs)
    for i in range(n_bins):
        mask = (probs >= bins[i]) & (probs < bins[i + 1])
        if mask.sum() == 0:
            continue
        bin_prob = probs[mask].mean()
        bin_acc = labels[mask].mean()
        bin_weight = mask.sum() / n
        ece += bin_weight * abs(bin_prob - bin_acc)
    return float(ece)
