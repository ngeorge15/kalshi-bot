"""Unit tests for src/data/nba/history.py.

Uses in-memory SQLite for isolation. Mocks LeagueGameFinder at the
``get_normalized_dict()`` level — no actual nba_api network calls.
"""

from unittest.mock import MagicMock, patch

import pytest

from src.db.database import Database
from src.data.nba.history import fetch_historical_results, get_historical_results


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mem_db():
    """In-memory SQLite Database with schema initialized."""
    return Database(":memory:")


def _make_game_rows(game_id: str, home_abbr: str, away_abbr: str,
                    home_team_id: int, away_team_id: int,
                    game_date: str = "2023-11-15",
                    home_pts: int = 110, away_pts: int = 100,
                    season: str = "2023-24") -> list[dict]:
    """Return two rows as LeagueGameFinder would return for a single game.

    LeagueGameFinder returns one row per team per game.
    """
    home_wl = "W" if home_pts > away_pts else "L"
    away_wl = "L" if home_pts > away_pts else "W"
    return [
        {
            "GAME_ID": game_id,
            "GAME_DATE": game_date,
            "TEAM_ID": home_team_id,
            "TEAM_ABBREVIATION": home_abbr,
            "MATCHUP": f"{home_abbr} vs. {away_abbr}",
            "WL": home_wl,
            "PTS": home_pts,
        },
        {
            "GAME_ID": game_id,
            "GAME_DATE": game_date,
            "TEAM_ID": away_team_id,
            "TEAM_ABBREVIATION": away_abbr,
            "MATCHUP": f"{away_abbr} @ {home_abbr}",
            "WL": away_wl,
            "PTS": away_pts,
        },
    ]


SAMPLE_ROWS = _make_game_rows(
    game_id="0022300001",
    home_abbr="LAL",
    away_abbr="GSW",
    home_team_id=1610612747,
    away_team_id=1610612744,
    game_date="2023-11-15",
    home_pts=115,
    away_pts=105,
    season="2023-24",
)

SAMPLE_ROWS_2 = _make_game_rows(
    game_id="0022300002",
    home_abbr="BOS",
    away_abbr="MIA",
    home_team_id=1610612738,
    away_team_id=1610612748,
    game_date="2023-11-16",
    home_pts=100,
    away_pts=120,  # Away wins
    season="2023-24",
)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_fetch_historical_results_writes_to_db(mem_db, monkeypatch):
    """fetch_historical_results calls LeagueGameFinder and writes to nba_game_results."""
    import src.data.cache as cache_mod
    import tempfile, pathlib
    tmp = pathlib.Path(tempfile.mkdtemp())
    monkeypatch.setattr(cache_mod, "CACHE_DIR", tmp / "cache")

    mock_endpoint = MagicMock()
    mock_endpoint.return_value.get_normalized_dict.return_value = {
        "LeagueGameFinderResults": SAMPLE_ROWS
    }

    with patch("src.data.nba.history.LeagueGameFinder", mock_endpoint), \
         patch("src.data.nba.history.time") as mock_time:
        mock_time.sleep = MagicMock()
        count = fetch_historical_results(db=mem_db, seasons=["2023-24"])

    rows = mem_db.fetchall("SELECT * FROM nba_game_results")
    assert len(rows) == 1
    assert rows[0]["game_id"] == "0022300001"
    assert count == 1


def test_fetch_historical_results_deduplicates(mem_db, monkeypatch):
    """Calling fetch_historical_results twice does not double records."""
    import src.data.cache as cache_mod
    import tempfile, pathlib
    tmp = pathlib.Path(tempfile.mkdtemp())
    monkeypatch.setattr(cache_mod, "CACHE_DIR", tmp / "cache")

    mock_endpoint = MagicMock()
    mock_endpoint.return_value.get_normalized_dict.return_value = {
        "LeagueGameFinderResults": SAMPLE_ROWS
    }

    with patch("src.data.nba.history.LeagueGameFinder", mock_endpoint), \
         patch("src.data.nba.history.time") as mock_time:
        mock_time.sleep = MagicMock()
        fetch_historical_results(db=mem_db, seasons=["2023-24"])
        fetch_historical_results(db=mem_db, seasons=["2023-24"])

    rows = mem_db.fetchall("SELECT * FROM nba_game_results")
    assert len(rows) == 1, "Record should not be duplicated"


def test_get_historical_results_returns_all(mem_db, monkeypatch):
    """get_historical_results(season=None) returns all stored results."""
    import src.data.cache as cache_mod
    import tempfile, pathlib
    tmp = pathlib.Path(tempfile.mkdtemp())
    monkeypatch.setattr(cache_mod, "CACHE_DIR", tmp / "cache")

    mock_endpoint = MagicMock()
    mock_endpoint.return_value.get_normalized_dict.return_value = {
        "LeagueGameFinderResults": SAMPLE_ROWS + SAMPLE_ROWS_2
    }

    with patch("src.data.nba.history.LeagueGameFinder", mock_endpoint), \
         patch("src.data.nba.history.time") as mock_time:
        mock_time.sleep = MagicMock()
        fetch_historical_results(db=mem_db, seasons=["2023-24"])

    results = get_historical_results(db=mem_db)
    assert isinstance(results, list)
    assert len(results) == 2


def test_get_historical_results_filters_by_season(mem_db, monkeypatch):
    """get_historical_results(season='2022-23') filters by season."""
    import src.data.cache as cache_mod
    import tempfile, pathlib
    tmp = pathlib.Path(tempfile.mkdtemp())
    monkeypatch.setattr(cache_mod, "CACHE_DIR", tmp / "cache")

    # Insert one game per season
    older_rows = _make_game_rows(
        game_id="0022200001",
        home_abbr="NYK",
        away_abbr="CHI",
        home_team_id=1610612752,
        away_team_id=1610612741,
        game_date="2022-11-15",
        season="2022-23",
    )

    mock_endpoint = MagicMock()
    # First call returns older season
    mock_endpoint.return_value.get_normalized_dict.side_effect = [
        {"LeagueGameFinderResults": older_rows},
        {"LeagueGameFinderResults": SAMPLE_ROWS},
    ]

    with patch("src.data.nba.history.LeagueGameFinder", mock_endpoint), \
         patch("src.data.nba.history.time") as mock_time:
        mock_time.sleep = MagicMock()
        fetch_historical_results(db=mem_db, seasons=["2022-23", "2023-24"])

    only_old = get_historical_results(db=mem_db, season="2022-23")
    assert len(only_old) == 1
    assert only_old[0]["season"] == "2022-23"


def test_home_win_flag_set_correctly(mem_db, monkeypatch):
    """home_win = 1 when home_pts > away_pts, 0 otherwise."""
    import src.data.cache as cache_mod
    import tempfile, pathlib
    tmp = pathlib.Path(tempfile.mkdtemp())
    monkeypatch.setattr(cache_mod, "CACHE_DIR", tmp / "cache")

    # Game 1: home wins (115 > 105), Game 2: away wins (100 < 120)
    mock_endpoint = MagicMock()
    mock_endpoint.return_value.get_normalized_dict.return_value = {
        "LeagueGameFinderResults": SAMPLE_ROWS + SAMPLE_ROWS_2
    }

    with patch("src.data.nba.history.LeagueGameFinder", mock_endpoint), \
         patch("src.data.nba.history.time") as mock_time:
        mock_time.sleep = MagicMock()
        fetch_historical_results(db=mem_db, seasons=["2023-24"])

    rows = {r["game_id"]: r for r in mem_db.fetchall("SELECT * FROM nba_game_results")}
    assert rows["0022300001"]["home_win"] == 1  # LAL 115 vs GSW 105 — LAL wins
    assert rows["0022300002"]["home_win"] == 0  # BOS 100 vs MIA 120 — BOS loses


def test_matchup_parsing_vs_is_home_at_is_away(mem_db, monkeypatch):
    """MATCHUP parsing: 'vs.' means that team is home, '@' means that team is away."""
    import src.data.cache as cache_mod
    import tempfile, pathlib
    tmp = pathlib.Path(tempfile.mkdtemp())
    monkeypatch.setattr(cache_mod, "CACHE_DIR", tmp / "cache")

    mock_endpoint = MagicMock()
    mock_endpoint.return_value.get_normalized_dict.return_value = {
        "LeagueGameFinderResults": SAMPLE_ROWS  # LAL vs. GSW => LAL is home
    }

    with patch("src.data.nba.history.LeagueGameFinder", mock_endpoint), \
         patch("src.data.nba.history.time") as mock_time:
        mock_time.sleep = MagicMock()
        fetch_historical_results(db=mem_db, seasons=["2023-24"])

    row = mem_db.fetchone("SELECT * FROM nba_game_results WHERE game_id = '0022300001'")
    assert row is not None
    assert row["home_team_id"] == 1610612747  # LAL is home
    assert row["away_team_id"] == 1610612744  # GSW is away


def test_fetch_uses_three_default_seasons(mem_db, monkeypatch):
    """Default seasons are 2021-22, 2022-23, 2023-24 (3 seasons)."""
    import src.data.cache as cache_mod
    import tempfile, pathlib
    tmp = pathlib.Path(tempfile.mkdtemp())
    monkeypatch.setattr(cache_mod, "CACHE_DIR", tmp / "cache")

    mock_endpoint = MagicMock()
    mock_endpoint.return_value.get_normalized_dict.return_value = {
        "LeagueGameFinderResults": []
    }

    with patch("src.data.nba.history.LeagueGameFinder", mock_endpoint), \
         patch("src.data.nba.history.time") as mock_time:
        mock_time.sleep = MagicMock()
        fetch_historical_results(db=mem_db)  # No seasons arg — use defaults

    # Should have called LeagueGameFinder 3 times (one per season)
    assert mock_endpoint.call_count == 3
    call_kwargs = [call.kwargs for call in mock_endpoint.call_args_list]
    seasons_called = [kw.get("season_nullable") for kw in call_kwargs]
    assert "2021-22" in seasons_called
    assert "2022-23" in seasons_called
    assert "2023-24" in seasons_called
