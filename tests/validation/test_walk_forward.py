"""Tests for src/validation/walk_forward.py — Rolling window validator.

Wave 0: All tests marked xfail(strict=True).
"""
import pytest
import numpy as np


@pytest.mark.xfail(strict=True, reason="walk_forward.py not implemented yet")
def test_walk_forward_yields_per_window_metrics():
    """validate() yields one metrics dict per window with required keys."""
    from src.validation.walk_forward import WalkForwardValidator
    from sklearn.linear_model import LogisticRegression
    n = 200
    X = np.random.randn(n, 3)
    y = (X[:, 0] > 0).astype(int)

    def train_fn(X_tr, y_tr):
        return LogisticRegression().fit(X_tr, y_tr)

    def test_fn(model, X_te, y_te):
        probs = model.predict_proba(X_te)[:, 1]
        brier = float(np.mean((probs - y_te) ** 2))
        return {"brier_score": brier}

    validator = WalkForwardValidator(n_splits=3)
    results = list(validator.validate(X, y, train_fn, test_fn))
    assert len(results) == 3
    for r in results:
        assert "window" in r
        assert "brier_score" in r["metrics"]
        assert "train_size" in r
        assert "test_size" in r


@pytest.mark.xfail(strict=True, reason="walk_forward.py not implemented yet")
def test_walk_forward_no_future_leakage():
    """Each window's test indices are strictly after its train indices."""
    from src.validation.walk_forward import WalkForwardValidator
    n = 100
    X = np.arange(n).reshape(-1, 1).astype(float)
    y = np.zeros(n, dtype=int)
    train_ends = []
    test_starts = []

    def train_fn(X_tr, y_tr):
        train_ends.append(int(X_tr.max()))
        return None

    def test_fn(model, X_te, y_te):
        test_starts.append(int(X_te.min()))
        return {}

    validator = WalkForwardValidator(n_splits=3)
    list(validator.validate(X, y, train_fn, test_fn))
    for te, ts in zip(train_ends, test_starts):
        assert te < ts
