"""Tests for src/models/model_store.py — Versioning, persistence, rollback.

Wave 0: All tests marked xfail(strict=True).
"""
import pytest
import tempfile
import os


@pytest.mark.xfail(strict=True, reason="model_store.py not implemented yet")
def test_save_and_load_roundtrip(tmp_path):
    """save() then load_latest() returns the same model object."""
    from src.models.model_store import ModelStore
    store = ModelStore("test_model", store_dir=str(tmp_path))
    from sklearn.linear_model import LogisticRegression
    import numpy as np
    X = np.random.randn(20, 3)
    y = (X[:, 0] > 0).astype(int)
    model = LogisticRegression().fit(X, y)
    store.save(model, calibrator=None, metrics={"brier": 0.22}, training_data_hash="abc123")
    loaded_model, loaded_cal = store.load_latest()
    assert loaded_model is not None


@pytest.mark.xfail(strict=True, reason="model_store.py not implemented yet")
def test_rollback_restores_previous_version(tmp_path):
    """rollback() marks previous version as active."""
    from src.models.model_store import ModelStore
    store = ModelStore("test_model", store_dir=str(tmp_path))
    from sklearn.linear_model import LogisticRegression
    import numpy as np
    X = np.random.randn(20, 3)
    y = (X[:, 0] > 0).astype(int)
    for _ in range(2):
        model = LogisticRegression().fit(X, y)
        store.save(model, calibrator=None, metrics={}, training_data_hash="hash")
    store.rollback()
    active = store.get_active_version()
    assert active == 1


@pytest.mark.xfail(strict=True, reason="model_store.py not implemented yet")
def test_is_bootstrap_mode():
    """is_bootstrap_mode() returns True when OOS predictions < 50 (D-08, D-09)."""
    from src.models.model_store import ModelStore
    store = ModelStore("nba_game")
    # With no predictions recorded, should be bootstrap mode
    assert store.is_bootstrap_mode() is True
