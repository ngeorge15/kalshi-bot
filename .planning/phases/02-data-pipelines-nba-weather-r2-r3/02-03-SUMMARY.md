---
phase: 02-data-pipelines-nba-weather-r2-r3
plan: "03"
subsystem: data
tags: [nws, noaa, weather, requests, sqlite, cache, tdd]

# Dependency graph
requires:
  - phase: 02-data-pipelines-nba-weather-r2-r3/02-01
    provides: cache.py with cache_get/cache_set/cache_get_stale and file-based TTL cache
  - phase: 01-kalshi-api-client-infrastructure-r1
    provides: Database class with execute/fetchall, schema.sql with noaa_daily_weather table, requests session pattern

provides:
  - STATIONS dict with KNYC/KMDW/KMIA/KAUS lat/lon and GHCND IDs
  - ticker_to_station and get_station_coords helpers
  - NWS two-step /points -> /gridpoints forecast pipeline with caching
  - NWS griddata for temperature and probabilityOfPrecipitation time series
  - NWS multi-model comparison (period + hourly forecasts)
  - Forecast delta detection for temperature change signals
  - NOAA CDO historical daily data pipeline with SQLite persistence
  - TMAX/TMIN/PRCP 3-year history per station with pagination and deduplication

affects:
  - phase-03-models: consumes station_map, nws, noaa for weather feature engineering
  - phase-04-trading: forecast delta signals feed into position sizing confidence

# Tech tracking
tech-stack:
  added: []
  patterns:
    - NWS two-step resolve pattern (_resolve_gridpoint cached 24h, forecast cached 1h)
    - Module-level session singleton (_SESSION) for HTTP connection reuse
    - Stale-fallback: cache_get_stale on exception + logger.warning (D-04)
    - Pull-once for historical data: cache_get short-circuits re-fetch (D-09)
    - RuntimeError for config errors raised before entering try/except (not swallowed)
    - TDD: test files committed RED then implementation committed GREEN

key-files:
  created:
    - src/data/weather/station_map.py
    - src/data/weather/nws.py
    - src/data/weather/noaa.py
    - tests/data/test_station_map.py
    - tests/data/test_weather_nws.py
    - tests/data/test_weather_noaa.py
  modified: []

key-decisions:
  - "RuntimeError for missing NOAA_API_TOKEN is validated before try/except so it propagates to caller — config errors are not transient failures"
  - "NWS gridpoint resolution cached 86400s (24h) because grid cell assignments never change; forecast data cached 3600s"
  - "_resolve_gridpoint used internally and cached separately from forecast so hourly and period forecasts share the same gridpoint lookup"

patterns-established:
  - "Stale fallback pattern: try HTTP -> except -> logger.warning -> cache_get_stale -> return [] or {} or 0"
  - "Config error pre-validation: call _get_noaa_token() before entering try block so RuntimeError propagates"

requirements-completed:
  - R3.1
  - R3.2
  - R3.3
  - R3.4
  - R3.5
  - R3.6

# Metrics
duration: 5min
completed: 2026-03-29
---

# Phase 02 Plan 03: Weather Data Pipeline Summary

**NWS and NOAA weather pipeline with station mapping, 12-hour/hourly/griddata forecasts, multi-model comparison, forecast delta detection, and NOAA 3-year historical daily data persisted to SQLite**

## Performance

- **Duration:** ~5 min
- **Started:** 2026-03-29T22:40:22Z
- **Completed:** 2026-03-29T22:45:00Z
- **Tasks:** 2 (both TDD with RED + GREEN commits)
- **Files modified:** 6

## Accomplishments

- Station mapping for KNYC/KMDW/KMIA/KAUS with exact lat/lon coordinates and GHCND IDs as specified in RESEARCH.md
- Full NWS pipeline: _resolve_gridpoint -> get_nws_forecast/get_nws_hourly/get_nws_griddata with cache and stale fallback
- compare_forecast_models and detect_forecast_delta for uncertainty analysis and trading signal generation
- NOAA CDO historical pipeline with pagination (offset-based), TMAX/TMIN÷10 conversion, INSERT OR IGNORE deduplication
- 35 tests across 3 files — all pass, no regressions

## Task Commits

Each task was committed atomically (TDD: RED commit then GREEN commit):

1. **Task 1 (RED): Failing tests for station_map and NWS** - `21e0016` (test)
2. **Task 1 (GREEN): station_map.py and nws.py implementation** - `e524cfc` (feat)
3. **Task 2 (RED): Failing tests for NOAA pipeline** - `79d7012` (test)
4. **Task 2 (GREEN): noaa.py implementation** - `ef360ce` (feat)

**Plan metadata:** (to be committed)

_Note: TDD tasks have two commits each (test → feat)_

## Files Created/Modified

- `src/data/weather/station_map.py` — STATIONS dict with 4 cities; ticker_to_station and get_station_coords helpers
- `src/data/weather/nws.py` — NWS session, gridpoint resolution, forecast/hourly/griddata, compare and delta functions
- `src/data/weather/noaa.py` — NOAA CDO fetch with pagination, TMAX/TMIN division, SQLite upsert, date-range query
- `tests/data/test_station_map.py` — 11 tests covering all STATIONS entries and helpers
- `tests/data/test_weather_nws.py` — 14 tests covering all NWS functions, cache, stale fallback, User-Agent
- `tests/data/test_weather_noaa.py` — 10 tests covering writes, TMAX/TMIN conversion, pagination, dedup, stale fallback

## Decisions Made

- **RuntimeError pre-validation:** `_get_noaa_token()` is called before the `try/except` block so missing token raises immediately instead of being swallowed by the stale-fallback handler. Config errors are not transient network errors.
- **gridpoint cached 24h separately:** `_resolve_gridpoint` has its own cache namespace (`nws_gridpoint`) at 86400s TTL, since grid cell assignments are permanent. Forecast caches at 3600s on top of this.
- **Stale cache for NWS `get_nws_forecast`:** The stale entry is keyed on `{"lat": ..., "lon": ...}` to match the fresh-fetch params exactly, ensuring stale fallback can find it.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Fixed stale fallback test to use expired cache entry**
- **Found during:** Task 1 (NWS stale fallback test)
- **Issue:** Test seeded a fresh cache entry then expected a warning log, but `get_nws_forecast` returned from fresh cache before hitting HTTP — so no warning was ever issued
- **Fix:** Updated test to write an expired cache file (cached_at 7200s in past, TTL=3600s) so the fresh check misses, the HTTP call fails, and the stale fallback is exercised
- **Files modified:** tests/data/test_weather_nws.py
- **Verification:** 25/25 station map + NWS tests pass
- **Committed in:** e524cfc (Task 1 GREEN commit, test file updated)

**2. [Rule 1 - Bug] RuntimeError for missing NOAA token was swallowed by except block**
- **Found during:** Task 2 (NOAA token error test)
- **Issue:** `_get_session()` called inside `try` block, so `RuntimeError("NOAA_API_TOKEN not set")` was caught and converted to a stale-fallback warning instead of propagating to caller
- **Fix:** Added `_get_noaa_token()` call before the `try` block so token errors always raise
- **Files modified:** src/data/weather/noaa.py
- **Verification:** `test_fetch_noaa_historical_missing_token_raises` passes
- **Committed in:** ef360ce (Task 2 GREEN commit)

---

**Total deviations:** 2 auto-fixed (both Rule 1 - Bug)
**Impact on plan:** Both fixes were required for correctness. Test fix ensures D-04 behavior is actually tested; token fix ensures callers get actionable errors on misconfiguration.

## Issues Encountered

- Pre-existing failure in `tests/data/test_nba_history.py::test_fetch_historical_results_writes_to_db` (no such table: nba_game_results) — confirmed pre-existing before our changes, out of scope, logged for deferred items.

## User Setup Required

None — weather API calls are mocked in tests. Live use requires:
- `NOAA_API_TOKEN` — free token from https://www.ncdc.noaa.gov/cdo-web/token
- `NWS_CONTACT_EMAIL` (optional) — defaults to `kalshi-bot@example.com` for NWS User-Agent

## Next Phase Readiness

- Weather feature engineering in Phase 3 can import `get_nws_forecast`, `get_nws_griddata`, `compare_forecast_models`, `detect_forecast_delta` from `src.data.weather.nws`
- Historical baseline via `get_noaa_historical` available for temperature bracket calibration
- Forecast delta signals ready for position-sizing confidence adjustments

---
*Phase: 02-data-pipelines-nba-weather-r2-r3*
*Completed: 2026-03-29*

## Self-Check: PASSED

All 7 expected files present. All 4 task commits found (21e0016, e524cfc, 79d7012, ef360ce).
