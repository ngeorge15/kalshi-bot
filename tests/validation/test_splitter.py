"""Tests for src/validation/splitter.py — Train/test/holdout temporal splitter."""
import pytest
import numpy as np
import pandas as pd


def test_split_preserves_temporal_ordering():
    """Holdout contains only the most recent observations (no future leakage)."""
    from src.validation.splitter import TemporalSplitter

    n = 100
    dates = pd.date_range("2022-01-01", periods=n, freq="D")
    X = np.random.randn(n, 3)
    y = np.random.randint(0, 2, n)
    splitter = TemporalSplitter(dates)
    (X_train, X_test), (y_train, y_test) = splitter.split(X, y)
    # Strict temporal ordering: train < test < holdout
    train_dates = dates[:len(X_train)]
    test_dates = dates[len(X_train):len(X_train) + len(X_test)]
    holdout_dates = dates[len(X_train) + len(X_test):]
    assert train_dates.max() < test_dates.min()
    assert test_dates.max() < holdout_dates.min()


def test_split_ratio_60_20_20():
    """Default split produces 60% train, 20% test, 20% holdout."""
    from src.validation.splitter import TemporalSplitter

    n = 100
    dates = pd.date_range("2022-01-01", periods=n, freq="D")
    X = np.random.randn(n, 3)
    y = np.random.randint(0, 2, n)
    splitter = TemporalSplitter(dates)
    (X_train, X_test), _ = splitter.split(X, y)
    X_holdout, _y = splitter.holdout(X, y, allow_holdout=True)
    assert len(X_train) == 60
    assert len(X_test) == 20
    assert len(X_holdout) == 20


def test_holdout_write_protected():
    """Holdout indices are exposed; calling access_holdout() without flag raises."""
    from src.validation.splitter import TemporalSplitter

    n = 100
    dates = pd.date_range("2022-01-01", periods=n, freq="D")
    splitter = TemporalSplitter(dates)
    with pytest.raises(PermissionError):
        splitter.access_holdout()  # Must raise without explicit allow_holdout=True


def test_split_does_not_return_holdout():
    """The bypass this module previously had: split() handing back the holdout.

    The gate on holdout access is only meaningful if the ordinary API cannot
    reach the data without it.
    """
    from src.validation.splitter import TemporalSplitter

    n = 100
    dates = pd.date_range("2022-01-01", periods=n, freq="D")
    X, y = np.random.randn(n, 3), np.random.randint(0, 2, n)
    splitter = TemporalSplitter(dates)
    features, labels = splitter.split(X, y)
    assert len(features) == 2 and len(labels) == 2
    assert len(features[0]) + len(features[1]) == 80  # holdout tail withheld


def test_holdout_data_requires_explicit_flag():
    from src.validation.splitter import TemporalSplitter

    n = 100
    dates = pd.date_range("2022-01-01", periods=n, freq="D")
    X, y = np.random.randn(n, 3), np.random.randint(0, 2, n)
    splitter = TemporalSplitter(dates)
    with pytest.raises(PermissionError):
        splitter.holdout(X, y)


def test_holdout_access_is_recorded():
    """A training routine can assert it never reached the holdout."""
    from src.validation.splitter import TemporalSplitter

    n = 100
    dates = pd.date_range("2022-01-01", periods=n, freq="D")
    X, y = np.random.randn(n, 3), np.random.randint(0, 2, n)
    splitter = TemporalSplitter(dates)
    splitter.split(X, y)
    assert splitter.holdout_accessed is False
    splitter.holdout(X, y, allow_holdout=True)
    assert splitter.holdout_accessed is True


def test_holdout_is_the_most_recent_data():
    """Whatever the gate, the holdout must still be the temporal tail."""
    from src.validation.splitter import TemporalSplitter

    n = 100
    dates = pd.date_range("2022-01-01", periods=n, freq="D")
    X = np.arange(n).reshape(n, 1).astype(float)
    y = np.random.randint(0, 2, n)
    splitter = TemporalSplitter(dates)
    X_holdout, _ = splitter.holdout(X, y, allow_holdout=True)
    assert X_holdout.min() == 80.0
