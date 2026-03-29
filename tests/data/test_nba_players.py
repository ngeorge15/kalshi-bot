"""Unit tests for src/data/nba/players.py.

Tests are fully offline — nba_api endpoint classes and requests are mocked
so no network calls are made.
"""

import logging
from datetime import date
from unittest.mock import MagicMock, patch, call

import pytest

import src.data.cache as cache_mod
from src.data.nba.players import (
    _fetch_espn_injuries,
    _fetch_nba_api_injuries,
    get_injury_report,
    get_matchup_context,
    get_player_game_logs,
    get_player_stats,
    get_tonights_players,
)


# ---------------------------------------------------------------------------
# Sample data fixtures
# ---------------------------------------------------------------------------

SAMPLE_SCHEDULE = [
    {
        "GAME_ID": "0022300001",
        "HOME_TEAM_ID": 1610612737,
        "VISITOR_TEAM_ID": 1610612738,
        "GAME_DATE_EST": "2024-11-01T00:00:00",
    }
]

SAMPLE_ROSTER_HAWKS = [
    {"PLAYER_ID": 101, "PLAYER": "Trae Young", "POSITION": "G", "TeamID": 1610612737},
    {"PLAYER_ID": 102, "PLAYER": "Dejounte Murray", "POSITION": "G", "TeamID": 1610612737},
]

SAMPLE_ROSTER_CELTICS = [
    {"PLAYER_ID": 201, "PLAYER": "Jayson Tatum", "POSITION": "F", "TeamID": 1610612738},
    {"PLAYER_ID": 202, "PLAYER": "Jaylen Brown", "POSITION": "F", "TeamID": 1610612738},
]

SAMPLE_GAME_LOG = [
    {
        "GAME_DATE": "2024-10-31",
        "MATCHUP": "ATL vs. BOS",
        "WL": "L",
        "MIN": "35",
        "PTS": 32,
        "REB": 5,
        "AST": 11,
        "STL": 1,
        "BLK": 0,
        "TOV": 4,
        "FGM": 11,
        "FGA": 22,
        "FG3M": 3,
        "FG3A": 8,
        "FTM": 7,
        "FTA": 8,
        "PLUS_MINUS": -5,
    },
    {
        "GAME_DATE": "2024-10-28",
        "MATCHUP": "ATL @ MIA",
        "WL": "W",
        "MIN": "34",
        "PTS": 28,
        "REB": 4,
        "AST": 9,
        "STL": 2,
        "BLK": 0,
        "TOV": 3,
        "FGM": 9,
        "FGA": 19,
        "FG3M": 4,
        "FG3A": 9,
        "FTM": 6,
        "FTA": 7,
        "PLUS_MINUS": 8,
    },
    {
        "GAME_DATE": "2024-10-26",
        "MATCHUP": "ATL vs. DET",
        "WL": "W",
        "MIN": "32",
        "PTS": 30,
        "REB": 3,
        "AST": 12,
        "STL": 0,
        "BLK": 1,
        "TOV": 2,
        "FGM": 10,
        "FGA": 20,
        "FG3M": 4,
        "FG3A": 9,
        "FTM": 6,
        "FTA": 7,
        "PLUS_MINUS": 10,
    },
    {
        "GAME_DATE": "2024-10-24",
        "MATCHUP": "ATL @ CHI",
        "WL": "L",
        "MIN": "36",
        "PTS": 22,
        "REB": 5,
        "AST": 8,
        "STL": 1,
        "BLK": 0,
        "TOV": 5,
        "FGM": 7,
        "FGA": 18,
        "FG3M": 2,
        "FG3A": 7,
        "FTM": 6,
        "FTA": 7,
        "PLUS_MINUS": -8,
    },
    {
        "GAME_DATE": "2024-10-22",
        "MATCHUP": "ATL vs. NYK",
        "WL": "W",
        "MIN": "33",
        "PTS": 35,
        "REB": 4,
        "AST": 13,
        "STL": 2,
        "BLK": 0,
        "TOV": 3,
        "FGM": 12,
        "FGA": 23,
        "FG3M": 5,
        "FG3A": 11,
        "FTM": 6,
        "FTA": 7,
        "PLUS_MINUS": 15,
    },
    {
        "GAME_DATE": "2024-10-20",
        "MATCHUP": "ATL @ PHI",
        "WL": "L",
        "MIN": "34",
        "PTS": 20,
        "REB": 5,
        "AST": 7,
        "STL": 0,
        "BLK": 0,
        "TOV": 4,
        "FGM": 6,
        "FGA": 17,
        "FG3M": 2,
        "FG3A": 7,
        "FTM": 6,
        "FTA": 7,
        "PLUS_MINUS": -12,
    },
]

SAMPLE_ESPN_INJURIES_RESPONSE = {
    "injuries": [
        {
            "team": {"displayName": "Atlanta Hawks"},
            "injuries": [
                {
                    "athlete": {
                        "id": "101",
                        "displayName": "Trae Young",
                    },
                    "status": "Questionable",
                    "details": {"type": "Ankle"},
                }
            ],
        },
        {
            "team": {"displayName": "Boston Celtics"},
            "injuries": [
                {
                    "athlete": {
                        "id": "201",
                        "displayName": "Jayson Tatum",
                    },
                    "status": "Out",
                    "details": {"type": "Knee"},
                },
                {
                    "athlete": {
                        "id": "202",
                        "displayName": "Jaylen Brown",
                    },
                    "status": "Day-To-Day",
                    "details": {"type": "Hamstring"},
                },
            ],
        },
    ]
}

SAMPLE_MATCHUP_STATS = [
    {
        "TEAM_ID": 1610612738,
        "TEAM_NAME": "Boston Celtics",
        "D_FG_PCT": 0.44,
        "FREQ": 0.25,
        "D_FGA": 18.5,
    },
    {
        "TEAM_ID": 1610612737,
        "TEAM_NAME": "Atlanta Hawks",
        "D_FG_PCT": 0.47,
        "FREQ": 0.22,
        "D_FGA": 17.2,
    },
]


# ---------------------------------------------------------------------------
# Helper: patch cache dir
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    """Redirect cache writes to tmp_path for all tests."""
    monkeypatch.setattr(cache_mod, "CACHE_DIR", tmp_path / "cache")


# ---------------------------------------------------------------------------
# get_tonights_players tests
# ---------------------------------------------------------------------------


def test_get_tonights_players_calls_schedule_and_rosters():
    """get_tonights_players() calls get_todays_schedule, then CommonTeamRoster for each team."""
    mock_roster = MagicMock()
    mock_roster.return_value.get_normalized_dict.side_effect = [
        {"CommonTeamRoster": SAMPLE_ROSTER_HAWKS},
        {"CommonTeamRoster": SAMPLE_ROSTER_CELTICS},
    ]

    with patch("src.data.nba.players.get_todays_schedule", return_value=SAMPLE_SCHEDULE) as mock_sched, \
         patch("src.data.nba.players.CommonTeamRoster", mock_roster), \
         patch("src.data.nba.players.time") as mock_time:
        mock_time.sleep = MagicMock()
        result = get_tonights_players(game_date=date(2024, 11, 1))

    mock_sched.assert_called_once()
    assert mock_roster.call_count == 2  # One per team
    assert isinstance(result, dict)


def test_get_tonights_players_returns_correct_structure():
    """get_tonights_players() returns dict[int, dict] keyed by player_id."""
    mock_roster = MagicMock()
    mock_roster.return_value.get_normalized_dict.side_effect = [
        {"CommonTeamRoster": SAMPLE_ROSTER_HAWKS},
        {"CommonTeamRoster": SAMPLE_ROSTER_CELTICS},
    ]

    with patch("src.data.nba.players.get_todays_schedule", return_value=SAMPLE_SCHEDULE), \
         patch("src.data.nba.players.CommonTeamRoster", mock_roster), \
         patch("src.data.nba.players.time") as mock_time:
        mock_time.sleep = MagicMock()
        result = get_tonights_players(game_date=date(2024, 11, 1))

    assert 101 in result
    assert 201 in result
    player = result[101]
    assert "player_id" in player
    assert "player_name" in player
    assert "position" in player
    assert "team_id" in player
    assert player["player_id"] == 101
    assert player["player_name"] == "Trae Young"


def test_get_tonights_players_sleep_before_each_roster_call():
    """get_tonights_players() calls time.sleep(0.5) before each CommonTeamRoster call."""
    mock_roster = MagicMock()
    mock_roster.return_value.get_normalized_dict.side_effect = [
        {"CommonTeamRoster": SAMPLE_ROSTER_HAWKS},
        {"CommonTeamRoster": SAMPLE_ROSTER_CELTICS},
    ]

    with patch("src.data.nba.players.get_todays_schedule", return_value=SAMPLE_SCHEDULE), \
         patch("src.data.nba.players.CommonTeamRoster", mock_roster), \
         patch("src.data.nba.players.time") as mock_time:
        mock_time.sleep = MagicMock()
        get_tonights_players(game_date=date(2024, 11, 1))

    # 2 teams -> 2 sleep calls
    assert mock_time.sleep.call_count >= 2
    mock_time.sleep.assert_any_call(0.5)


# ---------------------------------------------------------------------------
# get_player_stats tests
# ---------------------------------------------------------------------------


def test_get_player_stats_returns_per_game_averages():
    """get_player_stats() returns dict with per-game stats per player."""
    mock_game_log = MagicMock()
    mock_game_log.return_value.get_normalized_dict.return_value = {
        "PlayerGameLog": SAMPLE_GAME_LOG
    }

    with patch("src.data.nba.players.PlayerGameLog", mock_game_log), \
         patch("src.data.nba.players.time") as mock_time:
        mock_time.sleep = MagicMock()
        result = get_player_stats([101])

    assert isinstance(result, dict)
    assert 101 in result
    stats = result[101]
    for key in ("PTS", "REB", "AST", "FG3M", "MIN"):
        assert key in stats, f"Missing key: {key}"


def test_get_player_stats_correct_averages():
    """get_player_stats() computes correct per-game averages."""
    mock_game_log = MagicMock()
    mock_game_log.return_value.get_normalized_dict.return_value = {
        "PlayerGameLog": SAMPLE_GAME_LOG
    }

    with patch("src.data.nba.players.PlayerGameLog", mock_game_log), \
         patch("src.data.nba.players.time") as mock_time:
        mock_time.sleep = MagicMock()
        result = get_player_stats([101])

    stats = result[101]
    expected_pts = sum(g["PTS"] for g in SAMPLE_GAME_LOG) / len(SAMPLE_GAME_LOG)
    assert abs(stats["PTS"] - expected_pts) < 0.01


def test_get_player_stats_sleep_before_api_call():
    """get_player_stats() calls time.sleep(0.5) before each PlayerGameLog call."""
    mock_game_log = MagicMock()
    mock_game_log.return_value.get_normalized_dict.return_value = {
        "PlayerGameLog": SAMPLE_GAME_LOG
    }

    with patch("src.data.nba.players.PlayerGameLog", mock_game_log), \
         patch("src.data.nba.players.time") as mock_time:
        mock_time.sleep = MagicMock()
        get_player_stats([101, 201])

    mock_time.sleep.assert_called_with(0.5)
    assert mock_time.sleep.call_count >= 2


# ---------------------------------------------------------------------------
# get_player_game_logs tests
# ---------------------------------------------------------------------------


def test_get_player_game_logs_returns_n_games():
    """get_player_game_logs() returns exactly n most recent games."""
    mock_game_log = MagicMock()
    mock_game_log.return_value.get_normalized_dict.return_value = {
        "PlayerGameLog": SAMPLE_GAME_LOG
    }

    with patch("src.data.nba.players.PlayerGameLog", mock_game_log), \
         patch("src.data.nba.players.time") as mock_time:
        mock_time.sleep = MagicMock()
        result = get_player_game_logs(101, n=5)

    assert len(result) == 5


def test_get_player_game_logs_default_n_from_config():
    """get_player_game_logs() uses trend_n from trading_config (default 5)."""
    mock_game_log = MagicMock()
    mock_game_log.return_value.get_normalized_dict.return_value = {
        "PlayerGameLog": SAMPLE_GAME_LOG
    }

    with patch("src.data.nba.players.PlayerGameLog", mock_game_log), \
         patch("src.data.nba.players.time") as mock_time:
        mock_time.sleep = MagicMock()
        result = get_player_game_logs(101)  # No n= argument

    # trading_config.json has player_trend_games=5
    assert len(result) == 5


def test_get_player_game_logs_caches_with_correct_namespace():
    """get_player_game_logs() caches with namespace 'player_stats' and TTL 21600."""
    mock_game_log = MagicMock()
    mock_game_log.return_value.get_normalized_dict.return_value = {
        "PlayerGameLog": SAMPLE_GAME_LOG
    }

    with patch("src.data.nba.players.PlayerGameLog", mock_game_log), \
         patch("src.data.nba.players.time") as mock_time, \
         patch("src.data.nba.players.cache_get") as mock_cache_get, \
         patch("src.data.nba.players.cache_set") as mock_cache_set:
        mock_time.sleep = MagicMock()
        mock_cache_get.return_value = None  # Cache miss
        get_player_game_logs(101, n=5)

    # cache_get called with "player_stats" namespace
    mock_cache_get.assert_called_once()
    namespace_arg = mock_cache_get.call_args[0][0]
    assert namespace_arg == "player_stats"

    # cache_set called with "player_stats" namespace
    mock_cache_set.assert_called_once()
    assert mock_cache_set.call_args[0][0] == "player_stats"


def test_get_player_game_logs_returns_required_fields():
    """get_player_game_logs() returns dicts with required game fields."""
    mock_game_log = MagicMock()
    mock_game_log.return_value.get_normalized_dict.return_value = {
        "PlayerGameLog": SAMPLE_GAME_LOG
    }

    with patch("src.data.nba.players.PlayerGameLog", mock_game_log), \
         patch("src.data.nba.players.time") as mock_time:
        mock_time.sleep = MagicMock()
        result = get_player_game_logs(101, n=3)

    assert len(result) == 3
    required_keys = {"GAME_DATE", "MATCHUP", "WL", "MIN", "PTS", "REB", "AST", "FG3M", "PLUS_MINUS"}
    for game in result:
        assert required_keys.issubset(game.keys()), f"Missing: {required_keys - game.keys()}"


def test_get_player_game_logs_stale_fallback_on_failure(caplog):
    """get_player_game_logs() serves stale cache on API failure with warning log."""
    # Prime the cache first
    mock_game_log = MagicMock()
    mock_game_log.return_value.get_normalized_dict.return_value = {
        "PlayerGameLog": SAMPLE_GAME_LOG[:5]
    }

    with patch("src.data.nba.players.PlayerGameLog", mock_game_log), \
         patch("src.data.nba.players.time") as mock_time:
        mock_time.sleep = MagicMock()
        get_player_game_logs(101, n=5)

    # Now simulate API failure — should return stale cache
    failing_log = MagicMock()
    failing_log.return_value.get_normalized_dict.side_effect = RuntimeError("API down")

    with caplog.at_level(logging.WARNING, logger="src.data.nba.players"), \
         patch("src.data.nba.players.PlayerGameLog", failing_log), \
         patch("src.data.nba.players.time") as mock_time, \
         patch("src.data.nba.players.cache_get", return_value=None):
        mock_time.sleep = MagicMock()
        result = get_player_game_logs(101, n=5)

    assert result is not None
    assert len(caplog.records) > 0


# ---------------------------------------------------------------------------
# _fetch_espn_injuries tests (D-07: isolation)
# ---------------------------------------------------------------------------


def test_fetch_espn_injuries_is_isolated_function():
    """_fetch_espn_injuries is a standalone function (D-07)."""
    from src.data.nba import players as players_mod
    assert callable(getattr(players_mod, "_fetch_espn_injuries", None))


def test_fetch_espn_injuries_returns_list_on_success():
    """_fetch_espn_injuries() returns list of injury dicts on 200 response."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = SAMPLE_ESPN_INJURIES_RESPONSE

    mock_session = MagicMock()
    mock_session.get.return_value = mock_resp

    result = _fetch_espn_injuries(session=mock_session)

    assert isinstance(result, list)
    assert len(result) > 0
    for item in result:
        assert "player_name" in item
        assert "team" in item
        assert "status" in item


def test_fetch_espn_injuries_returns_empty_on_non_200():
    """_fetch_espn_injuries() returns empty list on non-200 status."""
    mock_resp = MagicMock()
    mock_resp.status_code = 503

    mock_session = MagicMock()
    mock_session.get.return_value = mock_resp

    result = _fetch_espn_injuries(session=mock_session)
    assert result == []


def test_fetch_espn_injuries_returns_empty_on_exception():
    """_fetch_espn_injuries() returns empty list on network exception."""
    mock_session = MagicMock()
    mock_session.get.side_effect = ConnectionError("Network failure")

    result = _fetch_espn_injuries(session=mock_session)
    assert result == []


# ---------------------------------------------------------------------------
# get_injury_report tests (D-07 fallback, D-08 confidence)
# ---------------------------------------------------------------------------


def test_get_injury_report_calls_espn_first():
    """get_injury_report() calls ESPN first."""
    espn_data = [
        {"player_name": "Trae Young", "team": "Atlanta Hawks", "status": "Questionable",
         "injury_type": "Ankle", "player_id": 101}
    ]

    with patch("src.data.nba.players._fetch_espn_injuries", return_value=espn_data) as mock_espn, \
         patch("src.data.nba.players._fetch_nba_api_injuries") as mock_nba, \
         patch("src.data.nba.players.cache_get", return_value=None), \
         patch("src.data.nba.players.cache_set"):
        result = get_injury_report()

    mock_espn.assert_called_once()
    mock_nba.assert_not_called()


def test_get_injury_report_falls_back_to_nba_api_when_espn_empty():
    """get_injury_report() falls back to nba_api when ESPN returns empty list."""
    nba_data = [
        {"player_name": "Jayson Tatum", "team": "Boston Celtics", "status": "Out",
         "injury_type": "Knee", "player_id": 201}
    ]

    with patch("src.data.nba.players._fetch_espn_injuries", return_value=[]) as mock_espn, \
         patch("src.data.nba.players._fetch_nba_api_injuries", return_value=nba_data) as mock_nba, \
         patch("src.data.nba.players.cache_get", return_value=None), \
         patch("src.data.nba.players.cache_set"):
        result = get_injury_report()

    mock_espn.assert_called_once()
    mock_nba.assert_called_once()
    assert len(result) == 1


def test_get_injury_report_out_confidence_none():
    """get_injury_report() sets confidence='none' for 'Out' players (D-08)."""
    espn_data = [
        {"player_name": "Jayson Tatum", "team": "Boston Celtics", "status": "Out",
         "injury_type": "Knee", "player_id": 201}
    ]

    with patch("src.data.nba.players._fetch_espn_injuries", return_value=espn_data), \
         patch("src.data.nba.players.cache_get", return_value=None), \
         patch("src.data.nba.players.cache_set"):
        result = get_injury_report()

    assert len(result) == 1
    assert result[0]["confidence"] == "none"


def test_get_injury_report_questionable_confidence_low():
    """get_injury_report() sets confidence='low' for Questionable players (D-08)."""
    espn_data = [
        {"player_name": "Trae Young", "team": "Atlanta Hawks", "status": "Questionable",
         "injury_type": "Ankle", "player_id": 101}
    ]

    with patch("src.data.nba.players._fetch_espn_injuries", return_value=espn_data), \
         patch("src.data.nba.players.cache_get", return_value=None), \
         patch("src.data.nba.players.cache_set"):
        result = get_injury_report()

    assert result[0]["confidence"] == "low"


def test_get_injury_report_gtd_confidence_low():
    """get_injury_report() sets confidence='low' for GTD/Day-To-Day players (D-08)."""
    espn_data = [
        {"player_name": "Jaylen Brown", "team": "Boston Celtics", "status": "Day-To-Day",
         "injury_type": "Hamstring", "player_id": 202}
    ]

    with patch("src.data.nba.players._fetch_espn_injuries", return_value=espn_data), \
         patch("src.data.nba.players.cache_get", return_value=None), \
         patch("src.data.nba.players.cache_set"):
        result = get_injury_report()

    assert result[0]["confidence"] == "low"


def test_get_injury_report_gtd_status_confidence_low():
    """get_injury_report() sets confidence='low' for GTD status (D-08)."""
    espn_data = [
        {"player_name": "Kevin Durant", "team": "Phoenix Suns", "status": "GTD",
         "injury_type": "Hamstring", "player_id": 300}
    ]

    with patch("src.data.nba.players._fetch_espn_injuries", return_value=espn_data), \
         patch("src.data.nba.players.cache_get", return_value=None), \
         patch("src.data.nba.players.cache_set"):
        result = get_injury_report()

    assert result[0]["confidence"] == "low"


def test_get_injury_report_gtd_not_filtered_out():
    """get_injury_report() does not skip GTD/Questionable players (D-08)."""
    espn_data = [
        {"player_name": "Trae Young", "team": "Atlanta Hawks", "status": "Questionable",
         "injury_type": "Ankle", "player_id": 101},
        {"player_name": "Kevin Durant", "team": "Phoenix Suns", "status": "GTD",
         "injury_type": "Hamstring", "player_id": 300},
    ]

    with patch("src.data.nba.players._fetch_espn_injuries", return_value=espn_data), \
         patch("src.data.nba.players.cache_get", return_value=None), \
         patch("src.data.nba.players.cache_set"):
        result = get_injury_report()

    # Both players present in output
    assert len(result) == 2
    names = [r["player_name"] for r in result]
    assert "Trae Young" in names
    assert "Kevin Durant" in names


# ---------------------------------------------------------------------------
# get_matchup_context tests (R2.9)
# ---------------------------------------------------------------------------


def test_get_matchup_context_returns_defensive_stats():
    """get_matchup_context() returns defensive rating vs position."""
    mock_defend = MagicMock()
    mock_defend.return_value.get_normalized_dict.return_value = {
        "LeagueDashPtDefend": SAMPLE_MATCHUP_STATS
    }

    with patch("src.data.nba.players.LeagueDashPtDefend", mock_defend), \
         patch("src.data.nba.players.time") as mock_time, \
         patch("src.data.nba.players.cache_get", return_value=None), \
         patch("src.data.nba.players.cache_set"):
        mock_time.sleep = MagicMock()
        result = get_matchup_context(opponent_team_id=1610612738, position="F")

    assert result is not None
    assert "team_id" in result
    assert "team_name" in result
    assert "d_fg_pct" in result
    assert result["team_id"] == 1610612738


def test_get_matchup_context_returns_none_when_team_not_found():
    """get_matchup_context() returns None when team not in results."""
    mock_defend = MagicMock()
    mock_defend.return_value.get_normalized_dict.return_value = {
        "LeagueDashPtDefend": SAMPLE_MATCHUP_STATS
    }

    with patch("src.data.nba.players.LeagueDashPtDefend", mock_defend), \
         patch("src.data.nba.players.time") as mock_time, \
         patch("src.data.nba.players.cache_get", return_value=None), \
         patch("src.data.nba.players.cache_set"):
        mock_time.sleep = MagicMock()
        result = get_matchup_context(opponent_team_id=99999999, position="F")

    assert result is None


def test_get_matchup_context_sleep_before_api_call():
    """get_matchup_context() calls time.sleep(0.5) before LeagueDashPtDefend."""
    mock_defend = MagicMock()
    mock_defend.return_value.get_normalized_dict.return_value = {
        "LeagueDashPtDefend": SAMPLE_MATCHUP_STATS
    }

    with patch("src.data.nba.players.LeagueDashPtDefend", mock_defend), \
         patch("src.data.nba.players.time") as mock_time, \
         patch("src.data.nba.players.cache_get", return_value=None), \
         patch("src.data.nba.players.cache_set"):
        mock_time.sleep = MagicMock()
        get_matchup_context(opponent_team_id=1610612738, position="G")

    mock_time.sleep.assert_called_with(0.5)


def test_get_matchup_context_stale_fallback_on_failure(caplog):
    """get_matchup_context() serves stale cache on API failure with warning log."""
    # First call: prime the cache
    mock_defend = MagicMock()
    mock_defend.return_value.get_normalized_dict.return_value = {
        "LeagueDashPtDefend": SAMPLE_MATCHUP_STATS
    }

    with patch("src.data.nba.players.LeagueDashPtDefend", mock_defend), \
         patch("src.data.nba.players.time") as mock_time:
        mock_time.sleep = MagicMock()
        get_matchup_context(opponent_team_id=1610612738, position="F")

    # Second call: API fails
    failing_defend = MagicMock()
    failing_defend.return_value.get_normalized_dict.side_effect = RuntimeError("API down")

    with caplog.at_level(logging.WARNING, logger="src.data.nba.players"), \
         patch("src.data.nba.players.LeagueDashPtDefend", failing_defend), \
         patch("src.data.nba.players.time") as mock_time, \
         patch("src.data.nba.players.cache_get", return_value=None):
        mock_time.sleep = MagicMock()
        result = get_matchup_context(opponent_team_id=1610612738, position="F")

    assert len(caplog.records) > 0
