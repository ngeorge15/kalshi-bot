"""Tests for src/models/nba_totals.py — NBA over/under pace-adjusted model."""
import pytest
import numpy as np


def test_predict_raises_model_not_found_when_untrained(tmp_path):
    """predict() raises ModelNotFoundError when no trained model exists."""
    from src.models.nba_totals import NBATotalsModel
    from src.models.exceptions import ModelNotFoundError

    model = NBATotalsModel(store_dir=str(tmp_path))
    with pytest.raises(ModelNotFoundError):
        model.predict(np.zeros((1, 5)))


def test_predict_returns_over_probability(tmp_path):
    """predict() returns P(total > line) in [0.0, 1.0]."""
    from src.models.nba_totals import NBATotalsModel
    from sklearn.ensemble import GradientBoostingClassifier
    from sklearn.calibration import CalibratedClassifierCV

    model = NBATotalsModel(store_dir=str(tmp_path))
    rng = np.random.RandomState(42)
    X = rng.randn(200, 5)
    y = (X[:, 0] > 0).astype(int)
    gbm = GradientBoostingClassifier(n_estimators=10, max_depth=2, random_state=42)
    cal = CalibratedClassifierCV(gbm, method="sigmoid", cv=3)
    cal.fit(X, y)
    model.store.save(cal, calibrator=None, metrics={"brier": 0.24}, training_data_hash="test")

    features = np.array([[112.5, 108.0, 96.5, 100.0, 220.5]])
    prob = model.predict(features)
    assert 0.0 <= prob[0] <= 1.0
