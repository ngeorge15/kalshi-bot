---
phase: 01-kalshi-api-client-infrastructure-r1
verified: 2026-03-28T00:00:00Z
status: passed
score: 11/11 must-haves verified
re_verification: false
gaps: []
human_verification:
  - test: "Run integration tests with KALSHI_INTEGRATION=true against a live Kalshi demo account"
    expected: "auth, NBA game discovery, orderbook pull, place+cancel order, balance and positions all return real data"
    why_human: "Integration stubs are pass-only; live API credentials required"
---

# Phase 01: Kalshi API Client Infrastructure Verification Report

**Phase Goal:** Build the Kalshi API client with RSA-PSS auth, market/event discovery across all categories (NBA games, props, futures; weather temperature, precip, severe), orderbook retrieval, order management, and portfolio tracking. Set up project skeleton: directory structure per CONVENTIONS.md, config management, .env loading, SQLite schema, GitHub remote, .gitignore, requirements.txt. All development targets demo API.

**Verified:** 2026-03-28
**Status:** PASSED
**Re-verification:** No — initial verification

---

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | pytest collects all 22 test stubs without error | VERIFIED | `22 tests collected` confirmed with 0 errors |
| 2 | Project directory structure matches CONVENTIONS.md | VERIFIED | `src/`, `src/kalshi/`, `src/db/`, `tests/`, all `__init__.py` present |
| 3 | requirements.txt lists all pinned dependencies | VERIFIED | requests==2.32.3, cryptography==43.0.0, pytest==7.4.4, etc. all present |
| 4 | .env.example documents all required environment variables | VERIFIED | KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY_PATH, KALSHI_ENV=demo present |
| 5 | Config loads .env and trading_config.json and exposes base_url, api_key_id, private_key_path | VERIFIED | `src/config.py` confirmed with `load_dotenv`, `trading_config.json` loading, demo/prod URL switching |
| 6 | KalshiAuth signs requests with RSA-PSS using timestamp_ms + METHOD + path (no query string) | VERIFIED | `test_auth_signature`, `test_auth_headers`, `test_auth_strips_query_string` all PASS |
| 7 | SQLite schema contains all 8 tables for all 7 phases plus schema_version | VERIFIED | 9 tables confirmed: trades, predictions, outcomes, daily_pnl, model_versions, improvements, evaluator_runs, holdout_results, schema_version |
| 8 | Database.py initializes schema on first connect and verifies schema version | VERIFIED | `get_schema_version()` returns 1; auto-creates parent dirs |
| 9 | KalshiClient can discover markets by series ticker with pagination | VERIFIED | `test_market_discovery_pagination` PASSES — cursor loop confirmed |
| 10 | KalshiClient can pull orderbook and parse _dollars format to cents | VERIFIED | `test_orderbook_parse` PASSES — (65, 10.0), (60, 5.0), (35, 8.0) asserted |
| 11 | KalshiClient can place limit order, cancel, get balance/positions/fills | VERIFIED | All 9 client-related unit tests PASS |

**Score:** 11/11 truths verified

---

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `tests/conftest.py` | RSA key fixture, mock config fixture | VERIFIED | Contains `test_private_key_path`, `mock_config_env`, `skip_without_integration` |
| `tests/test_kalshi_client.py` | All unit + integration test stubs | VERIFIED | 16 unit tests pass; 6 integration stubs in `TestIntegration` class |
| `pytest.ini` | pytest config with integration marker | VERIFIED | Contains `integration: marks tests as requiring live Kalshi demo API` |
| `trading_config.json` | Default trading configuration | VERIFIED | `kelly_fraction: 0.25`, `max_daily_loss_cents: -10000`, keys: environment/markets/risk/validation |
| `.env.example` | Environment variable template | VERIFIED | All required vars documented |
| `src/config.py` | Config singleton with demo/prod URL switching | VERIFIED | `DEMO_BASE_URL`, `PROD_BASE_URL`, `load_dotenv`, `trading_config.json`, `_load_config()` guard |
| `src/kalshi/auth.py` | RSA-PSS request signing as AuthBase | VERIFIED | `KalshiAuth(AuthBase)`, all three KALSHI-ACCESS-* headers, `padding.PSS.MAX_LENGTH`, `dollars_to_cents` |
| `src/db/schema.sql` | Full SQLite schema for all phases | VERIFIED | 9 `CREATE TABLE IF NOT EXISTS` statements, 8 indexes, `INSERT OR IGNORE INTO schema_version` |
| `src/db/database.py` | Data access layer with schema versioning | VERIFIED | `Database`, `_init_schema`, `get_schema_version`, `conn.row_factory = sqlite3.Row`, `schema.sql` reference |
| `src/kalshi/client.py` | Full Kalshi API client (min 150 lines) | VERIFIED | 407 lines; all 13 methods present including `from_config` classmethod |

---

### Key Link Verification

| From | To | Via | Status | Details |
|------|-----|-----|--------|---------|
| `tests/conftest.py` | `tests/test_kalshi_client.py` | pytest fixture injection | WIRED | `test_private_key_path` injected into all 16 unit tests that create KalshiAuth/KalshiClient |
| `src/config.py` | `.env` | `load_dotenv()` | WIRED | `load_dotenv(override=False)` at module level (line 23) |
| `src/config.py` | `trading_config.json` | `json.load()` | WIRED | `_PROJECT_ROOT / "trading_config.json"` opened with context manager |
| `src/kalshi/auth.py` | `cryptography` library | RSA-PSS signing | WIRED | `padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH)` |
| `src/db/database.py` | `src/db/schema.sql` | `executescript` on init | WIRED | `Path(__file__).parent / "schema.sql"` loaded in `_init_schema` |
| `src/kalshi/client.py` | `src/kalshi/auth.py` | `KalshiAuth` passed to Session | WIRED | `from src.kalshi.auth import KalshiAuth, dollars_to_cents`; `self._auth = KalshiAuth(...)` |
| `src/kalshi/client.py` | `src/config.py` | `Config` for `base_url` | WIRED | `from_config(cls, config)` uses `config.base_url`, `config.api_key_id`, `config.private_key_path` |
| `src/kalshi/client.py` | Kalshi REST API | `requests.Session` with retry | WIRED | `HTTPAdapter(max_retries=Retry(...))` mounted on `"https://"` |

---

### Data-Flow Trace (Level 4)

Not applicable for this phase. Artifacts are API client libraries, auth modules, and database schema — not UI rendering components. Data flows are verified structurally via key links and behavioral unit tests above.

---

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| All 16 unit tests pass | `pytest tests/test_kalshi_client.py -v -m "not integration"` | 16 passed, 6 deselected in 1.43s | PASS |
| pytest collects 22 tests | `pytest --collect-only -q` | 22 tests collected in 0.14s | PASS |
| All modules import cleanly | `python -c "from src.kalshi.client import KalshiClient; ..."` | "all imports ok" | PASS |
| Database creates all 9 tables at schema_version 1 | `Database(tmp_path)` + `get_schema_version()` | schema_version=1, 9 tables | PASS |
| Database auto-creates parent directory | `Database("subdir/test.db")` | parent dir and db file created | PASS |

---

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|------------|------------|-------------|--------|---------|
| R1.1 | 01-02 | RSA-PSS authentication (timestamp + method + path) | SATISFIED | `KalshiAuth.__call__` implements exact signing string; `test_auth_signature`, `test_auth_strips_query_string` pass |
| R1.2 | 01-02 | Configurable base URL: demo vs production | SATISFIED | `DEMO_BASE_URL`/`PROD_BASE_URL` constants in `Config`; `test_base_url_demo`, `test_base_url_prod` pass. Note: REQUIREMENTS.md has stale production URL (`api.elections.kalshi.com`) — RESEARCH.md documents the correct current URL (`trading-api.kalshi.com`) used in implementation |
| R1.3 | 01-03 | Market discovery with series ticker filtering | SATISFIED | `get_markets_by_series`, `get_market` implemented; cursor pagination tested |
| R1.4 | 01-03 | Event discovery with bracket structure | SATISFIED | `get_event` implemented; `test_get_event` passes |
| R1.5 | 01-03 | Orderbook retrieval with _dollars parsing | SATISFIED | `get_orderbook` converts `yes_dollars`/`no_dollars` to cent tuples; `test_orderbook_parse` passes |
| R1.6 | 01-03 | Order placement — limit orders with side/price/quantity | SATISFIED | `place_limit_order` with `post_only=True` default; `test_place_order_payload` verifies full JSON payload |
| R1.7 | 01-03 | Order management — cancel, status check, fills | SATISFIED | `cancel_order`, `get_orders`, `get_fills` implemented; `test_cancel_order`, `test_get_fills` pass |
| R1.8 | 01-03 | Portfolio state — positions, balance, settlements | SATISFIED | `get_balance`, `get_positions`, `get_settlements` implemented; `test_get_balance`, `test_get_positions` pass |
| R1.9 | 01-02, 01-03 | Rate limiting with exponential backoff | SATISFIED | `Retry(total=3, backoff_factor=1, status_forcelist=[429,500,502,503,504])` mounted on session; `test_retry_config` asserts `total==3`, `backoff_factor==1` |
| R1.10 | 01-01, 01-02 | Credentials from environment variables / .env, never hardcoded | SATISFIED | `os.environ["KALSHI_API_KEY_ID"]` raises `KeyError` if missing; `_load_config()` guard; `.env.example` documents vars; `test_missing_env_var` passes |

**Orphaned requirements check:** No phase-1 requirements in REQUIREMENTS.md are unaccounted for by the three plans.

---

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| `tests/test_kalshi_client.py` | 379-400 | Integration test stubs (`pass` body) | Info | Expected — these are Wave 0 stubs awaiting live credentials; gated by `skip_without_integration` fixture so they cannot run accidentally |

No blockers or warnings found. The integration stubs are intentional scaffolding and clearly gated.

---

### Human Verification Required

#### 1. Live API Integration Tests

**Test:** Set `KALSHI_INTEGRATION=true` and configure a real Kalshi demo account's credentials in `.env` (KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY_PATH). Run `pytest tests/ -v -m integration`.

**Expected:** All 6 integration tests pass:
- `test_integration_auth` — `GET /exchange/status` returns 200
- `test_integration_discover_nba_games` — `get_markets_by_series("KXNBAGAME")` returns list with at least one market
- `test_integration_orderbook` — `get_orderbook(ticker)` returns non-empty `yes_bids` or `no_bids`
- `test_integration_place_cancel` — `place_limit_order` at extreme price succeeds, `cancel_order` returns without error
- `test_integration_get_balance` — `get_balance()` returns dict with `balance_cents` > 0
- `test_integration_get_positions` — `get_positions()` returns a list (may be empty)

**Why human:** Integration stubs have `pass` bodies — they require real Kalshi demo credentials and a live network connection. Cannot verify programmatically in CI.

---

### Notes

**R1.2 URL discrepancy:** REQUIREMENTS.md specifies the production URL as `https://api.elections.kalshi.com/trade-api/v2` (legacy hostname). RESEARCH.md documents this discrepancy and confirms `https://trading-api.kalshi.com/trade-api/v2` is the current production hostname per official Kalshi docs. The implementation uses the correct current URL. REQUIREMENTS.md should be updated, but this does not block phase goal achievement.

**data/ directory:** The `data/` directory is not committed (it is in `.gitignore`) and does not exist in the working tree. This is correct and expected — the `Database` class creates the `data/` directory automatically on first instantiation via `os.makedirs(..., exist_ok=True)`.

---

### Gaps Summary

No gaps. All 11 must-have truths are verified, all artifacts exist and are substantive and wired, all 10 phase-1 requirements are satisfied, and all 16 unit tests pass. The only open item is human verification of the 6 integration stubs against a live demo API account.

---

_Verified: 2026-03-28_
_Verifier: Claude (gsd-verifier)_
