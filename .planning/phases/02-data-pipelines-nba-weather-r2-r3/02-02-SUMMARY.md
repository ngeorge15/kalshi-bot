---
phase: 02-data-pipelines-nba-weather-r2-r3
plan: "02"
subsystem: nba-data-pipeline
tags: [nba, nba_api, elo, cache, sqlite, teams, history]
dependency_graph:
  requires: ["02-01"]
  provides: ["nba-team-stats", "nba-schedule", "nba-elo", "nba-rest-days", "nba-history"]
  affects: ["03-nba-model"]
tech_stack:
  added: ["nba_api (LeagueDashTeamStats, ScoreboardV3, LeagueGameFinder)"]
  patterns: ["stale-cache-fallback (D-04)", "rate-limit-sleep-0.5s", "INSERT OR IGNORE dedup", "TDD RED-GREEN"]
key_files:
  created:
    - src/data/nba/teams.py
    - src/data/nba/history.py
    - tests/data/test_nba_teams.py
    - tests/data/test_nba_history.py
  modified: []
decisions:
  - "rest_days uses basketball convention: days between games minus 1, so back-to-back = 0"
  - "Database(:memory:) unsupported for tests because Database.execute() re-opens connections; tests use tmp file DB"
  - "EloTracker constants defined as class-level float attributes for clean subclass access"
metrics:
  duration: "5min"
  completed: "2026-03-29"
  tasks_completed: 2
  files_created: 4
  tests_added: 20
---

# Phase 02 Plan 02: NBA Teams and History Pipeline Summary

**One-liner:** NBA team stats/schedule (LeagueDashTeamStats + ScoreboardV3), FiveThirtyEight ELO tracker, rest-day calculation, and 3-season historical results via LeagueGameFinder to SQLite.

## Tasks Completed

| Task | Name | Commit | Key Files |
|------|------|--------|-----------|
| 1 | NBA teams module — stats, schedule, ELO, rest tracking | `0e0d989` | src/data/nba/teams.py, tests/data/test_nba_teams.py |
| 2 | NBA historical results — LeagueGameFinder to SQLite | `bc6c453` | src/data/nba/history.py, tests/data/test_nba_history.py |

## What Was Built

### src/data/nba/teams.py
- `get_team_stats(season)`: LeagueDashTeamStats with Advanced metrics (OFF/DEF/NET_RATING, PACE, W/L/W_PCT), 24h cache, stale fallback on failure
- `get_todays_schedule(game_date)`: ScoreboardV3 (NOT V2 — broken for 2025-26), 24h cache, stale fallback
- `get_rest_days(team_id, schedule, reference_date)`: Calendar days since last game; back-to-back = 0, unknown = -1
- `EloTracker`: FiveThirtyEight MOV-adjusted K-factor, `INITIAL_ELO=1300`, `HOME_ADVANTAGE=100`, `SEASON_RESET_FRACTION=0.33`, `season_reset()` pulls ratings toward 1300 by 1/3

### src/data/nba/history.py
- `fetch_historical_results(db, seasons)`: Fetches 3 seasons (2021-22, 2022-23, 2023-24), parses MATCHUP string ("vs." = home, "@" = away), `INSERT OR IGNORE` for idempotent re-runs
- `get_historical_results(db, season)`: Reads from SQLite, optional season filter

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Fixed off-by-one in test_get_rest_days_returns_correct_days**
- **Found during:** Task 1 GREEN phase
- **Issue:** Test comment said "Oct 30 game, Nov 3 reference = 4 days rest" but basketball convention is rest_days = calendar_diff - 1 (days between games, not including game day). Oct 30 to Nov 3 = 3 rest days (Oct 31, Nov 1, Nov 2). The back-to-back test (returns 0 for day-before game) established the correct convention.
- **Fix:** Updated test expectation from `assert rest == 4` to `assert rest == 3` with clarifying comment
- **Files modified:** tests/data/test_nba_teams.py
- **Commit:** included in `0e0d989`

**2. [Rule 1 - Bug] Fixed Database(:memory:) incompatibility in history tests**
- **Found during:** Task 2 GREEN phase
- **Issue:** `Database(":memory:")` re-opens a new in-memory SQLite connection on every `execute()` call, so schema applied in `__init__` is lost before the first INSERT
- **Fix:** Changed `mem_db` fixture to use `Database(str(tmp_path / "test.db"))` — temp file persists schema across calls
- **Files modified:** tests/data/test_nba_history.py
- **Commit:** included in `bc6c453`

## Test Results

```
20 NBA tests pass (13 teams + 7 history)
Full suite: 79 passed, 6 skipped
```

## Known Stubs

None — all exported functions are fully wired to nba_api and SQLite.

## Self-Check: PASSED
