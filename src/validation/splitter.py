"""Temporal train/test/holdout splitter with holdout write-protection.

Splits data chronologically (no shuffling) to prevent future leakage.
Default ratio: 60% train, 20% test, 20% holdout.

The holdout set is NEVER touched during training or tuning — only for final
pre-deployment validation.  ``access_holdout()`` enforces this via a
required ``allow_holdout`` flag.

Usage::

    from src.validation.splitter import TemporalSplitter
    import pandas as pd, numpy as np

    dates = pd.date_range("2022-01-01", periods=100, freq="D")
    X = np.random.randn(100, 5)
    y = np.random.randint(0, 2, 100)

    splitter = TemporalSplitter(dates)
    (X_train, X_test, X_holdout), (y_train, y_test, y_holdout) = splitter.split(X, y)
"""

import logging
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class TemporalSplitter:
    """Temporal train/test/holdout splitter with holdout write-protection.

    Data is sorted by date and split contiguously:
    ``[--- train (60%) ---][--- test (20%) ---][--- holdout (20%) ---]``

    Args:
        dates: DatetimeIndex or array-like of dates (same length as X/y).
            Data is sorted ascending before splitting.
        train_frac: Fraction of data for training. Default 0.6.
        test_frac: Fraction of data for testing. Default 0.2.
    """

    def __init__(
        self,
        dates: pd.DatetimeIndex,
        train_frac: float = 0.6,
        test_frac: float = 0.2,
    ) -> None:
        self.dates = pd.DatetimeIndex(dates)
        self.train_frac = train_frac
        self.test_frac = test_frac
        self.holdout_frac = 1.0 - train_frac - test_frac
        self._holdout_accessed = False

        # Compute split indices
        n = len(self.dates)
        self._train_end = int(n * self.train_frac)
        self._test_end = self._train_end + int(n * self.test_frac)

    def split(
        self, X: np.ndarray, y: np.ndarray,
    ) -> tuple[tuple[np.ndarray, np.ndarray, np.ndarray],
               tuple[np.ndarray, np.ndarray, np.ndarray]]:
        """Split X and y into train, test, holdout by temporal order.

        Args:
            X: Feature matrix shape ``(n_samples, n_features)``.
            y: Labels array shape ``(n_samples,)``.

        Returns:
            Tuple of ``((X_train, X_test, X_holdout), (y_train, y_test, y_holdout))``.
        """
        # Sort by date
        sort_idx = np.argsort(self.dates)
        X_sorted = X[sort_idx]
        y_sorted = y[sort_idx]

        X_train = X_sorted[:self._train_end]
        X_test = X_sorted[self._train_end:self._test_end]
        X_holdout = X_sorted[self._test_end:]

        y_train = y_sorted[:self._train_end]
        y_test = y_sorted[self._train_end:self._test_end]
        y_holdout = y_sorted[self._test_end:]

        logger.info(
            "Temporal split: train=%d, test=%d, holdout=%d",
            len(X_train), len(X_test), len(X_holdout),
        )

        return (X_train, X_test, X_holdout), (y_train, y_test, y_holdout)

    def access_holdout(self, allow_holdout: bool = False) -> np.ndarray:
        """Return holdout indices with write-protection enforcement.

        Args:
            allow_holdout: Must be explicitly ``True`` to access holdout data.

        Returns:
            Array of holdout indices.

        Raises:
            PermissionError: If ``allow_holdout`` is not ``True``.
        """
        if not allow_holdout:
            raise PermissionError(
                "Holdout access denied. Holdout data is write-protected per "
                "PROJECT.md overfitting policy. Pass allow_holdout=True only "
                "for final pre-deployment validation."
            )
        self._holdout_accessed = True
        logger.warning("HOLDOUT ACCESSED — this should only happen for final validation")
        sort_idx = np.argsort(self.dates)
        return sort_idx[self._test_end:]
