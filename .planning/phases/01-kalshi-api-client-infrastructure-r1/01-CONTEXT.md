# Phase 1: Kalshi API Client & Infrastructure - Context

**Gathered:** 2026-03-28
**Status:** Ready for planning

<domain>
## Phase Boundary

Build the Kalshi API client with RSA-PSS authentication, market/event discovery, orderbook retrieval, order management, and portfolio tracking. Set up the full project skeleton: directory structure per CONVENTIONS.md, config management, `.env` loading, SQLite schema (all tables, full schema), GitHub remote, `.gitignore`, and `requirements.txt`. All development targets the demo API.

**Note:** GitHub repo already exists at `https://github.com/ngeorge15/kalshi-bot.git` and `.env` is populated with demo credentials — those steps are already done.

</domain>

<decisions>
## Implementation Decisions

### Kalshi API Client
- **D-01:** Build the client with raw `requests` + custom RSA-PSS auth. Do NOT use the `kalshi-python` SDK — it has stale endpoints and awkward auth patching. Implement all HTTP calls directly against the REST API.
- **D-02:** Client is synchronous (`requests` library). No async/await. The bot runs on a schedule, not in real-time — sequential calls are fast enough. Simpler code throughout all phases.

### RSA-PSS Authentication
- **D-03:** Load the private key once at `KalshiAuth` initialization time and cache in memory. No per-request file I/O.
- **D-04:** Timestamp in the auth signature is Unix milliseconds as an integer string — matches Kalshi's documented format.

### Config
- **D-05:** Module-level singleton pattern. `config.py` loads `.env` + `trading_config.json` at import time and exposes a `config` object. Downstream code accesses via `from src.config import config`. No dependency injection threading through constructors.

### SQLite Schema
- **D-06:** Define the **full schema** in Phase 1 — all tables for all 7 phases (`trades`, `predictions`, `outcomes`, `daily_pnl`, `model_versions`, `improvements`, `evaluator_runs`, `holdout_results`, `schema_version`). Avoids mid-project migrations. `database.py` includes schema versioning so future changes are additive.

### Testing
- **D-07:** Integration tests (demo API calls — auth, market discovery, orderbook pull, place/cancel order) are opt-in. Run only when `KALSHI_INTEGRATION=true` is set. Default `pytest tests/` uses mocks — fast, no network required. Mark with `@pytest.mark.integration`.

### Claude's Discretion
- HTTP session management (connection pooling, timeouts) — standard `requests.Session` config
- Retry logic implementation (3 retries, exponential backoff per CONVENTIONS.md)
- Rate limiting approach (simple sleep-based per CONVENTIONS.md: respect tier limits)
- Private key file format handling (PEM loading via `cryptography` library)

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Project Specs
- `.planning/CONVENTIONS.md` — directory structure, Python conventions, config file format (`trading_config.json` schema), environment variables, API interaction conventions (retry policy, rate limits, money in cents)
- `.planning/REQUIREMENTS.md` §R1 — full acceptance criteria for Phase 1 (R1.1–R1.10)
- `.planning/ROADMAP.md` §Phase 1 — deliverables list and working test definition
- `.planning/PROJECT.md` — tech stack decisions, key design decisions, architecture overview

No external ADRs or specs beyond the above.

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- None — fresh project. Only spec files exist in project root and `.planning/`.

### Established Patterns
- None yet — Phase 1 establishes all patterns.

### Integration Points
- `.env` already populated with `KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY_PATH`, `KALSHI_ENV`, `EVALUATOR_LLM_PROVIDER`, `GOOGLE_API_KEY`, `NWS_CONTACT_EMAIL`
- GitHub remote already configured: `https://github.com/ngeorge15/kalshi-bot.git`

</code_context>

<specifics>
## Specific Ideas

- User explicitly chose raw HTTP over SDK — keep all Kalshi API interaction in `src/kalshi/client.py` and `src/kalshi/auth.py` with no third-party Kalshi wrapper
- Sync over async was a deliberate simplicity choice — do not introduce `asyncio` or `httpx` in this phase

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope.

</deferred>

---

*Phase: 01-kalshi-api-client-infrastructure-r1*
*Context gathered: 2026-03-28*
