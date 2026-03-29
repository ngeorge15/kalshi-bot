---
phase: 02-data-pipelines-nba-weather-r2-r3
plan: "05"
subsystem: data-pipeline
tags: [cli, pipeline, integration-tests, nba, weather, nws, noaa]
dependency_graph:
  requires: ["02-02", "02-03", "02-04"]
  provides: ["src/data/pipeline.py", "tests/data/test_integration.py"]
  affects: ["phase-03-prediction-models"]
tech_stack:
  added: ["argparse"]
  patterns: ["CLI entry point", "TDD red-green", "integration test skip pattern"]
key_files:
  created:
    - src/data/pipeline.py
    - tests/data/test_pipeline.py
    - tests/data/test_integration.py
  modified: []
decisions:
  - "pipeline.py iterates STATIONS dict (not a hardcoded list) so adding a 5th station auto-includes it in both historical and daily refresh paths"
  - "Integration tests use @pytest.mark.integration + skip_without_integration fixture — consistent with existing test_integration.py pattern from Phase 01"
  - "test_live_nba_schedule accepts empty list on off-days to avoid false failures during playoffs gap"
  - "test_live_ticker_to_station includes time.sleep(1) after NWS call to respect rate limits"
metrics:
  duration: "2min"
  completed: "2026-03-29"
  tasks_completed: 2
  files_created: 3
  files_modified: 0
---

# Phase 02 Plan 05: CLI Entry Point and Integration Tests Summary

**One-liner:** Argparse CLI with --refresh-history dispatching to NBA+NOAA historical or daily schedule+stats+NWS refresh, plus 4 opt-in live API integration tests covering the Phase 2 working test (KNYC ticker -> lat/lon -> NWS forecast round-trip).

## What Was Built

### `src/data/pipeline.py`

CLI entry point for the data pipeline with:

- `parse_args(argv)` — argparse with `--refresh-history` flag (action="store_true")
- `run_historical_refresh()` — calls `fetch_historical_results()` + `fetch_noaa_historical()` for all 4 stations in `STATIONS`
- `run_daily_refresh()` — calls `get_todays_schedule()`, `get_team_stats()`, and `get_nws_forecast(lat, lon)` for each station
- `main(argv)` — dispatches to historical or daily refresh based on parsed args
- `if __name__ == "__main__": main()` — runnable as `python -m src.data.pipeline`

### `tests/data/test_pipeline.py`

8 unit tests, all passing, all mocked (no live API calls):

- `test_parse_args_refresh_history_flag` — `--refresh-history` sets `args.refresh_history = True`
- `test_parse_args_no_flags` — defaults to `args.refresh_history = False`
- `test_run_historical_refresh_calls_fetch_historical_results` — called exactly once
- `test_run_historical_refresh_calls_fetch_noaa_for_all_4_stations` — KNYC, KMDW, KMIA, KAUS each called once
- `test_run_daily_refresh_calls_get_todays_schedule` — called exactly once
- `test_run_daily_refresh_calls_get_team_stats` — called exactly once
- `test_main_with_refresh_history_dispatches_to_historical_refresh` — correct dispatch
- `test_main_without_flags_dispatches_to_daily_refresh` — correct dispatch

### `tests/data/test_integration.py`

4 integration tests, all `@pytest.mark.integration`, all skipped without `KALSHI_INTEGRATION=true`:

1. `test_live_nba_schedule` — `get_todays_schedule()`, accepts empty list on off-days, validates GAME_ID key exists if non-empty
2. `test_live_nws_forecast` — `get_nws_forecast(40.7794, -73.9692)`, validates non-empty, temperature + shortForecast keys present, -50 < temp < 150
3. `test_live_nba_team_stats` — `get_team_stats()`, validates exactly 30 teams with TEAM_ID, OFF_RATING, DEF_RATING
4. `test_live_ticker_to_station` — KNYC ticker -> lat/lon -> NWS forecast end-to-end (Phase 2 working test)

## Verification

```
python -m pytest tests/data/test_pipeline.py -x -q   # 8 passed
python -m pytest tests/data/test_integration.py -x -q  # 4 skipped
python -m src.data.pipeline --help                    # shows --refresh-history flag
python -m pytest tests/ -x -q                         # 113 passed, 10 skipped
```

## Commits

| Task | Commit | Description |
|------|--------|-------------|
| 1 (RED+GREEN) | a8c71fb | feat(02-05): CLI entry point with --refresh-history flag |
| 2 | ab1e29b | feat(02-05): integration tests for live API connectivity |

## Deviations from Plan

None — plan executed exactly as written.

## Known Stubs

None — all function implementations wire to real data functions. Integration tests are intentionally skipped by default (design, not stubs).

## Self-Check: PASSED

- [x] `src/data/pipeline.py` exists
- [x] `tests/data/test_pipeline.py` exists
- [x] `tests/data/test_integration.py` exists
- [x] Commit a8c71fb exists
- [x] Commit ab1e29b exists
- [x] 113 tests pass, 10 skipped (no regressions)
