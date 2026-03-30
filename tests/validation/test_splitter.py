"""Tests for src/validation/splitter.py — Train/test/holdout temporal splitter.

Wave 0: All tests marked xfail(strict=True).
"""
import pytest
import numpy as np
import pandas as pd


@pytest.mark.xfail(strict=True, reason="splitter.py not implemented yet")
def test_split_preserves_temporal_ordering():
    """Holdout contains only the most recent observations (no future leakage)."""
    from src.validation.splitter import TemporalSplitter
    n = 100
    dates = pd.date_range("2022-01-01", periods=n, freq="D")
    X = np.random.randn(n, 3)
    y = np.random.randint(0, 2, n)
    splitter = TemporalSplitter(dates)
    (X_train, X_test, X_holdout), (y_train, y_test, y_holdout) = splitter.split(X, y)
    # Strict temporal ordering: train < test < holdout
    train_dates = dates[:len(X_train)]
    test_dates = dates[len(X_train):len(X_train) + len(X_test)]
    holdout_dates = dates[len(X_train) + len(X_test):]
    assert train_dates.max() < test_dates.min()
    assert test_dates.max() < holdout_dates.min()


@pytest.mark.xfail(strict=True, reason="splitter.py not implemented yet")
def test_split_ratio_60_20_20():
    """Default split produces 60% train, 20% test, 20% holdout."""
    from src.validation.splitter import TemporalSplitter
    n = 100
    dates = pd.date_range("2022-01-01", periods=n, freq="D")
    X = np.random.randn(n, 3)
    y = np.random.randint(0, 2, n)
    splitter = TemporalSplitter(dates)
    (X_train, X_test, X_holdout), _ = splitter.split(X, y)
    assert len(X_train) == 60
    assert len(X_test) == 20
    assert len(X_holdout) == 20


@pytest.mark.xfail(strict=True, reason="splitter.py not implemented yet")
def test_holdout_write_protected():
    """Holdout indices are exposed; calling access_holdout() without flag raises."""
    from src.validation.splitter import TemporalSplitter
    n = 100
    dates = pd.date_range("2022-01-01", periods=n, freq="D")
    splitter = TemporalSplitter(dates)
    with pytest.raises(PermissionError):
        splitter.access_holdout()  # Must raise without explicit allow_holdout=True
