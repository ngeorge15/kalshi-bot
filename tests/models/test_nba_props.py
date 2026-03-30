"""Tests for src/models/nba_props.py — Player prop projections.

Wave 0: All tests marked xfail(strict=True).
"""
import pytest
import numpy as np


@pytest.mark.xfail(strict=True, reason="nba_props.py not implemented yet")
def test_predict_points_prop():
    """predict() for 'pts' prop returns P(pts > line) in [0.0, 1.0]."""
    from src.models.nba_props import nba_props_model
    features = np.array([[22.3, 20.1, 24.5, 0.8, 35.0, 85.0, 1]])  # season_avg, last5_avg, last5_max, opp_def_rating_vs_pos, minutes_proj, line*10, home
    prob = nba_props_model.predict(features, prop_type="pts")
    assert 0.0 <= prob[0] <= 1.0


@pytest.mark.xfail(strict=True, reason="nba_props.py not implemented yet")
def test_predict_supports_all_prop_types():
    """predict() accepts prop_type in ('pts', 'reb', 'ast', '3pm')."""
    from src.models.nba_props import nba_props_model
    from src.models.exceptions import ModelNotFoundError
    features = np.zeros((1, 7))
    for prop_type in ("pts", "reb", "ast", "3pm"):
        with pytest.raises(ModelNotFoundError):
            nba_props_model.predict(features, prop_type=prop_type)
