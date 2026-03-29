"""NBA team stats, schedule, ELO ratings, and rest-day tracking.

Wraps ``nba_api`` endpoints with file-based caching and stale-fallback per D-04.
All nba_api calls include a ``time.sleep(0.5)`` rate-limit delay.

Usage::

    from src.data.nba.teams import (
        get_team_stats,
        get_todays_schedule,
        get_rest_days,
        get_team_elos,
        update_elo,
        EloTracker,
    )

    teams = get_team_stats()
    games  = get_todays_schedule()
    elo    = EloTracker()
"""

import logging
import time
from datetime import date
from typing import Optional

from nba_api.stats.endpoints import LeagueDashTeamStats, ScoreboardV3

from src.data.cache import cache_get, cache_get_stale, cache_set

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Team Stats
# ---------------------------------------------------------------------------


def get_team_stats(season: str = "2024-25") -> list[dict]:
    """Fetch advanced team stats (OFF/DEF/NET rating, PACE, W/L) via nba_api.

    Results are cached with namespace ``"team_stats"`` for 24 hours.  On API
    failure the most recent stale cache entry is returned instead (D-04).

    Args:
        season: NBA season string in the form ``"YYYY-YY"`` (e.g. ``"2024-25"``).

    Returns:
        List of team stat dicts with at minimum TEAM_ID, TEAM_NAME,
        OFF_RATING, DEF_RATING, NET_RATING, PACE, W, L, W_PCT.
        Returns an empty list if both the API and cache are unavailable.
    """
    params = {"season": season}
    cached = cache_get("team_stats", params)
    if cached is not None:
        return cached

    try:
        time.sleep(0.5)
        result = LeagueDashTeamStats(
            season=season,
            season_type_all_star="Regular Season",
            measure_type_detailed_defense="Advanced",
            per_mode_simple="PerGame",
        ).get_normalized_dict()
        teams: list[dict] = result["LeagueDashTeamStats"]
        cache_set("team_stats", params, teams)
        return teams
    except Exception as exc:
        logger.warning(
            "nba_api failed for team stats, serving stale cache: %s", exc
        )
        stale = cache_get_stale("team_stats", params)
        return stale if stale is not None else []


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------


def get_todays_schedule(game_date: Optional[date] = None) -> list[dict]:
    """Fetch today's NBA schedule via ScoreboardV3.

    Results are cached with namespace ``"schedule"`` for 24 hours.  On API
    failure the most recent stale cache entry is returned (D-04).

    .. warning::
        Uses ``ScoreboardV3``, NOT ``ScoreboardV2``.  ScoreboardV2 is broken for
        the 2025-26 season (and newer).

    Args:
        game_date: The date to fetch games for.  Defaults to ``date.today()``.

    Returns:
        List of game header dicts containing at minimum GAME_ID, HOME_TEAM_ID,
        VISITOR_TEAM_ID, GAME_DATE_EST.
        Returns an empty list if both the API and cache are unavailable.
    """
    if game_date is None:
        game_date = date.today()

    params = {"date": game_date.isoformat()}
    cached = cache_get("schedule", params)
    if cached is not None:
        return cached

    try:
        time.sleep(0.5)
        result = ScoreboardV3(
            game_date=game_date.strftime("%m/%d/%Y"),
            league_id="00",
        ).get_normalized_dict()
        games: list[dict] = result["GameHeader"]
        cache_set("schedule", params, games)
        return games
    except Exception as exc:
        logger.warning(
            "nba_api failed for schedule, serving stale cache: %s", exc
        )
        stale = cache_get_stale("schedule", params)
        return stale if stale is not None else []


# ---------------------------------------------------------------------------
# Rest Days
# ---------------------------------------------------------------------------


def get_rest_days(
    team_id: int,
    schedule: list[dict],
    reference_date: Optional[date] = None,
) -> int:
    """Return the number of rest days for *team_id* before *reference_date*.

    Scans *schedule* for the most recent game in which the team participated
    (home or away) that is strictly before *reference_date*.

    Args:
        team_id: NBA team ID (integer).
        schedule: List of game dicts, each with HOME_TEAM_ID, VISITOR_TEAM_ID,
            and GAME_DATE_EST (ISO-8601 date string prefix).
        reference_date: The reference date.  Defaults to ``date.today()``.

    Returns:
        Number of calendar days since the team's last game.
        Returns 0 if the last game was the day before (back-to-back).
        Returns -1 if no previous game is found in *schedule*.
    """
    if reference_date is None:
        reference_date = date.today()

    last_game_date: Optional[date] = None
    for game in schedule:
        home = game.get("HOME_TEAM_ID")
        away = game.get("VISITOR_TEAM_ID")
        if team_id not in (home, away):
            continue

        # GAME_DATE_EST may look like "2024-10-30T00:00:00" or "2024-10-30"
        raw_date = game.get("GAME_DATE_EST", "")
        game_date_str = raw_date[:10]
        try:
            game_date_obj = date.fromisoformat(game_date_str)
        except ValueError:
            continue

        if game_date_obj >= reference_date:
            continue  # Future or same-day game — skip

        if last_game_date is None or game_date_obj > last_game_date:
            last_game_date = game_date_obj

    if last_game_date is None:
        return -1

    return (reference_date - last_game_date).days - 1


# ---------------------------------------------------------------------------
# ELO Tracker
# ---------------------------------------------------------------------------


class EloTracker:
    """FiveThirtyEight-style ELO tracker for NBA teams.

    ELO formula uses margin-of-victory adjusted K-factor to reduce the weight
    of blowouts on rating changes.

    Constants
    ---------
    K_BASE : float
        Base K-factor.
    INITIAL_ELO : float
        Starting ELO for any team not yet in the system.
    HOME_ADVANTAGE : float
        ELO points added to the home team's rating before computing win
        probability (not applied in ``update`` automatically — caller applies
        when constructing matchup features).
    SEASON_RESET_FRACTION : float
        Fraction of the gap from 1300 that is removed on each season reset.
    """

    K_BASE: float = 20.0
    INITIAL_ELO: float = 1300.0
    HOME_ADVANTAGE: float = 100.0
    SEASON_RESET_FRACTION: float = 0.33

    def __init__(self) -> None:
        self._ratings: dict[int, float] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_elo(self, team_id: int) -> float:
        """Return the current ELO for *team_id*, or INITIAL_ELO if unknown.

        Args:
            team_id: NBA team ID.

        Returns:
            ELO rating as a float.
        """
        return self._ratings.get(team_id, self.INITIAL_ELO)

    def get_all_ratings(self) -> dict[int, float]:
        """Return a copy of the current ratings dict.

        Returns:
            Mapping of team_id -> ELO rating.
        """
        return dict(self._ratings)

    @staticmethod
    def expected_outcome(elo_a: float, elo_b: float) -> float:
        """Compute the expected win probability for team A given both ELOs.

        Uses the standard logistic formula:
        ``E_A = 1 / (1 + 10^((elo_b - elo_a) / 400))``

        Args:
            elo_a: ELO rating of team A.
            elo_b: ELO rating of team B.

        Returns:
            Probability (0–1) that team A wins.
        """
        return 1.0 / (1.0 + 10.0 ** ((elo_b - elo_a) / 400.0))

    @staticmethod
    def _elo_k(mov: int, elo_diff: float) -> float:
        """FiveThirtyEight MOV-adjusted K factor.

        ``K = K_BASE * ((mov + 3)^0.8) / (7.5 + 0.006 * |elo_diff|)``

        Args:
            mov: Margin of victory (absolute value of point differential).
            elo_diff: Absolute ELO difference between winner and loser.

        Returns:
            Adjusted K factor.
        """
        return EloTracker.K_BASE * ((mov + 3) ** 0.8) / (7.5 + 0.006 * abs(elo_diff))

    def update(self, winner_id: int, loser_id: int, margin: int) -> None:
        """Update ELO ratings after a game result.

        Args:
            winner_id: NBA team ID of the winning team.
            loser_id: NBA team ID of the losing team.
            margin: Point margin (winner_pts - loser_pts, should be > 0).
        """
        winner_elo = self.get_elo(winner_id)
        loser_elo = self.get_elo(loser_id)

        elo_diff = winner_elo - loser_elo
        k = self._elo_k(abs(margin), elo_diff)
        expected_winner = self.expected_outcome(winner_elo, loser_elo)

        self._ratings[winner_id] = winner_elo + k * (1.0 - expected_winner)
        self._ratings[loser_id] = loser_elo + k * (0.0 - (1.0 - expected_winner))

    def season_reset(self) -> None:
        """Pull all ratings toward 1300 by SEASON_RESET_FRACTION.

        Applied between seasons to reduce the impact of past performance on
        future predictions.  Formula: ``new = old - (old - 1300) * fraction``
        """
        for team_id in list(self._ratings.keys()):
            old = self._ratings[team_id]
            self._ratings[team_id] = old - (old - self.INITIAL_ELO) * self.SEASON_RESET_FRACTION


# ---------------------------------------------------------------------------
# Module-level convenience wrappers (thin pass-throughs)
# ---------------------------------------------------------------------------


def get_team_elos(tracker: EloTracker) -> dict[int, float]:
    """Return a snapshot of all team ELO ratings from *tracker*.

    Args:
        tracker: An :class:`EloTracker` instance.

    Returns:
        Dict mapping team_id -> ELO rating.
    """
    return tracker.get_all_ratings()


def update_elo(tracker: EloTracker, winner_id: int, loser_id: int, margin: int) -> None:
    """Update *tracker* with a game result (thin wrapper for ``tracker.update``).

    Args:
        tracker: An :class:`EloTracker` instance.
        winner_id: NBA team ID of the winning team.
        loser_id: NBA team ID of the losing team.
        margin: Point margin (winner_pts - loser_pts).
    """
    tracker.update(winner_id=winner_id, loser_id=loser_id, margin=margin)
