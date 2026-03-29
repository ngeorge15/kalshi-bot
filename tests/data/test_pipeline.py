"""Unit tests for src/data/pipeline.py — CLI arg parsing and dispatch logic.

All data functions are mocked. No live API calls occur here.
"""

import argparse
from unittest.mock import MagicMock, call, patch

import pytest


# ---------------------------------------------------------------------------
# parse_args
# ---------------------------------------------------------------------------


def test_parse_args_refresh_history_flag():
    """--refresh-history sets args.refresh_history = True."""
    from src.data.pipeline import parse_args

    args = parse_args(["--refresh-history"])
    assert args.refresh_history is True


def test_parse_args_no_flags():
    """With no flags, args.refresh_history defaults to False."""
    from src.data.pipeline import parse_args

    args = parse_args([])
    assert args.refresh_history is False


# ---------------------------------------------------------------------------
# run_historical_refresh
# ---------------------------------------------------------------------------


def test_run_historical_refresh_calls_fetch_historical_results():
    """run_historical_refresh() calls fetch_historical_results() exactly once."""
    from src.data.pipeline import run_historical_refresh

    with (
        patch("src.data.pipeline.fetch_historical_results") as mock_history,
        patch("src.data.pipeline.fetch_noaa_historical") as mock_noaa,
    ):
        mock_history.return_value = 42
        mock_noaa.return_value = 10
        run_historical_refresh()

    mock_history.assert_called_once()


def test_run_historical_refresh_calls_fetch_noaa_for_all_4_stations():
    """run_historical_refresh() calls fetch_noaa_historical() for KNYC, KMDW, KMIA, KAUS."""
    from src.data.pipeline import run_historical_refresh

    with (
        patch("src.data.pipeline.fetch_historical_results") as mock_history,
        patch("src.data.pipeline.fetch_noaa_historical") as mock_noaa,
    ):
        mock_history.return_value = 0
        mock_noaa.return_value = 5
        run_historical_refresh()

    assert mock_noaa.call_count == 4
    called_station_codes = {c.args[0] for c in mock_noaa.call_args_list}
    assert called_station_codes == {"KNYC", "KMDW", "KMIA", "KAUS"}


# ---------------------------------------------------------------------------
# run_daily_refresh
# ---------------------------------------------------------------------------


def test_run_daily_refresh_calls_get_todays_schedule():
    """run_daily_refresh() calls get_todays_schedule() exactly once."""
    from src.data.pipeline import run_daily_refresh

    with (
        patch("src.data.pipeline.get_todays_schedule") as mock_schedule,
        patch("src.data.pipeline.get_team_stats") as mock_stats,
        patch("src.data.pipeline.get_nws_forecast") as mock_forecast,
    ):
        mock_schedule.return_value = []
        mock_stats.return_value = []
        mock_forecast.return_value = []
        run_daily_refresh()

    mock_schedule.assert_called_once()


def test_run_daily_refresh_calls_get_team_stats():
    """run_daily_refresh() calls get_team_stats() exactly once."""
    from src.data.pipeline import run_daily_refresh

    with (
        patch("src.data.pipeline.get_todays_schedule") as mock_schedule,
        patch("src.data.pipeline.get_team_stats") as mock_stats,
        patch("src.data.pipeline.get_nws_forecast") as mock_forecast,
    ):
        mock_schedule.return_value = []
        mock_stats.return_value = []
        mock_forecast.return_value = []
        run_daily_refresh()

    mock_stats.assert_called_once()


# ---------------------------------------------------------------------------
# main dispatch
# ---------------------------------------------------------------------------


def test_main_with_refresh_history_dispatches_to_historical_refresh():
    """main() with --refresh-history calls run_historical_refresh(), not run_daily_refresh()."""
    from src.data.pipeline import main

    with (
        patch("src.data.pipeline.run_historical_refresh") as mock_hist,
        patch("src.data.pipeline.run_daily_refresh") as mock_daily,
    ):
        main(["--refresh-history"])

    mock_hist.assert_called_once()
    mock_daily.assert_not_called()


def test_main_without_flags_dispatches_to_daily_refresh():
    """main() with no flags calls run_daily_refresh(), not run_historical_refresh()."""
    from src.data.pipeline import main

    with (
        patch("src.data.pipeline.run_historical_refresh") as mock_hist,
        patch("src.data.pipeline.run_daily_refresh") as mock_daily,
    ):
        main([])

    mock_daily.assert_called_once()
    mock_hist.assert_not_called()
