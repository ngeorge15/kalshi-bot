"""Tests for src/models/nba_game.py — NBA game moneyline/spread model."""
import pytest
import numpy as np


def test_predict_raises_model_not_found_when_untrained(tmp_path):
    """predict() raises ModelNotFoundError when no trained model exists."""
    from src.models.nba_game import NBAGameModel
    from src.models.exceptions import ModelNotFoundError

    model = NBAGameModel(store_dir=str(tmp_path))
    with pytest.raises(ModelNotFoundError):
        model.predict(np.zeros((1, 7)))


def test_predict_returns_calibrated_probability(tmp_path):
    """predict() returns float in [0.0, 1.0] for a valid feature vector."""
    from src.models.nba_game import NBAGameModel
    from sklearn.ensemble import GradientBoostingClassifier
    from sklearn.calibration import CalibratedClassifierCV

    # Train a tiny model and save it
    model = NBAGameModel(store_dir=str(tmp_path))
    rng = np.random.RandomState(42)
    X = rng.randn(200, 7)
    y = (X[:, 0] > 0).astype(int)
    gbm = GradientBoostingClassifier(n_estimators=10, max_depth=2, random_state=42)
    cal = CalibratedClassifierCV(gbm, method="sigmoid", cv=3)
    cal.fit(X, y)
    model.store.save(cal, calibrator=None, metrics={"brier": 0.22}, training_data_hash="test")

    features = np.array([[1500.0, 1480.0, 1, 2, 0, 0.05, 0.03]])
    prob = model.predict(features)
    assert 0.0 <= prob[0] <= 1.0


def test_predict_moneyline_and_spread(tmp_path):
    """predict() with context='moneyline' and context='spread' both return valid probs."""
    from src.models.nba_game import NBAGameModel
    from sklearn.ensemble import GradientBoostingClassifier
    from sklearn.calibration import CalibratedClassifierCV

    model = NBAGameModel(store_dir=str(tmp_path))
    rng = np.random.RandomState(42)
    X = rng.randn(200, 7)
    y = (X[:, 0] > 0).astype(int)
    gbm = GradientBoostingClassifier(n_estimators=10, max_depth=2, random_state=42)
    cal = CalibratedClassifierCV(gbm, method="sigmoid", cv=3)
    cal.fit(X, y)
    model.store.save(cal, calibrator=None, metrics={}, training_data_hash="test")

    features = np.array([[1500.0, 1480.0, 1, 2, 0, 0.05, 0.03]])
    prob_ml = model.predict(features, context="moneyline")
    prob_sp = model.predict(features, context="spread")
    assert 0.0 <= prob_ml[0] <= 1.0
    assert 0.0 <= prob_sp[0] <= 1.0
