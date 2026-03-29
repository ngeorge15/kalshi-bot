---
phase: 01-kalshi-api-client-infrastructure-r1
plan: "01"
subsystem: infra
tags: [python, pytest, requests, cryptography, dotenv, rsa, kalshi]

# Dependency graph
requires: []
provides:
  - Project directory structure matching CONVENTIONS.md (src/, src/kalshi/, src/db/, tests/, data/)
  - requirements.txt with pinned deps (requests, cryptography, dotenv, pytest, pytest-mock, responses)
  - .env.example with all required environment variables documented
  - trading_config.json with complete default configuration
  - pytest.ini with integration marker defined
  - tests/conftest.py with RSA key fixture, mock config env fixture, and skip_without_integration
  - tests/test_kalshi_client.py with 22 test stubs (16 unit + 6 integration) ready for implementation
affects:
  - 01-02-PLAN.md  # Config, auth, and DB layer — uses conftest fixtures and directory structure
  - 01-03-PLAN.md  # Kalshi client — fills in the test stubs created here

# Tech tracking
tech-stack:
  added:
    - requests==2.32.3
    - cryptography==43.0.0
    - python-dotenv>=1.0.1
    - pytest==7.4.4
    - pytest-mock==3.15.1
    - responses==0.26.0
  patterns:
    - Wave 0 test scaffolding: all test stubs created before implementation so every subsequent commit has failing tests to fill
    - Session-scoped RSA key fixture for efficient test key generation
    - Integration tests grouped in TestIntegration class with pytestmark gating

key-files:
  created:
    - requirements.txt
    - trading_config.json
    - pytest.ini
    - tests/conftest.py
    - tests/test_kalshi_client.py
    - src/__init__.py
    - src/kalshi/__init__.py
    - src/db/__init__.py
    - tests/__init__.py
  modified:
    - .gitignore (added dist/, build/, .pytest_cache/)
    - .env.example (updated to match CONVENTIONS.md exactly)

key-decisions:
  - "RSA key fixture is session-scoped to generate once and reuse across tests — avoids 2048-bit keygen overhead per test"
  - "Integration tests grouped in TestIntegration class with pytestmark so all can be selected/deselected with -m integration"
  - "skip_without_integration is a standalone fixture (not autouse) so tests explicitly declare their integration dependency"
  - "python-dotenv pinned at >=1.0.1 (plan requirement); installed 1.2.2 (latest) — compatible"

patterns-established:
  - "Wave 0 scaffolding: stub all tests for a phase before implementing any code"
  - "Integration test gating via KALSHI_INTEGRATION=true env var and skip_without_integration fixture"
  - "RSA key generation via cryptography.hazmat.primitives.asymmetric.rsa for test fixtures"

requirements-completed:
  - R1.10

# Metrics
duration: 2min
completed: 2026-03-29
---

# Phase 01 Plan 01: Project Skeleton and Wave 0 Test Scaffolding Summary

**Python project skeleton with pinned deps, complete trading config, RSA key fixture, and 22 pytest stubs covering all Kalshi client behaviors ready for Wave 1 implementation**

## Performance

- **Duration:** ~2 min
- **Started:** 2026-03-29T06:44:09Z
- **Completed:** 2026-03-29T06:46:30Z
- **Tasks:** 2 completed
- **Files modified:** 11

## Accomplishments

- Created complete directory structure matching CONVENTIONS.md (src/, src/kalshi/, src/db/, tests/, data/)
- Installed and pinned all dependencies: requests, cryptography, dotenv, pytest, pytest-mock, responses
- Created trading_config.json with full config schema including kelly_fraction=0.25, risk limits, NBA and weather market settings
- Created pytest.ini with integration marker; conftest.py with session-scoped RSA key generation fixture
- Scaffolded all 22 test stubs (16 unit + 6 integration) that Wave 1 implementation plans will fill in

## Task Commits

Each task was committed atomically:

1. **Task 1: Project skeleton — directories, config files, .gitignore, requirements.txt** - `4d774db` (feat)
2. **Task 2: Wave 0 test scaffolding — conftest.py, test stubs, pytest.ini** - `1545a54` (feat)

## Files Created/Modified

- `requirements.txt` — Pinned deps: requests==2.32.3, cryptography==43.0.0, python-dotenv>=1.0.1, pytest==7.4.4, pytest-mock==3.15.1, responses==0.26.0
- `trading_config.json` — Complete default trading config with risk, markets (NBA + weather), and validation sections
- `pytest.ini` — pytest config with integration marker and testpaths=tests
- `tests/conftest.py` — RSA key fixture (session-scoped), mock_config_env (function-scoped), skip_without_integration
- `tests/test_kalshi_client.py` — 16 unit stubs + 6 integration stubs in TestIntegration class
- `src/__init__.py`, `src/kalshi/__init__.py`, `src/db/__init__.py`, `tests/__init__.py` — Package init files
- `.env.example` — Updated to match CONVENTIONS.md exactly (KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY_PATH, KALSHI_ENV=demo)
- `.gitignore` — Added dist/, build/, .pytest_cache/ entries

## Decisions Made

- RSA key fixture is session-scoped: 2048-bit keygen runs once per test session, key file is reused. Faster than per-test generation.
- Integration tests grouped in `TestIntegration` class with `pytestmark = pytest.mark.integration` so `-m integration` / `-m "not integration"` selects correctly.
- `skip_without_integration` is an explicit fixture (not autouse) — tests must declare they need it, making integration dependency visible.

## Deviations from Plan

None - plan executed exactly as written.

The existing `.gitignore` already had most required entries (`.env`, `*.pem`, `__pycache__/`, `data/`, `.venv/`). Added the missing entries (`dist/`, `build/`, `.pytest_cache/`) rather than overwriting. The existing `.env.example` had correct structure but used `/path/to/` format for the key path; updated to `./kalshi_private_key.pem` per CONVENTIONS.md.

## Issues Encountered

None.

## User Setup Required

None - no external service configuration required for this plan. Integration tests require Kalshi demo credentials (see `.env.example`) but those are already configured per CONTEXT.md.

## Next Phase Readiness

- All test stubs in place — Plan 01-02 (Config, Auth, SQLite schema) can immediately start filling in test implementations
- `test_private_key_path` and `mock_config_env` fixtures are ready for Plan 01-02's KalshiAuth and Config unit tests
- `TestIntegration` stubs are ready for Plan 01-03's client integration tests
- No blockers

---
*Phase: 01-kalshi-api-client-infrastructure-r1*
*Completed: 2026-03-29*
