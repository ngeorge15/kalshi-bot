"""CLI entry point for the Kalshi Bot data pipeline.

Provides two modes:

* **Historical refresh** (``--refresh-history``): Re-fetches all historical
  NBA game results and NOAA weather data for all 4 stations from scratch.
  Use this on first run or when you want to force a full data reload (D-09).

* **Daily refresh** (default): Fetches today's NBA schedule and current team
  stats, then fetches the latest NWS forecast for each of the 4 weather
  stations.  Run this each day before the trading session.

Usage::

    # One-time historical pull
    python -m src.data.pipeline --refresh-history

    # Daily pre-session refresh
    python -m src.data.pipeline

"""

import argparse
import logging
from typing import Optional

from src.data.nba.history import fetch_historical_results
from src.data.nba.teams import get_team_stats, get_todays_schedule
from src.data.weather.noaa import fetch_noaa_historical
from src.data.weather.nws import get_nws_forecast
from src.data.weather.station_map import STATIONS

logger = logging.getLogger(__name__)


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    """Parse command-line arguments for the data pipeline.

    Args:
        argv: Argument list to parse.  Defaults to ``sys.argv[1:]`` when
            ``None``.

    Returns:
        Parsed :class:`argparse.Namespace` with attributes:
        - ``refresh_history`` (bool): True if ``--refresh-history`` was given.
    """
    parser = argparse.ArgumentParser(description="Kalshi Bot Data Pipeline")
    parser.add_argument(
        "--refresh-history",
        action="store_true",
        help="Force re-fetch of all historical data (NBA results + NOAA weather)",
    )
    return parser.parse_args(argv)


def run_historical_refresh() -> None:
    """Fetch and store all historical NBA results and NOAA weather data.

    Calls:
    - :func:`src.data.nba.history.fetch_historical_results` — stores NBA
      game results for the default 3 seasons to SQLite.
    - :func:`src.data.weather.noaa.fetch_noaa_historical` — stores 3 years
      of daily TMAX/TMIN/PRCP data for each of the 4 Kalshi weather stations
      (KNYC, KMDW, KMIA, KAUS).

    This implements the pull-once strategy (D-09): run with ``--refresh-history``
    on first setup or when you need to force a full data reload.
    """
    logger.info("Starting historical data refresh...")

    count = fetch_historical_results()
    logger.info("NBA historical results: %d rows inserted", count)

    for station_code, station_info in STATIONS.items():
        noaa_count = fetch_noaa_historical(station_code)
        logger.info(
            "NOAA historical [%s - %s]: %d rows inserted",
            station_code,
            station_info["name"],
            noaa_count,
        )

    logger.info("Historical refresh complete")


def run_daily_refresh() -> None:
    """Fetch today's NBA schedule, team stats, and NWS weather forecasts.

    Calls:
    - :func:`src.data.nba.teams.get_todays_schedule` — today's games.
    - :func:`src.data.nba.teams.get_team_stats` — current advanced team stats.
    - :func:`src.data.weather.nws.get_nws_forecast` — NWS 12-hour period
      forecast for each of the 4 Kalshi weather stations.

    Run this once per day (or more frequently for forecast updates) before the
    trading session.
    """
    logger.info("Starting daily data refresh...")

    games = get_todays_schedule()
    logger.info("Today's schedule: %d games", len(games))

    teams = get_team_stats()
    logger.info("Team stats: %d teams", len(teams))

    for station_code, station_info in STATIONS.items():
        periods = get_nws_forecast(station_info["lat"], station_info["lon"])
        logger.info(
            "NWS forecast [%s - %s]: %d periods",
            station_code,
            station_info["name"],
            len(periods),
        )

    logger.info("Daily refresh complete")


def main(argv: Optional[list[str]] = None) -> None:
    """Entry point for the data pipeline CLI.

    Args:
        argv: Argument list to parse.  Defaults to ``sys.argv[1:]`` when
            ``None``.
    """
    args = parse_args(argv)
    if args.refresh_history:
        run_historical_refresh()
    else:
        run_daily_refresh()


if __name__ == "__main__":
    main()
