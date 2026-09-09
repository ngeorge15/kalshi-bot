"""Temporal train/test/holdout splitter with holdout write-protection.

Splits data chronologically (no shuffling) to prevent future leakage.
Default ratio: 60% train, 20% test, 20% holdout.

The holdout set is NEVER returned by :meth:`TemporalSplitter.split`.  Reaching
it requires :meth:`TemporalSplitter.holdout` (data) or
:meth:`TemporalSplitter.access_holdout` (indices) with an explicit
``allow_holdout=True``, which is logged.

An earlier version documented that protection while ``split()`` returned the
holdout arrays unconditionally, so the gate could be bypassed simply by using
the ordinary API — and the tests covering the gate never exercised that path.
``split()`` now returns train and test only.

Usage::

    from src.validation.splitter import TemporalSplitter
    import pandas as pd, numpy as np

    dates = pd.date_range("2022-01-01", periods=100, freq="D")
    X = np.random.randn(100, 5)
    y = np.random.randint(0, 2, 100)

    splitter = TemporalSplitter(dates)
    (X_train, X_test), (y_train, y_test) = splitter.split(X, y)

    # Final pre-deployment validation only:
    X_holdout, y_holdout = splitter.holdout(X, y, allow_holdout=True)
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
    ) -> tuple[tuple[np.ndarray, np.ndarray], tuple[np.ndarray, np.ndarray]]:
        """Split X and y into train and test by temporal order.

        The holdout tail is deliberately **not** returned.  Handing it back here
        would make the write-protection on :meth:`holdout` decorative, since no
        caller would ever need to ask for it.

        Args:
            X: Feature matrix shape ``(n_samples, n_features)``.
            y: Labels array shape ``(n_samples,)``.

        Returns:
            ``((X_train, X_test), (y_train, y_test))``.
        """
        X_sorted, y_sorted = self._sorted(X, y)
        X_train, X_test = X_sorted[:self._train_end], X_sorted[self._train_end:self._test_end]
        y_train, y_test = y_sorted[:self._train_end], y_sorted[self._train_end:self._test_end]
        logger.info(
            "Temporal split: train=%d, test=%d, holdout=%d withheld",
            len(X_train), len(X_test), len(X_sorted) - self._test_end,
        )
        return (X_train, X_test), (y_train, y_test)

    def _sorted(self, X: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Return X and y reordered by ascending date."""
        sort_idx = np.argsort(self.dates)
        return X[sort_idx], y[sort_idx]

    def holdout(
        self, X: np.ndarray, y: np.ndarray, allow_holdout: bool = False,
    ) -> tuple[np.ndarray, np.ndarray]:
        """Return the holdout data, gated behind an explicit flag.

        Args:
            X: Feature matrix.
            y: Labels array.
            allow_holdout: Must be explicitly ``True``.

        Returns:
            ``(X_holdout, y_holdout)``.

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
        X_sorted, y_sorted = self._sorted(X, y)
        return X_sorted[self._test_end:], y_sorted[self._test_end:]

    @property
    def holdout_accessed(self) -> bool:
        """Whether the holdout has been reached in this splitter's lifetime.

        Lets a training routine assert it never touched the holdout, and lets a
        provenance record state so.
        """
        return self._holdout_accessed

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
