"""Unit tests for src/data/nba/teams.py.

Tests are fully offline — nba_api endpoint classes are mocked at the
``get_normalized_dict()`` level so no network calls are made.
"""

import logging
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from src.data.nba.teams import (
    EloTracker,
    get_rest_days,
    get_team_stats,
    get_todays_schedule,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_TEAM_STATS = [
    {
        "TEAM_ID": 1610612737,
        "TEAM_NAME": "Atlanta Hawks",
        "OFF_RATING": 112.5,
        "DEF_RATING": 115.0,
        "NET_RATING": -2.5,
        "PACE": 100.1,
        "W": 20,
        "L": 30,
        "W_PCT": 0.4,
    },
    {
        "TEAM_ID": 1610612738,
        "TEAM_NAME": "Boston Celtics",
        "OFF_RATING": 120.0,
        "DEF_RATING": 108.5,
        "NET_RATING": 11.5,
        "PACE": 98.7,
        "W": 40,
        "L": 10,
        "W_PCT": 0.8,
    },
]

SAMPLE_SCHEDULE = [
    {
        "GAME_ID": "0022300001",
        "HOME_TEAM_ID": 1610612737,
        "VISITOR_TEAM_ID": 1610612738,
        "GAME_DATE_EST": "2024-11-01T00:00:00",
    },
    {
        "GAME_ID": "0022300002",
        "HOME_TEAM_ID": 1610612739,
        "VISITOR_TEAM_ID": 1610612740,
        "GAME_DATE_EST": "2024-11-01T00:00:00",
    },
]

# Historical schedule used for rest_days calculation
HISTORICAL_SCHEDULE = [
    {
        "GAME_ID": "0022300100",
        "HOME_TEAM_ID": 1610612737,
        "VISITOR_TEAM_ID": 1610612738,
        "GAME_DATE_EST": "2024-10-30T00:00:00",
    },
    {
        "GAME_ID": "0022300101",
        "HOME_TEAM_ID": 1610612739,
        "VISITOR_TEAM_ID": 1610612737,
        "GAME_DATE_EST": "2024-10-29T00:00:00",
    },
]


# ---------------------------------------------------------------------------
# get_team_stats tests
# ---------------------------------------------------------------------------


def test_get_team_stats_returns_required_keys(tmp_path, monkeypatch):
    """get_team_stats() returns list of dicts with required keys."""
    import src.data.cache as cache_mod
    monkeypatch.setattr(cache_mod, "CACHE_DIR", tmp_path / "cache")

    mock_endpoint = MagicMock()
    mock_endpoint.return_value.get_normalized_dict.return_value = {
        "LeagueDashTeamStats": SAMPLE_TEAM_STATS
    }

    with patch(
        "src.data.nba.teams.LeagueDashTeamStats", mock_endpoint
    ), patch("src.data.nba.teams.time") as mock_time:
        mock_time.sleep = MagicMock()
        result = get_team_stats()

    assert isinstance(result, list)
    assert len(result) == 2
    required_keys = {"TEAM_ID", "TEAM_NAME", "OFF_RATING", "DEF_RATING", "NET_RATING", "PACE", "W", "L", "W_PCT"}
    for team in result:
        assert required_keys.issubset(team.keys()), f"Missing keys: {required_keys - team.keys()}"


def test_get_team_stats_uses_cache_on_second_call(tmp_path, monkeypatch):
    """Second call with valid TTL does NOT call nba_api again."""
    import src.data.cache as cache_mod
    monkeypatch.setattr(cache_mod, "CACHE_DIR", tmp_path / "cache")

    mock_endpoint = MagicMock()
    mock_endpoint.return_value.get_normalized_dict.return_value = {
        "LeagueDashTeamStats": SAMPLE_TEAM_STATS
    }

    with patch(
        "src.data.nba.teams.LeagueDashTeamStats", mock_endpoint
    ), patch("src.data.nba.teams.time") as mock_time:
        mock_time.sleep = MagicMock()
        get_team_stats()  # First call — populates cache
        get_team_stats()  # Second call — should use cache

    # LeagueDashTeamStats should only be instantiated once
    assert mock_endpoint.call_count == 1


def test_get_team_stats_stale_cache_on_api_failure(tmp_path, monkeypatch, caplog):
    """On API failure, get_team_stats() returns stale cache and logs warning."""
    import src.data.cache as cache_mod
    monkeypatch.setattr(cache_mod, "CACHE_DIR", tmp_path / "cache")

    # First call: prime the cache
    mock_endpoint = MagicMock()
    mock_endpoint.return_value.get_normalized_dict.return_value = {
        "LeagueDashTeamStats": SAMPLE_TEAM_STATS
    }
    with patch(
        "src.data.nba.teams.LeagueDashTeamStats", mock_endpoint
    ), patch("src.data.nba.teams.time") as mock_time:
        mock_time.sleep = MagicMock()
        get_team_stats()

    # Second call: API raises exception — should serve stale cache
    failing_endpoint = MagicMock()
    failing_endpoint.return_value.get_normalized_dict.side_effect = RuntimeError("API down")
    with caplog.at_level(logging.WARNING, logger="src.data.nba.teams"), patch(
        "src.data.nba.teams.LeagueDashTeamStats", failing_endpoint
    ), patch("src.data.nba.teams.time") as mock_time:
        mock_time.sleep = MagicMock()
        # Force cache expiry by using a very short TTL — but we need to bypass TTL
        # instead we patch cache_get to return None (simulating expired)
        with patch("src.data.nba.teams.cache_get", return_value=None):
            result = get_team_stats()

    assert result is not None
    assert "warning" in caplog.text.lower() or len(caplog.records) > 0


# ---------------------------------------------------------------------------
# get_todays_schedule tests
# ---------------------------------------------------------------------------


def test_get_todays_schedule_returns_required_keys(tmp_path, monkeypatch):
    """get_todays_schedule() returns list of dicts with expected keys."""
    import src.data.cache as cache_mod
    monkeypatch.setattr(cache_mod, "CACHE_DIR", tmp_path / "cache")

    mock_endpoint = MagicMock()
    mock_endpoint.return_value.get_normalized_dict.return_value = {
        "GameHeader": SAMPLE_SCHEDULE
    }

    with patch(
        "src.data.nba.teams.ScoreboardV3", mock_endpoint
    ), patch("src.data.nba.teams.time") as mock_time:
        mock_time.sleep = MagicMock()
        result = get_todays_schedule(game_date=date(2024, 11, 1))

    assert isinstance(result, list)
    assert len(result) == 2
    required_keys = {"GAME_ID", "HOME_TEAM_ID", "VISITOR_TEAM_ID", "GAME_DATE_EST"}
    for game in result:
        assert required_keys.issubset(game.keys()), f"Missing keys: {required_keys - game.keys()}"


# ---------------------------------------------------------------------------
# get_rest_days tests
# ---------------------------------------------------------------------------


def test_get_rest_days_back_to_back():
    """get_rest_days returns 0 or 1 for a back-to-back scenario."""
    # Team 1610612737 played yesterday (Oct 30), today is Oct 31
    rest = get_rest_days(
        team_id=1610612737,
        schedule=HISTORICAL_SCHEDULE,
        reference_date=date(2024, 10, 31),
    )
    # Rest = 0 (played day before reference = back-to-back)
    assert rest == 0


def test_get_rest_days_no_previous_game():
    """get_rest_days returns -1 when no previous game found."""
    rest = get_rest_days(
        team_id=9999999,  # Unknown team
        schedule=HISTORICAL_SCHEDULE,
        reference_date=date(2024, 10, 31),
    )
    assert rest == -1


def test_get_rest_days_returns_correct_days():
    """get_rest_days returns correct number of days since last game."""
    # Team 1610612737 played Oct 30, reference date is Nov 3 => 4 days rest
    rest = get_rest_days(
        team_id=1610612737,
        schedule=HISTORICAL_SCHEDULE,
        reference_date=date(2024, 11, 3),
    )
    assert rest == 4


# ---------------------------------------------------------------------------
# EloTracker tests
# ---------------------------------------------------------------------------


def test_elo_tracker_initial_elo():
    """EloTracker.get_elo returns INITIAL_ELO (1300) for unknown team."""
    tracker = EloTracker()
    assert tracker.get_elo(99999) == EloTracker.INITIAL_ELO
    assert EloTracker.INITIAL_ELO == 1300


def test_elo_tracker_update_winner_increases_loser_decreases():
    """EloTracker.update: winner ELO increases, loser ELO decreases."""
    tracker = EloTracker()
    winner_id = 1
    loser_id = 2
    initial_winner = tracker.get_elo(winner_id)
    initial_loser = tracker.get_elo(loser_id)

    tracker.update(winner_id=winner_id, loser_id=loser_id, margin=10)

    assert tracker.get_elo(winner_id) > initial_winner
    assert tracker.get_elo(loser_id) < initial_loser


def test_elo_tracker_expected_outcome_equal_elos():
    """EloTracker.expected_outcome returns ~0.5 for equal ELOs."""
    tracker = EloTracker()
    result = tracker.expected_outcome(1300.0, 1300.0)
    assert abs(result - 0.5) < 0.001


def test_elo_tracker_expected_outcome_higher_elo_wins():
    """EloTracker.expected_outcome returns >0.5 when team A is higher rated."""
    tracker = EloTracker()
    result = tracker.expected_outcome(1500.0, 1300.0)
    assert result > 0.5


def test_elo_tracker_season_reset():
    """EloTracker.season_reset pulls ratings toward 1300 by 1/3."""
    tracker = EloTracker()
    # Set a team's elo to 1600 (300 above baseline)
    tracker._ratings[1] = 1600.0
    tracker.season_reset()
    # Expected: 1600 - (1600 - 1300) * 0.33 = 1600 - 99 = 1501
    expected = 1600 - (1600 - 1300) * EloTracker.SEASON_RESET_FRACTION
    assert abs(tracker.get_elo(1) - expected) < 0.01


def test_elo_tracker_get_all_ratings_returns_copy():
    """EloTracker.get_all_ratings returns a copy of the ratings dict."""
    tracker = EloTracker()
    tracker.update(winner_id=1, loser_id=2, margin=5)
    ratings = tracker.get_all_ratings()
    assert isinstance(ratings, dict)
    assert len(ratings) == 2
    # Verify it's a copy — mutating it doesn't affect tracker
    ratings[1] = 9999.0
    assert tracker.get_elo(1) != 9999.0
