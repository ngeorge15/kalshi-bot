"""Tests for src/models/nba_game.py — NBA game moneyline/spread model.

Wave 0: All tests marked xfail(strict=True). They will turn green when
nba_game.py is implemented in plan 03-01.
"""
import pytest
import numpy as np


@pytest.mark.xfail(strict=True, reason="nba_game.py not implemented yet")
def test_predict_returns_calibrated_probability():
    """predict() returns float in [0.0, 1.0] for a valid feature vector."""
    from src.models.nba_game import nba_game_model
    features = np.array([[1500.0, 1480.0, 1, 2, 0, 0.05, 0.03]])  # home_elo, away_elo, home_court, rest_home, rest_away, inj_home, inj_away
    prob = nba_game_model.predict(features)
    assert 0.0 <= prob[0] <= 1.0


@pytest.mark.xfail(strict=True, reason="nba_game.py not implemented yet")
def test_predict_raises_model_not_found_when_untrained():
    """predict() raises ModelNotFoundError when no trained model exists."""
    from src.models.nba_game import NBAGameModel
    from src.models.exceptions import ModelNotFoundError
    model = NBAGameModel()
    with pytest.raises(ModelNotFoundError):
        model.predict(np.zeros((1, 7)))


@pytest.mark.xfail(strict=True, reason="nba_game.py not implemented yet")
def test_predict_moneyline_and_spread():
    """predict() with context='moneyline' and context='spread' both return valid probs."""
    from src.models.nba_game import nba_game_model
    features = np.array([[1500.0, 1480.0, 1, 2, 0, 0.05, 0.03]])
    prob_ml = nba_game_model.predict(features, context="moneyline")
    prob_sp = nba_game_model.predict(features, context="spread")
    assert 0.0 <= prob_ml[0] <= 1.0
    assert 0.0 <= prob_sp[0] <= 1.0
