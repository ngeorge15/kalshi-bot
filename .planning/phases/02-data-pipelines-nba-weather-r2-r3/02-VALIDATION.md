---
phase: 2
slug: data-pipelines-nba-weather-r2-r3
status: draft
nyquist_compliant: true
wave_0_complete: true
created: 2026-03-29
---

# Phase 2 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 7.x |
| **Config file** | pytest.ini or pyproject.toml [tool.pytest.ini_options] |
| **Quick run command** | `pytest tests/data/ -x -q` |
| **Full suite command** | `pytest tests/ -q` |
| **Estimated runtime** | ~15 seconds |

---

## Sampling Rate

- **After every task commit:** Run `pytest tests/data/ -x -q`
- **After every plan wave:** Run `pytest tests/ -q`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 15 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 2-cache-01 | cache | 1 | R2/R3 | unit | `pytest tests/data/test_cache.py -x -q` | TDD | ⬜ pending |
| 2-nba-teams-01 | nba | 1 | R2 | unit | `pytest tests/data/test_nba_teams.py -x -q` | TDD | ⬜ pending |
| 2-nba-players-01 | nba | 1 | R2 | unit | `pytest tests/data/test_nba_players.py -x -q` | TDD | ⬜ pending |
| 2-nba-history-01 | nba | 1 | R2 | unit | `pytest tests/data/test_nba_history.py -x -q` | TDD | ⬜ pending |
| 2-weather-nws-01 | weather | 2 | R3 | unit | `pytest tests/data/test_weather_nws.py -x -q` | TDD | ⬜ pending |
| 2-weather-noaa-01 | weather | 2 | R3 | unit | `pytest tests/data/test_weather_noaa.py -x -q` | TDD | ⬜ pending |
| 2-station-map-01 | weather | 2 | R3 | unit | `pytest tests/data/test_station_map.py -x -q` | TDD | ⬜ pending |
| 2-integration-01 | integration | 3 | R2,R3 | integration | `pytest tests/data/ -m integration -q` | TDD | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

All plans use embedded TDD (tdd="true" on tasks). Test files are created within each implementing task as the RED step of the TDD cycle, not as a separate Wave 0 plan.

- [x] `tests/data/__init__.py` — created in Plan 02-01 (bootstrap)
- [x] `tests/data/conftest.py` — created in Plan 02-01 (bootstrap), provides shared fixtures including `skip_without_integration`
- [x] `tests/data/test_cache.py` — created within Plan 02-01 Task 2 (TDD)
- [x] `tests/data/test_nba_teams.py` — created within Plan 02-02 Task 1 (TDD)
- [x] `tests/data/test_nba_players.py` — created within Plan 02-04 Task 1 (TDD)
- [x] `tests/data/test_nba_history.py` — created within Plan 02-02 Task 2 (TDD)
- [x] `tests/data/test_weather_nws.py` — created within Plan 02-03 Task 2 (TDD)
- [x] `tests/data/test_weather_noaa.py` — created within Plan 02-03 Task 3 (TDD)
- [x] `tests/data/test_station_map.py` — created within Plan 02-03 Task 1 (TDD)
- [x] `tests/data/test_pipeline.py` — created within Plan 02-05 Task 1 (TDD)
- [x] `tests/data/test_integration.py` — created within Plan 02-05 Task 2

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Live NBA schedule pull with real API | R2 | Requires live nba_api call, cannot mock in CI | `KALSHI_INTEGRATION=true pytest tests/data/test_integration.py::test_live_nba_schedule -v` |
| Live NWS forecast for NYC (KNYC) | R3 | Requires live NWS API call | `KALSHI_INTEGRATION=true pytest tests/data/test_integration.py::test_live_nws_forecast -v` |
| Live NBA team stats pull | R2 | Requires live nba_api call | `KALSHI_INTEGRATION=true pytest tests/data/test_integration.py::test_live_nba_team_stats -v` |
| Kalshi ticker to NWS station round-trip | R3 | End-to-end map + live API | `KALSHI_INTEGRATION=true pytest tests/data/test_integration.py::test_live_ticker_to_station -v` |

---

## Validation Sign-Off

- [x] All tasks have `<automated>` verify — TDD tasks create test files in the RED step
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covered by embedded TDD pattern (test files created within implementing tasks)
- [x] No watch-mode flags
- [x] Feedback latency < 15s
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** ready
