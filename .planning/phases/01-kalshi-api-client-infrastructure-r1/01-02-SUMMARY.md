---
phase: 01-kalshi-api-client-infrastructure-r1
plan: 02
subsystem: auth, database, config
tags: [rsa-pss, requests, cryptography, sqlite, python-dotenv, authbase]

# Dependency graph
requires:
  - phase: 01-01
    provides: project skeleton, tests/conftest.py with RSA key fixture and mock_config_env, pytest configuration
provides:
  - Config singleton loading .env + trading_config.json with demo/prod URL switching
  - KalshiAuth(AuthBase) RSA-PSS request signing with timestamp+METHOD+path (no query string)
  - dollars_to_cents() fixed-point string to integer cents conversion
  - Full SQLite schema (9 tables, schema_version=1) for all 7 phases
  - Database class with schema init, execute/fetchall/fetchone, foreign keys enabled
affects: [01-03-kalshi-client, all-phases-trading, evaluator]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Module-level Config singleton via _load_config() returning None on missing env vars"
    - "KalshiAuth as AuthBase subclass for clean session.auth = auth composition"
    - "RSA private key loaded once at KalshiAuth init (D-03 cache-in-memory)"
    - "dollars_to_cents() uses Decimal for exact fixed-point arithmetic"
    - "Database._get_connection() opens fresh connection per operation (no pooling)"
    - "schema.sql applied via executescript on first connect (idempotent)"

key-files:
  created:
    - src/config.py
    - src/kalshi/auth.py
    - src/db/schema.sql
    - src/db/database.py
  modified:
    - tests/test_kalshi_client.py

key-decisions:
  - "Config(env_override=) parameter avoids importlib.reload() side-effects in tests where load_dotenv bleeds across test boundaries"
  - "test_auth_strips_query_string uses time.time mock + cryptographic signature verification instead of patching Rust extension sign() method (read-only)"
  - "_load_config() returns None on missing env vars so module-level singleton does not crash CI imports; tests instantiate Config() directly after monkeypatching"

patterns-established:
  - "All monetary values in cents (INTEGER) matching Kalshi API convention"
  - "Timestamps as ISO 8601 UTC strings in SQLite"
  - "Booleans as 0/1 integers in SQLite"
  - "conn.row_factory = sqlite3.Row enables dict-style row access"
  - "PRAGMA foreign_keys = ON on every connection"

requirements-completed: [R1.1, R1.2, R1.9, R1.10]

# Metrics
duration: 4min
completed: 2026-03-29
---

# Phase 01 Plan 02: Config, RSA-PSS Auth, and SQLite Schema Summary

**Config singleton loading .env + trading_config.json, KalshiAuth RSA-PSS signing with query-string stripping, and full 9-table SQLite schema for all 7 bot phases**

## Performance

- **Duration:** 4 min
- **Started:** 2026-03-29T06:49:11Z
- **Completed:** 2026-03-29T06:53:13Z
- **Tasks:** 2
- **Files modified:** 5

## Accomplishments
- `src/config.py`: Config singleton reads KALSHI_API_KEY_ID/KALSHI_PRIVATE_KEY_PATH from env, switches demo/prod base URLs, loads trading_config.json risk/markets settings
- `src/kalshi/auth.py`: KalshiAuth(AuthBase) signs every request with RSA-PSS using SHA-256, strips query string from signing path per Kalshi spec; `dollars_to_cents()` converts fixed-point strings using Decimal
- `src/db/schema.sql` + `src/db/database.py`: Full 9-table SQLite schema initialized on first connect, schema_version=1, Database provides execute/fetchall/fetchone with dict row access

## Task Commits

Each task was committed atomically:

1. **TDD RED — Failing tests for Task 1** - `603d7ea` (test)
2. **Task 1: Config singleton + KalshiAuth** - `d4fe80b` (feat)
3. **Task 2: SQLite schema + Database class** - `2079017` (feat)

_Note: TDD task has separate test commit (RED) then implementation commit (GREEN)_

## Files Created/Modified
- `src/config.py` - Config class with DEMO_BASE_URL/PROD_BASE_URL constants, load_dotenv, trading_config.json loading, module-level singleton
- `src/kalshi/auth.py` - KalshiAuth(AuthBase) with RSA-PSS signing; dollars_to_cents() helper
- `src/db/schema.sql` - 9 CREATE TABLE IF NOT EXISTS statements, 8 indexes, INSERT OR IGNORE schema_version=1
- `src/db/database.py` - Database class: _init_schema, get_schema_version, execute, fetchall, fetchone
- `tests/test_kalshi_client.py` - 7 unit tests wired up: test_auth_signature, test_auth_headers, test_auth_strips_query_string, test_base_url_demo, test_base_url_prod, test_missing_env_var, test_dollars_to_cents

## Decisions Made
- Used `Config(env_override=)` parameter instead of `importlib.reload()` in URL tests — reload re-runs `load_dotenv()` which re-populates env vars from `.env`, causing `test_missing_env_var` to fail silently
- `test_auth_strips_query_string` patches `time.time` to a fixed value and then verifies the signature cryptographically against the expected path-only message, rather than patching the Rust extension's read-only `sign()` method
- `_load_config()` wraps singleton creation in try/except so `config = None` when env vars are absent at import time; tests that need a config call `Config()` directly after monkeypatching env vars

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Fixed test_auth_strips_query_string approach for Rust extension type**
- **Found during:** Task 1 (GREEN phase — running tests)
- **Issue:** Cryptography library's `RSAPrivateKey` is a Rust extension; `.sign` attribute is read-only, cannot be monkey-patched directly or via `patch.object` (raises `AttributeError`)
- **Fix:** Changed test to mock `time.time` to a fixed value, then verify the RSA-PSS signature cryptographically against the expected message (path without query string). This proves the signing string used the correct format without needing to intercept the internal sign call.
- **Files modified:** tests/test_kalshi_client.py
- **Verification:** test_auth_strips_query_string passes
- **Committed in:** d4fe80b (Task 1 feat commit)

**2. [Rule 1 - Bug] Fixed test_missing_env_var failing due to importlib.reload + load_dotenv interaction**
- **Found during:** Task 1 (GREEN phase — running all 7 target tests together)
- **Issue:** `test_base_url_demo` and `test_base_url_prod` used `importlib.reload(config_module)` which re-ran `load_dotenv(override=False)`. With `.env` present, this re-populated `KALSHI_API_KEY_ID` before `test_missing_env_var` ran, causing it to not raise `KeyError`.
- **Fix:** Removed `importlib.reload()` from both URL tests; used `Config(env_override="demo"|"production")` to select the URL without re-running module-level code. `test_missing_env_var` calls `Config()` directly after `monkeypatch.delenv()`.
- **Files modified:** tests/test_kalshi_client.py
- **Verification:** All 7 target tests pass in order; `test_missing_env_var` reliably raises `KeyError`
- **Committed in:** d4fe80b (Task 1 feat commit)

---

**Total deviations:** 2 auto-fixed (both Rule 1 bugs in test implementation)
**Impact on plan:** Both fixes necessary for correct test behavior. Production code unchanged. No scope creep.

## Issues Encountered
- Rust-based cryptography extension types have read-only attributes; standard mock patterns don't apply — resolved by testing behavior cryptographically rather than by intercepting internal calls.

## User Setup Required
None — no external service configuration required for these modules. Credentials are loaded from `.env` which already exists from Plan 01-01.

## Next Phase Readiness
- `Config`, `KalshiAuth`, and `Database` are all importable and tested
- Plan 03 (Kalshi API client) can import `from src.config import config` and `from src.kalshi.auth import KalshiAuth` directly
- Database is ready for trade/prediction/outcome storage from Phase 4 onwards
- No blockers

## Known Stubs
None — all functionality is implemented and tested.

---
*Phase: 01-kalshi-api-client-infrastructure-r1*
*Completed: 2026-03-29*

## Self-Check: PASSED

- FOUND: src/config.py
- FOUND: src/kalshi/auth.py
- FOUND: src/db/schema.sql
- FOUND: src/db/database.py
- FOUND: tests/test_kalshi_client.py
- FOUND: .planning/phases/01-kalshi-api-client-infrastructure-r1/01-02-SUMMARY.md
- FOUND: commit 603d7ea (test(01-02): TDD RED)
- FOUND: commit d4fe80b (feat(01-02): config + auth)
- FOUND: commit 2079017 (feat(01-02): schema + database)
