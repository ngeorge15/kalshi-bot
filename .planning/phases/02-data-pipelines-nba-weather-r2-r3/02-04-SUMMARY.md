---
phase: 02-data-pipelines-nba-weather-r2-r3
plan: "04"
subsystem: data-pipelines
tags: [nba, players, injuries, stats, matchup, espn, nba_api, caching]
dependency_graph:
  requires: ["02-02", "02-03"]
  provides: ["player-stats-module", "injury-report", "matchup-context"]
  affects: ["phase-03-nba-models"]
tech_stack:
  added: []
  patterns:
    - ESPN JSON API for injury data (D-07)
    - nba_api PlayerGameLog for per-game stats and logs
    - nba_api CommonTeamRoster for schedule-driven roster fetch (D-05)
    - nba_api LeagueDashPtDefend for matchup context (R2.9)
    - D-08 confidence flagging (none/low/high) for injury uncertainty
key_files:
  created:
    - src/data/nba/players.py
    - tests/data/test_nba_players.py
  modified: []
key_decisions:
  - ESPN JSON API as primary injury source with nba_api fallback per D-07
  - GTD/Questionable/Day-To-Day players flagged confidence=low, not dropped per D-08
  - Player stats computed as averages over full PlayerGameLog, not a separate endpoint
  - player_roster cached as JSON list (not dict) to survive round-trip (int keys become str in JSON)
  - _fetch_espn_injuries is an isolated standalone function per D-07 for easy replacement
metrics:
  duration: 4min
  completed_date: "2026-03-29"
  tasks_completed: 1
  files_created: 2
---

# Phase 02 Plan 04: NBA Players Module Summary

NBA player data module providing schedule-driven on-demand player stats, per-game averages, game logs for trend detection, ESPN-primary injury reports with confidence flagging, and matchup context via opponent defensive stats.

## Tasks Completed

| Task | Name | Commit | Files |
|------|------|--------|-------|
| RED | Failing tests for players module | 87318d0 | tests/data/test_nba_players.py |
| GREEN | NBA players module implementation | 6ec5c92 | src/data/nba/players.py |

## What Was Built

### `src/data/nba/players.py`

Seven public/private functions:

1. **`_fetch_espn_injuries(session)`** — Isolated ESPN JSON API scraper (D-07). Hits `ESPN_INJURIES_URL`, checks status_code == 200 before parsing, returns list of injury dicts. Returns empty list on any exception so caller handles fallback.

2. **`_fetch_nba_api_injuries(season)`** — nba_api `LeagueInjuryReport` fallback (D-07). Normalizes output to same format as ESPN function.

3. **`get_injury_report(season)`** — ESPN-first, nba_api fallback. Applies D-08 confidence flags: `Out -> none`, `Questionable/Day-To-Day/GTD -> low`, healthy -> `high`. GTD players are never dropped. Cached 3600s (TTL matches injury volatility).

4. **`get_tonights_players(game_date, season)`** — Schedule-driven on-demand roster fetch (D-05). Calls `get_todays_schedule()`, then `CommonTeamRoster` for each unique team. Cached 86400s. Returns `dict[int, dict]` keyed by player_id.

5. **`get_player_stats(player_ids, season)`** — Fetches `PlayerGameLog` and computes per-game averages for PTS, REB, AST, FG3M, MIN, FGM, FGA, FG3A, FTM, FTA, STL, BLK, TOV. Cached per-player, 21600s (6h TTL).

6. **`get_player_game_logs(player_id, n, season)`** — Returns last-n games (default from `config.markets["nba"]["player_trend_games"]` = 5). Returns GAME_DATE, MATCHUP, WL, MIN, PTS, REB, AST, STL, BLK, TOV, FG3M, PLUS_MINUS fields. Cached 21600s.

7. **`get_matchup_context(opponent_team_id, position, season)`** — `LeagueDashPtDefend` lookup for opponent defensive stats vs position. Returns `team_id, team_name, d_fg_pct, freq, d_fga`. Returns None if team not found. Cached 86400s.

All nba_api calls: `time.sleep(0.5)` before each call. All functions: stale cache fallback on API failure with `logger.warning` (D-04).

## Test Coverage

26 test functions in `tests/data/test_nba_players.py`:
- `get_tonights_players`: schedule called, roster per-team, sleep rate limiting, dict structure
- `get_player_stats`: per-game averages, correct math, multi-player sleep
- `get_player_game_logs`: exactly n games, default n from config, namespace/TTL, required fields, stale fallback
- `_fetch_espn_injuries`: standalone callable, list on 200, empty on non-200, empty on exception
- `get_injury_report`: ESPN first, nba_api fallback when ESPN empty, Out->none, Questionable->low, Day-To-Day->low, GTD->low, GTD not filtered
- `get_matchup_context`: returns defensive stats, returns None when not found, sleep before call, stale fallback

## Verification

```
python -m pytest tests/data/test_nba_players.py -x -q  # 26 passed
python -c "from src.data.nba.players import ..."        # OK
python -m pytest tests/data/ -x -q                     # 89 passed
python -m pytest tests/ -x -q                          # 105 passed, 6 skipped
```

## Deviations from Plan

None - plan executed exactly as written.

## Known Stubs

None - all functions are fully implemented with real ESPN/nba_api data sources.

## Self-Check: PASSED
