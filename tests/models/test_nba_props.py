"""Tests for src/models/nba_props.py — Player prop projections."""
import pytest
import numpy as np


def test_predict_raises_model_not_found_for_all_prop_types(tmp_path):
    """predict() raises ModelNotFoundError for all prop types when untrained."""
    from src.models.nba_props import NBAPropsModel
    from src.models.exceptions import ModelNotFoundError

    model = NBAPropsModel(store_dir=str(tmp_path))
    features = np.zeros((1, 7))
    for prop_type in ("pts", "reb", "ast", "3pm"):
        with pytest.raises(ModelNotFoundError):
            model.predict(features, prop_type=prop_type)


def test_predict_points_prop(tmp_path):
    """predict() for 'pts' prop returns P(pts > line) in [0.0, 1.0]."""
    from src.models.nba_props import NBAPropsModel
    from src.models.model_store import ModelStore
    from sklearn.ensemble import GradientBoostingClassifier
    from sklearn.calibration import CalibratedClassifierCV

    # Train and save a tiny model for pts
    rng = np.random.RandomState(42)
    X = rng.randn(200, 7)
    y = (X[:, 0] > 0).astype(int)
    gbm = GradientBoostingClassifier(n_estimators=10, max_depth=2, random_state=42)
    cal = CalibratedClassifierCV(gbm, method="sigmoid", cv=3)
    cal.fit(X, y)

    store = ModelStore("nba_props_pts", store_dir=str(tmp_path))
    store.save(cal, calibrator=None, metrics={"brier": 0.23}, training_data_hash="test")

    model = NBAPropsModel(store_dir=str(tmp_path))
    features = np.array([[22.3, 20.1, 24.5, 0.8, 35.0, 85.0, 1]])
    prob = model.predict(features, prop_type="pts")
    assert 0.0 <= prob[0] <= 1.0


def test_predict_supports_all_prop_types(tmp_path):
    """predict() accepts prop_type in ('pts', 'reb', 'ast', '3pm')."""
    from src.models.nba_props import NBAPropsModel
    from src.models.model_store import ModelStore
    from sklearn.ensemble import GradientBoostingClassifier
    from sklearn.calibration import CalibratedClassifierCV

    rng = np.random.RandomState(42)
    X = rng.randn(200, 7)
    y = (X[:, 0] > 0).astype(int)

    # Save a model for each prop type
    for pt in ("pts", "reb", "ast", "3pm"):
        gbm = GradientBoostingClassifier(n_estimators=10, max_depth=2, random_state=42)
        cal = CalibratedClassifierCV(gbm, method="sigmoid", cv=3)
        cal.fit(X, y)
        store = ModelStore(f"nba_props_{pt}", store_dir=str(tmp_path))
        store.save(cal, calibrator=None, metrics={}, training_data_hash="test")

    model = NBAPropsModel(store_dir=str(tmp_path))
    features = np.zeros((1, 7))
    for pt in ("pts", "reb", "ast", "3pm"):
        prob = model.predict(features, prop_type=pt)
        assert 0.0 <= prob[0] <= 1.0


def test_predict_invalid_prop_type_raises_value_error(tmp_path):
    """predict() raises ValueError for invalid prop_type."""
    from src.models.nba_props import NBAPropsModel

    model = NBAPropsModel(store_dir=str(tmp_path))
    with pytest.raises(ValueError, match="prop_type must be one of"):
        model.predict(np.zeros((1, 7)), prop_type="invalid")
