"""NBA player stats, game logs, injury reports, and matchup context.

Wraps ``nba_api`` endpoints with file-based caching and stale-fallback per D-04.
All nba_api calls include a ``time.sleep(0.5)`` rate-limit delay.

Injury data uses ESPN JSON API as primary source with nba_api fallback (D-07).
GTD/Questionable players are flagged as low-confidence, not dropped (D-08).
Players are fetched on-demand for tonight's schedule only (D-05).

Usage::

    from src.data.nba.players import (
        get_tonights_players,
        get_player_stats,
        get_player_game_logs,
        get_injury_report,
        get_matchup_context,
    )

    players = get_tonights_players()
    stats = get_player_stats(list(players.keys()))
    logs = get_player_game_logs(player_id=101, n=5)
    injuries = get_injury_report()
    context = get_matchup_context(opponent_team_id=1610612738, position="F")
"""

import logging
import time
from datetime import date
from typing import Optional

import requests
from nba_api.stats.endpoints import (
    CommonTeamRoster,
    LeagueDashPtDefend,
    PlayerGameLog,
)

from src.config import config
from src.data.cache import cache_get, cache_get_stale, cache_set
from src.data.nba.teams import get_todays_schedule

logger = logging.getLogger(__name__)

ESPN_INJURIES_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries"
)

_DEFAULT_TREND_N = 5


def _get_trend_n() -> int:
    """Return the configured player_trend_games value, defaulting to 5."""
    try:
        if config is not None:
            return config.markets["nba"].get("player_trend_games", _DEFAULT_TREND_N)
    except Exception:
        pass
    return _DEFAULT_TREND_N


# ---------------------------------------------------------------------------
# ESPN Injury Scraper (D-07: isolated function)
# ---------------------------------------------------------------------------


def _fetch_espn_injuries(
    session: Optional[requests.Session] = None,
) -> list[dict]:
    """Fetch current NBA injuries from the ESPN JSON API (D-07: isolated function).

    This function is intentionally standalone so it can be replaced without
    touching the rest of the injury pipeline.

    Args:
        session: Optional :class:`requests.Session` to use.  Creates a new
            session if not provided.

    Returns:
        List of injury dicts with keys ``player_name``, ``team``, ``status``,
        ``injury_type``, ``player_id``.  Returns an empty list on any failure.
    """
    if session is None:
        session = requests.Session()

    try:
        resp = session.get(ESPN_INJURIES_URL, timeout=15)
        if resp.status_code != 200:
            logger.warning(
                "ESPN injuries endpoint returned status %d", resp.status_code
            )
            return []

        data = resp.json()
        injuries: list[dict] = []

        for team_entry in data.get("injuries", []):
            team_name = team_entry.get("team", {}).get("displayName", "")
            for injury in team_entry.get("injuries", []):
                athlete = injury.get("athlete", {})
                raw_player_id = athlete.get("id")
                try:
                    player_id: Optional[int] = int(raw_player_id) if raw_player_id else None
                except (ValueError, TypeError):
                    player_id = None

                details = injury.get("details", {})
                injuries.append(
                    {
                        "player_name": athlete.get("displayName", ""),
                        "team": team_name,
                        "status": injury.get("status", ""),
                        "injury_type": details.get("type", ""),
                        "player_id": player_id,
                    }
                )

        return injuries

    except Exception as exc:
        logger.warning("ESPN injuries fetch failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# nba_api Injury Fallback (D-07)
# ---------------------------------------------------------------------------


def _fetch_nba_api_injuries(season: str = "2024-25") -> list[dict]:
    """Fetch NBA injuries from nba_api as a fallback when ESPN fails (D-07).

    Args:
        season: NBA season string (e.g. ``"2024-25"``).

    Returns:
        List of injury dicts normalized to the same format as
        :func:`_fetch_espn_injuries`.  Returns an empty list on any failure.
    """
    try:
        from nba_api.stats.endpoints import LeagueInjuryReport

        time.sleep(0.5)
        result = LeagueInjuryReport(season=season).get_normalized_dict()

        # nba_api LeagueInjuryReport returns a dict with one or more table keys.
        # The primary table is usually "LeagueInjuryReport".
        rows = []
        for key in result:
            if isinstance(result[key], list):
                rows = result[key]
                break

        injuries: list[dict] = []
        for row in rows:
            injuries.append(
                {
                    "player_name": row.get("PlayerName", row.get("PLAYER_NAME", "")),
                    "team": row.get("TeamName", row.get("TEAM_NAME", "")),
                    "status": row.get("PlayerStatus", row.get("PLAYER_STATUS", "")),
                    "injury_type": row.get("InjuryDescription", row.get("INJURY_DESCRIPTION", "")),
                    "player_id": row.get("PlayerId", row.get("PLAYER_ID")),
                }
            )
        return injuries

    except Exception as exc:
        logger.warning("nba_api injury report fetch failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Injury Report (public API)
# ---------------------------------------------------------------------------


def get_injury_report(season: str = "2024-25") -> list[dict]:
    """Fetch current NBA injury report with ESPN as primary source (D-07).

    TTL is 3 600 s (1 hour) — injury status changes throughout the day.
    On total failure, stale cache is returned with a warning log (D-04).

    GTD, Questionable, and Day-To-Day players are *not* dropped — they are
    returned with ``confidence="low"`` per D-08.  ``"Out"`` players have
    ``confidence="none"``.  Healthy / probable players have
    ``confidence="high"``.

    Args:
        season: NBA season string for nba_api fallback (e.g. ``"2024-25"``).

    Returns:
        List of injury dicts with keys ``player_name``, ``team``, ``status``,
        ``injury_type``, ``confidence``.  Returns an empty list if both
        sources and cache are unavailable.
    """
    namespace = "injuries"
    params = {"season": season}
    ttl = 3600

    cached = cache_get(namespace, params, ttl_seconds=ttl)
    if cached is not None:
        return cached

    raw: list[dict] = _fetch_espn_injuries()
    if not raw:
        raw = _fetch_nba_api_injuries(season=season)

    if not raw:
        logger.warning(
            "All injury sources failed for season %s, serving stale cache", season
        )
        stale = cache_get_stale(namespace, params)
        return stale if stale is not None else []

    # Apply D-08 confidence flags
    _LOW_CONFIDENCE_STATUSES = {"questionable", "day-to-day", "gtd"}
    result: list[dict] = []
    for inj in raw:
        status = inj.get("status", "")
        status_lower = status.lower()

        if status_lower == "out":
            confidence = "none"
        elif status_lower in _LOW_CONFIDENCE_STATUSES:
            confidence = "low"
        else:
            confidence = "high"

        result.append(
            {
                "player_name": inj.get("player_name", ""),
                "team": inj.get("team", ""),
                "status": status,
                "injury_type": inj.get("injury_type", ""),
                "confidence": confidence,
            }
        )

    cache_set(namespace, params, result)
    return result


# ---------------------------------------------------------------------------
# Tonight's Players (D-05: on-demand schedule-driven fetch)
# ---------------------------------------------------------------------------


def get_tonights_players(
    game_date: Optional[date] = None,
    season: str = "2024-25",
) -> dict[int, dict]:
    """Fetch all players rostered on teams playing tonight (D-05).

    Uses :func:`src.data.nba.teams.get_todays_schedule` to discover tonight's
    teams, then fetches the roster for each via ``CommonTeamRoster``.

    Results are cached with namespace ``"player_roster"`` for 24 hours.

    Args:
        game_date: Date to fetch games for.  Defaults to ``date.today()``.
        season: NBA season string (e.g. ``"2024-25"``).

    Returns:
        Dict keyed by player_id: ``{"player_id": int, "player_name": str,
        "position": str, "team_id": int}``.
        Returns an empty dict if both the API and cache are unavailable.
    """
    if game_date is None:
        game_date = date.today()

    namespace = "player_roster"
    params = {"date": game_date.isoformat(), "season": season}
    ttl = 86400

    cached = cache_get(namespace, params, ttl_seconds=ttl)
    if cached is not None:
        # cached is a list of player dicts — rebuild as dict keyed by player_id
        if isinstance(cached, list):
            return {p["player_id"]: p for p in cached}
        return cached

    schedule = get_todays_schedule(game_date)

    # Collect unique team IDs from tonight's games
    team_ids: set[int] = set()
    for game in schedule:
        if game.get("HOME_TEAM_ID"):
            team_ids.add(int(game["HOME_TEAM_ID"]))
        if game.get("VISITOR_TEAM_ID"):
            team_ids.add(int(game["VISITOR_TEAM_ID"]))

    players: dict[int, dict] = {}
    for team_id in team_ids:
        try:
            time.sleep(0.5)
            roster = CommonTeamRoster(
                team_id=team_id, season=season
            ).get_normalized_dict()["CommonTeamRoster"]

            for row in roster:
                pid = int(row.get("PLAYER_ID", 0))
                if pid:
                    players[pid] = {
                        "player_id": pid,
                        "player_name": row.get("PLAYER", ""),
                        "position": row.get("POSITION", ""),
                        "team_id": team_id,
                    }
        except Exception as exc:
            logger.warning(
                "CommonTeamRoster failed for team %d: %s", team_id, exc
            )

    # Cache as a list so it survives JSON round-trip (int keys would become str)
    cache_set(namespace, params, list(players.values()))
    return players


# ---------------------------------------------------------------------------
# Player Stats (per-game averages via PlayerGameLog)
# ---------------------------------------------------------------------------


def get_player_stats(
    player_ids: list[int],
    season: str = "2024-25",
) -> dict[int, dict]:
    """Fetch per-game stat averages for the given player IDs.

    Computes averages from the full season ``PlayerGameLog`` for each player.
    Results are cached per player with namespace ``"player_stats"`` for 6 hours.
    Stale cache is returned on API failure (D-04).

    Args:
        player_ids: List of NBA player IDs to fetch stats for.
        season: NBA season string (e.g. ``"2024-25"``).

    Returns:
        Dict keyed by player_id containing per-game averages:
        ``PTS, REB, AST, FG3M, MIN, FGM, FGA, FG3A, FTM, FTA, STL, BLK, TOV``.
    """
    namespace = "player_stats"
    ttl = 21600

    _STAT_KEYS = ["PTS", "REB", "AST", "FG3M", "MIN", "FGM", "FGA", "FG3A", "FTM", "FTA", "STL", "BLK", "TOV"]

    result: dict[int, dict] = {}

    for pid in player_ids:
        params = {"player_id": pid, "season": season, "type": "averages"}
        cached = cache_get(namespace, params, ttl_seconds=ttl)
        if cached is not None:
            result[pid] = cached
            continue

        try:
            time.sleep(0.5)
            logs = PlayerGameLog(
                player_id=pid, season=season
            ).get_normalized_dict()["PlayerGameLog"]

            if not logs:
                result[pid] = {k: 0.0 for k in _STAT_KEYS}
                continue

            # Compute per-game averages
            averages: dict[str, float] = {}
            for key in _STAT_KEYS:
                values = []
                for game in logs:
                    raw = game.get(key, 0)
                    try:
                        # MIN may be "35:12" format — extract minutes as float
                        if key == "MIN" and isinstance(raw, str) and ":" in raw:
                            parts = raw.split(":")
                            raw = float(parts[0]) + float(parts[1]) / 60
                        values.append(float(raw))
                    except (ValueError, TypeError):
                        values.append(0.0)
                averages[key] = sum(values) / len(values) if values else 0.0

            cache_set(namespace, params, averages)
            result[pid] = averages

        except Exception as exc:
            logger.warning(
                "PlayerGameLog (averages) failed for player %d: %s", pid, exc
            )
            stale = cache_get_stale(namespace, params)
            if stale is not None:
                result[pid] = stale

    return result


# ---------------------------------------------------------------------------
# Player Game Logs (trend detection)
# ---------------------------------------------------------------------------


def get_player_game_logs(
    player_id: int,
    n: Optional[int] = None,
    season: str = "2024-25",
) -> list[dict]:
    """Fetch the last *n* games for a player (trend detection).

    Default *n* comes from ``config.markets["nba"]["player_trend_games"]``
    (currently 5).  Results are cached per player with namespace
    ``"player_stats"`` for 6 hours.  Stale cache is returned on API failure
    (D-04).

    Args:
        player_id: NBA player ID.
        n: Number of most-recent games to return.  Defaults to config value.
        season: NBA season string (e.g. ``"2024-25"``).

    Returns:
        List of game log dicts (most recent first) containing:
        ``GAME_DATE, MATCHUP, WL, MIN, PTS, REB, AST, STL, BLK, TOV,
        FG3M, PLUS_MINUS``.
        Returns an empty list if both API and cache are unavailable.
    """
    if n is None:
        n = _get_trend_n()

    namespace = "player_stats"
    params = {"player_id": player_id, "season": season, "type": "logs"}
    ttl = 21600

    cached = cache_get(namespace, params, ttl_seconds=ttl)
    if cached is not None:
        return cached[:n]

    _LOG_KEYS = [
        "GAME_DATE", "MATCHUP", "WL", "MIN", "PTS", "REB", "AST",
        "STL", "BLK", "TOV", "FG3M", "PLUS_MINUS",
    ]

    try:
        time.sleep(0.5)
        raw_logs = PlayerGameLog(
            player_id=player_id, season=season
        ).get_normalized_dict()["PlayerGameLog"]

        # Trim to the requested fields only
        logs: list[dict] = [
            {k: g.get(k) for k in _LOG_KEYS}
            for g in raw_logs
        ]

        cache_set(namespace, params, logs)
        return logs[:n]

    except Exception as exc:
        logger.warning(
            "PlayerGameLog (logs) failed for player %d, serving stale cache: %s",
            player_id,
            exc,
        )
        stale = cache_get_stale(namespace, params)
        if stale is not None:
            return stale[:n]
        return []


# ---------------------------------------------------------------------------
# Matchup Context (R2.9)
# ---------------------------------------------------------------------------


def get_matchup_context(
    opponent_team_id: int,
    position: str,
    season: str = "2024-25",
) -> Optional[dict]:
    """Fetch defensive rating stats for an opponent team vs a given position.

    Uses ``LeagueDashPtDefend`` to get per-position opponent defense stats.
    Results are cached with namespace ``"matchup_context"`` for 24 hours.
    Stale cache returned on API failure (D-04).

    Args:
        opponent_team_id: NBA team ID of the opposing team.
        position: Player position to look up (e.g. ``"G"``, ``"F"``, ``"C"``).
        season: NBA season string (e.g. ``"2024-25"``).

    Returns:
        Dict with keys ``team_id``, ``team_name``, ``d_fg_pct``, ``freq``,
        ``d_fga``, or ``None`` if the team is not found in the results.
        Returns stale cache on failure, or ``None`` if nothing available.
    """
    namespace = "matchup_context"
    params = {
        "opponent_team_id": opponent_team_id,
        "position": position,
        "season": season,
    }
    ttl = 86400

    cached = cache_get(namespace, params, ttl_seconds=ttl)
    if cached is not None:
        return cached

    try:
        time.sleep(0.5)
        result = LeagueDashPtDefend(
            defense_category=position,
            season=season,
            per_mode_simple="PerGame",
        ).get_normalized_dict()

        # Key may vary — find the list
        rows: list[dict] = []
        for key in result:
            if isinstance(result[key], list):
                rows = result[key]
                break

        # Find the row matching opponent_team_id
        for row in rows:
            row_team_id = row.get("TEAM_ID", row.get("TeamId"))
            if row_team_id is not None and int(row_team_id) == opponent_team_id:
                context = {
                    "team_id": int(row_team_id),
                    "team_name": row.get("TEAM_NAME", row.get("TeamName", "")),
                    "d_fg_pct": float(row.get("D_FG_PCT", row.get("D_FGA_PCT", 0.0))),
                    "freq": float(row.get("FREQ", 0.0)),
                    "d_fga": float(row.get("D_FGA", 0.0)),
                }
                cache_set(namespace, params, context)
                return context

        logger.warning(
            "Team %d not found in LeagueDashPtDefend results for position %s",
            opponent_team_id,
            position,
        )
        return None

    except Exception as exc:
        logger.warning(
            "LeagueDashPtDefend failed for team %d, serving stale cache: %s",
            opponent_team_id,
            exc,
        )
        stale = cache_get_stale(namespace, params)
        return stale
