"""Tests for src/models/model_store.py — Versioning, persistence, rollback."""
import pytest
import numpy as np


def test_save_and_load_roundtrip(tmp_path):
    """save() then load_latest() returns the same model object."""
    from src.models.model_store import ModelStore
    from sklearn.linear_model import LogisticRegression

    store = ModelStore("test_model", store_dir=str(tmp_path))
    X = np.random.randn(20, 3)
    y = (X[:, 0] > 0).astype(int)
    model = LogisticRegression().fit(X, y)
    store.save(model, calibrator=None, metrics={"brier": 0.22}, training_data_hash="abc123")
    loaded_model, loaded_cal = store.load_latest()
    assert loaded_model is not None
    assert loaded_cal is None


def test_rollback_restores_previous_version(tmp_path):
    """rollback() marks previous version as active."""
    from src.models.model_store import ModelStore
    from sklearn.linear_model import LogisticRegression

    store = ModelStore("test_model", store_dir=str(tmp_path))
    X = np.random.randn(20, 3)
    y = (X[:, 0] > 0).astype(int)
    for _ in range(2):
        model = LogisticRegression().fit(X, y)
        store.save(model, calibrator=None, metrics={}, training_data_hash="hash")
    store.rollback()
    active = store.get_active_version()
    assert active == 1


def test_is_bootstrap_mode(tmp_path):
    """is_bootstrap_mode() returns True when no model exists (D-08, D-09)."""
    from src.models.model_store import ModelStore

    store = ModelStore("nba_game", store_dir=str(tmp_path))
    # With no versions at all, should be bootstrap mode
    assert store.is_bootstrap_mode() is True


def test_load_latest_raises_when_empty(tmp_path):
    """load_latest() raises ModelNotFoundError when no versions exist."""
    from src.models.model_store import ModelStore
    from src.models.exceptions import ModelNotFoundError

    store = ModelStore("empty_model", store_dir=str(tmp_path))
    with pytest.raises(ModelNotFoundError):
        store.load_latest()


def test_save_increments_version(tmp_path):
    """Each save() increments the version number."""
    from src.models.model_store import ModelStore
    from sklearn.linear_model import LogisticRegression

    store = ModelStore("test_model", store_dir=str(tmp_path))
    X = np.random.randn(20, 3)
    y = (X[:, 0] > 0).astype(int)
    model = LogisticRegression().fit(X, y)
    v1 = store.save(model, metrics={"brier": 0.25}, training_data_hash="h1")
    v2 = store.save(model, metrics={"brier": 0.23}, training_data_hash="h2")
    assert v1 == 1
    assert v2 == 2
    assert store.get_active_version() == 2
