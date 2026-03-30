"""Tests for src/models/nba_totals.py — NBA over/under pace-adjusted model.

Wave 0: All tests marked xfail(strict=True).
"""
import pytest
import numpy as np


@pytest.mark.xfail(strict=True, reason="nba_totals.py not implemented yet")
def test_predict_returns_over_probability():
    """predict() returns P(total > line) in [0.0, 1.0]."""
    from src.models.nba_totals import nba_totals_model
    features = np.array([[112.5, 108.0, 96.5, 100.0, 220.5]])  # home_ortg, away_ortg, home_pace, away_pace, line
    prob = nba_totals_model.predict(features)
    assert 0.0 <= prob[0] <= 1.0


@pytest.mark.xfail(strict=True, reason="nba_totals.py not implemented yet")
def test_predict_raises_model_not_found_when_untrained():
    """predict() raises ModelNotFoundError when no trained model exists."""
    from src.models.nba_totals import NBATotalsModel
    from src.models.exceptions import ModelNotFoundError
    model = NBATotalsModel()
    with pytest.raises(ModelNotFoundError):
        model.predict(np.zeros((1, 5)))
