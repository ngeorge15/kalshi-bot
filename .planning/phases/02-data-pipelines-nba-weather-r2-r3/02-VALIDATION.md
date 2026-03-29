---
phase: 2
slug: data-pipelines-nba-weather-r2-r3
status: draft
nyquist_compliant: false
wave_0_complete: false
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
| 2-cache-01 | cache | 1 | R2/R3 | unit | `pytest tests/data/test_cache.py -x -q` | ❌ W0 | ⬜ pending |
| 2-nba-teams-01 | nba | 1 | R2 | unit | `pytest tests/data/test_nba_teams.py -x -q` | ❌ W0 | ⬜ pending |
| 2-nba-players-01 | nba | 1 | R2 | unit | `pytest tests/data/test_nba_players.py -x -q` | ❌ W0 | ⬜ pending |
| 2-nba-history-01 | nba | 1 | R2 | unit | `pytest tests/data/test_nba_history.py -x -q` | ❌ W0 | ⬜ pending |
| 2-weather-nws-01 | weather | 2 | R3 | unit | `pytest tests/data/test_weather_nws.py -x -q` | ❌ W0 | ⬜ pending |
| 2-weather-noaa-01 | weather | 2 | R3 | unit | `pytest tests/data/test_weather_noaa.py -x -q` | ❌ W0 | ⬜ pending |
| 2-station-map-01 | weather | 2 | R3 | unit | `pytest tests/data/test_station_map.py -x -q` | ❌ W0 | ⬜ pending |
| 2-integration-01 | integration | 3 | R2,R3 | integration | `pytest tests/data/ -m integration -q` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/data/__init__.py` — test package init
- [ ] `tests/data/test_cache.py` — TTL cache unit test stubs
- [ ] `tests/data/test_nba_teams.py` — team stats, ELO, schedule stubs
- [ ] `tests/data/test_nba_players.py` — player stats, game logs, injury stubs
- [ ] `tests/data/test_nba_history.py` — historical results stubs
- [ ] `tests/data/test_weather_nws.py` — NWS forecast, ensemble stubs
- [ ] `tests/data/test_weather_noaa.py` — NOAA historical stubs
- [ ] `tests/data/test_station_map.py` — ticker→station mapping stubs
- [ ] `tests/data/conftest.py` — shared fixtures (mock responses, sample data)

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Live NBA schedule pull with real API | R2 | Requires live nba_api call, cannot mock in CI | Run `python -c "from data.nba.teams import get_todays_schedule; print(get_todays_schedule())"` |
| Live NWS forecast for NYC (KNYC) | R3 | Requires live NWS API call | Run `python -c "from data.weather.nws import get_point_forecast; print(get_point_forecast(40.7794, -73.9692))"` |
| Kalshi ticker → NWS station round-trip | R3 | End-to-end map validation | Run `python -c "from data.weather.station_map import ticker_to_station; print(ticker_to_station('KXTEMP-XXXX'))"` |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 15s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
