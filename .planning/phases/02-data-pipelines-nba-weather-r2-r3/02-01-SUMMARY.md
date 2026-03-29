---
phase: 02-data-pipelines-nba-weather-r2-r3
plan: 01
subsystem: data
tags: [cache, schema, config, nba_api, foundation]
dependency_graph:
  requires: []
  provides:
    - src/data/cache.py (TTL file-based cache with stale fallback)
    - src/db/schema.sql v2 (nba_game_results, noaa_daily_weather tables)
    - trading_config.json cache section (TTL defaults)
    - nba_api 1.11.4 installed
  affects:
    - src/data/nba/ (NBA pipeline will import cache)
    - src/data/weather/ (weather pipeline will import cache)
    - src/config.py (config.cache exposed)
tech_stack:
  added:
    - nba_api==1.11.4 (NBA data via free public API)
  patterns:
    - File-based TTL cache with MD5-hashed keys (hashlib.md5 of sorted JSON params)
    - Stale fallback pattern: cache_get_stale returns expired value when upstream unavailable
    - CACHE_DIR module attribute allows test isolation via monkeypatch
key_files:
  created:
    - src/data/__init__.py
    - src/data/cache.py
    - src/data/nba/__init__.py
    - src/data/weather/__init__.py
    - tests/data/__init__.py
    - tests/data/conftest.py
    - tests/data/test_cache.py
  modified:
    - src/config.py (added self.cache)
    - src/db/schema.sql (added nba_game_results, noaa_daily_weather, schema v2)
    - trading_config.json (added cache section, player_trend_games)
    - .env.example (added NOAA_API_TOKEN)
    - requirements.txt (added nba_api==1.11.4)
    - .gitignore (anchored data/ pattern to project root)
decisions:
  - CACHE_DIR is a module-level attribute (not a function arg) so tests can monkeypatch it without modifying function signatures
  - Cache key uses md5(json.dumps(params, sort_keys=True)) — deterministic, collision-resistant for reasonable param sizes
  - cache_get_stale ignores TTL entirely (reads value regardless of cached_at) — D-04 stale fallback
  - _get_default_ttl reads from config.cache["ttl_seconds"] if config is available; falls back to hardcoded _DEFAULT_TTL dict
  - .gitignore data/ pattern anchored to /data/ so src/data/ and tests/data/ are tracked properly
metrics:
  duration: 3min
  completed: "2026-03-29T22:37:54Z"
  tasks_completed: 1
  files_created: 7
  files_modified: 6
---

# Phase 02 Plan 01: Cache Foundation + Schema v2 Summary

**One-liner:** File-based TTL cache (MD5 keys, stale fallback) with nba_api install, schema v2 tables, and config/env plumbing for data pipelines.

## What Was Built

A shared foundation enabling both the NBA and weather data pipelines:

1. **`src/data/cache.py`** — Three public functions:
   - `cache_set(namespace, params, value)` — writes `{"cached_at": timestamp, "value": value}` JSON, creates dirs with `mkdir(parents=True, exist_ok=True)`
   - `cache_get(namespace, params, ttl_seconds=None)` — returns value if within TTL, else None
   - `cache_get_stale(namespace, params)` — returns last known value regardless of TTL (upstream-down fallback)
   - Cache keys: `data/cache/{namespace}/{md5_of_sorted_params}.json`
   - `CACHE_DIR` module attribute for test isolation via monkeypatch

2. **`src/db/schema.sql` v2** — Two new tables:
   - `nba_game_results` (game_id, dates, team IDs, scores, season) with date/season indexes
   - `noaa_daily_weather` (station_id, station_code, date, tmax_f, tmin_f, prcp_in) with station/date unique constraint + index
   - `INSERT OR IGNORE INTO schema_version (version) VALUES (2)`

3. **Config updates:**
   - `trading_config.json`: `cache.ttl_seconds` section (nws_forecast: 3600, player_stats: 21600, team_stats: 86400, schedule: 86400, historical: 604800); `markets.nba.player_trend_games: 5`
   - `src/config.py`: `self.cache = self._trading.get("cache", {})` exposed on Config

4. **Dependency:** `nba_api==1.11.4` installed and added to `requirements.txt`

5. **Test scaffolding:** 8 cache unit tests covering all behaviors; `tests/data/conftest.py` with `cache_dir` and `skip_without_integration` fixtures

## Verification Results

- `python -m pytest tests/data/test_cache.py -x -q` — **8 passed**
- `python -m pytest tests/ -x -q` — **24 passed, 6 skipped** (no regressions from Phase 1)
- `python -c "from src.data.cache import cache_get, cache_set, cache_get_stale; print('OK')"` — OK
- `python -c "import nba_api; print(nba_api.__version__)"` — 1.11.4
- Schema v2 confirmed via file-based DB (get_schema_version() returns 2)

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Fixed .gitignore blocking src/data and tests/data from being tracked**
- **Found during:** RED phase commit — `git add` failed with "paths are ignored" error
- **Issue:** `.gitignore` had `data/` (unanchored) which matched `src/data/` and `tests/data/`
- **Fix:** Changed `data/` to `/data/` to anchor the pattern to the project root, preserving the intent of ignoring the runtime data/ directory only
- **Files modified:** `.gitignore`
- **Commit:** `70eec3d`

## Known Stubs

None — all functions are fully implemented and tested.

## Self-Check: PASSED

- src/data/cache.py: FOUND
- src/data/__init__.py: FOUND
- tests/data/test_cache.py: FOUND
- commit 70eec3d (RED): FOUND
- commit d7717e4 (GREEN): FOUND
