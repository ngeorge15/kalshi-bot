"""NBA historical game results pipeline.

Fetches multiple seasons of game results via nba_api's LeagueGameFinder endpoint
and persists them to the ``nba_game_results`` SQLite table.  Subsequent reads
query the local database — no API call is needed after the initial fetch.

Per D-09: pull-once, store to SQLite.  Re-fetch with ``--refresh-history`` flag
(not implemented here — caller is responsible for deciding when to invoke).

Rate-limit compliance: every nba_api call is preceded by ``time.sleep(0.5)``.

Usage::

    from src.data.nba.history import fetch_historical_results, get_historical_results
    from src.db.database import Database

    db = Database()
    n = fetch_historical_results(db=db)        # Fetches 3 seasons, returns # inserted
    results = get_historical_results(db=db)    # All seasons
    filtered = get_historical_results(db=db, season="2023-24")
"""

import logging
import time
from collections import defaultdict
from datetime import datetime
from typing import Optional

from nba_api.stats.endpoints import LeagueGameFinder

from src.data.cache import cache_get_stale, cache_set
from src.db.database import Database

logger = logging.getLogger(__name__)

# Default seasons to fetch on first run (D-06: 3 seasons of training data)
DEFAULT_SEASONS: list[str] = ["2021-22", "2022-23", "2023-24"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_game_records(rows: list[dict], season: str) -> list[dict]:
    """Convert raw LeagueGameFinder rows into nba_game_results records.

    LeagueGameFinder returns one row per *team* per game (so 2 rows for each
    game).  This function groups rows by GAME_ID, identifies home/away from
    MATCHUP string, and returns one record per game.

    MATCHUP format:
        ``"LAL vs. GSW"`` — LAL is home (played at home, hosting GSW)
        ``"LAL @ GSW"``   — LAL is away (played at GSW)

    Args:
        rows: Raw ``LeagueGameFinderResults`` list.
        season: Season string (e.g. ``"2023-24"``), stored on every record.

    Returns:
        List of ``nba_game_results``-compatible dicts (one per game).
    """
    by_game: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_game[row["GAME_ID"]].append(row)

    records: list[dict] = []
    for game_id, game_rows in by_game.items():
        home_row: Optional[dict] = None
        away_row: Optional[dict] = None

        for row in game_rows:
            matchup: str = row.get("MATCHUP", "")
            if " vs. " in matchup or " vs " in matchup:
                home_row = row
            elif " @ " in matchup:
                away_row = row

        if home_row is None or away_row is None:
            logger.warning("Could not determine home/away for game_id=%s; skipping", game_id)
            continue

        home_pts = int(home_row.get("PTS") or 0)
        away_pts = int(away_row.get("PTS") or 0)
        game_date_raw: str = home_row.get("GAME_DATE", "")
        # Normalize date: might be "2023-11-15" or "NOV 15, 2023"
        # LeagueGameFinder returns ISO format "YYYY-MM-DD"
        try:
            game_date = datetime.strptime(game_date_raw, "%Y-%m-%d").date().isoformat()
        except ValueError:
            game_date = game_date_raw  # Store as-is if unparseable

        records.append(
            {
                "game_id": game_id,
                "game_date": game_date,
                "home_team_id": int(home_row["TEAM_ID"]),
                "away_team_id": int(away_row["TEAM_ID"]),
                "home_pts": home_pts,
                "away_pts": away_pts,
                "home_win": 1 if home_pts > away_pts else 0,
                "season": season,
            }
        )

    return records


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_historical_results(
    db: Optional[Database] = None,
    seasons: Optional[list[str]] = None,
) -> int:
    """Fetch historical NBA game results and persist them to SQLite.

    For each season, calls LeagueGameFinder, parses home/away, and inserts
    records using ``INSERT OR IGNORE`` so re-running is idempotent.

    Cache behavior: on API failure for a season, attempts stale-cache fallback
    with a warning log (D-04).  Cache namespace is ``"historical"``.

    Args:
        db: :class:`~src.db.database.Database` instance.  Defaults to a new
            ``Database()`` pointing at ``data/kalshi_bot.db``.
        seasons: Season strings to fetch.  Defaults to
            ``["2021-22", "2022-23", "2023-24"]``.

    Returns:
        Total number of newly inserted rows (``rowcount`` across all seasons).
        0 if all records were already present (idempotent re-run).
    """
    if db is None:
        db = Database()
    if seasons is None:
        seasons = DEFAULT_SEASONS

    cache_params = {"type": "nba_results", "seasons": seasons}
    total_inserted = 0

    for season in seasons:
        rows: Optional[list[dict]] = None
        try:
            time.sleep(0.5)
            result = LeagueGameFinder(
                player_or_team_abbreviation="T",
                season_nullable=season,
                season_type_nullable="Regular Season",
            ).get_normalized_dict()
            rows = result.get("LeagueGameFinderResults", [])
        except Exception as exc:
            logger.warning(
                "nba_api LeagueGameFinder failed for season %s, trying stale cache: %s",
                season,
                exc,
            )
            stale = cache_get_stale("historical", {"season": season})
            if stale is not None:
                rows = stale
            else:
                logger.warning("No stale cache for season %s; skipping", season)
                continue

        records = _parse_game_records(rows, season)
        for record in records:
            cursor = db.execute(
                """INSERT OR IGNORE INTO nba_game_results
                   (game_id, game_date, home_team_id, away_team_id,
                    home_pts, away_pts, home_win, season)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record["game_id"],
                    record["game_date"],
                    record["home_team_id"],
                    record["away_team_id"],
                    record["home_pts"],
                    record["away_pts"],
                    record["home_win"],
                    record["season"],
                ),
            )
            total_inserted += cursor.rowcount

    # Cache a fetch-completion timestamp so callers can check freshness
    cache_set("historical", cache_params, {"fetched_seasons": seasons})
    logger.debug("fetch_historical_results: inserted %d rows total", total_inserted)
    return total_inserted


def get_historical_results(
    db: Optional[Database] = None,
    season: Optional[str] = None,
) -> list[dict]:
    """Read historical NBA game results from the local SQLite database.

    Args:
        db: :class:`~src.db.database.Database` instance.  Defaults to a new
            ``Database()`` pointing at ``data/kalshi_bot.db``.
        season: If provided, filter results to this season string
            (e.g. ``"2023-24"``).  If ``None``, returns all seasons.

    Returns:
        List of row dicts from ``nba_game_results``, ordered by ``game_date``.
    """
    if db is None:
        db = Database()

    if season is None:
        return db.fetchall("SELECT * FROM nba_game_results ORDER BY game_date")
    else:
        return db.fetchall(
            "SELECT * FROM nba_game_results WHERE season = ? ORDER BY game_date",
            (season,),
        )
