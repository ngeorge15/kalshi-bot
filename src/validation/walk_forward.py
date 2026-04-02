"""Walk-forward (rolling window) validation for time-series models.

Splits data into N expanding-window or fixed-window train/test pairs,
trains a model on each, and collects per-window metrics.  Ensures no
future leakage: each test window is strictly after its train window.

Usage::

    from src.validation.walk_forward import WalkForwardValidator
    from sklearn.linear_model import LogisticRegression

    def train_fn(X_tr, y_tr):
        return LogisticRegression().fit(X_tr, y_tr)

    def test_fn(model, X_te, y_te):
        probs = model.predict_proba(X_te)[:, 1]
        brier = float(np.mean((probs - y_te) ** 2))
        return {"brier_score": brier}

    validator = WalkForwardValidator(n_splits=4)
    for result in validator.validate(X, y, train_fn, test_fn):
        print(result["window"], result["metrics"]["brier_score"])
"""

import logging
from typing import Any, Callable, Generator

import numpy as np

logger = logging.getLogger(__name__)


class WalkForwardValidator:
    """Rolling-window walk-forward validator.

    Splits ``n`` observations into ``n_splits`` windows.  For each window ``i``:
        - Train on observations ``[0 : split_point_i]``
        - Test on observations ``[split_point_i : split_point_{i+1}]``

    This is an expanding-window approach: each subsequent window has a
    larger training set.

    Args:
        n_splits: Number of test windows. Default 4.
        min_train_size: Minimum training set size. Default 50.
    """

    def __init__(
        self, n_splits: int = 4, min_train_size: int = 50,
    ) -> None:
        self.n_splits = n_splits
        self.min_train_size = min_train_size

    def validate(
        self,
        X: np.ndarray,
        y: np.ndarray,
        train_fn: Callable[[np.ndarray, np.ndarray], Any],
        test_fn: Callable[[Any, np.ndarray, np.ndarray], dict],
    ) -> Generator[dict, None, None]:
        """Run walk-forward validation, yielding per-window results.

        Args:
            X: Feature matrix shape ``(n_samples, n_features)``.
            y: Labels array shape ``(n_samples,)``.
            train_fn: ``(X_train, y_train) -> model``. Trains and returns model.
            test_fn: ``(model, X_test, y_test) -> dict``. Evaluates and returns
                metrics dict.

        Yields:
            Dict with keys:
                - ``window``: int (1-indexed window number)
                - ``train_size``: int
                - ``test_size``: int
                - ``train_end_idx``: int (last index in training set)
                - ``test_start_idx``: int (first index in test set)
                - ``metrics``: dict from test_fn
        """
        n = len(X)
        # Calculate test window size
        test_size = max(1, (n - self.min_train_size) // self.n_splits)
        # First split point: leave enough room for n_splits test windows
        first_split = n - self.n_splits * test_size

        if first_split < self.min_train_size:
            first_split = self.min_train_size
            test_size = max(1, (n - first_split) // self.n_splits)

        for i in range(self.n_splits):
            train_end = first_split + i * test_size
            test_start = train_end
            test_end = min(train_end + test_size, n)

            if test_start >= n:
                break

            X_train, y_train = X[:train_end], y[:train_end]
            X_test, y_test = X[test_start:test_end], y[test_start:test_end]

            if len(X_test) == 0:
                break

            logger.debug(
                "Walk-forward window %d: train[:%d] test[%d:%d]",
                i + 1, train_end, test_start, test_end,
            )

            model = train_fn(X_train, y_train)
            metrics = test_fn(model, X_test, y_test)

            yield {
                "window": i + 1,
                "train_size": len(X_train),
                "test_size": len(X_test),
                "train_end_idx": train_end - 1,
                "test_start_idx": test_start,
                "metrics": metrics,
            }
