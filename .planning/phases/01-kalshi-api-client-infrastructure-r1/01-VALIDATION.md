---
phase: 1
slug: kalshi-api-client-infrastructure-r1
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-03-28
---

# Phase 1 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 7.4.4 |
| **Config file** | `pytest.ini` or `pyproject.toml [tool.pytest]` — Wave 0 creates |
| **Quick run command** | `pytest tests/test_kalshi_client.py -v -m "not integration"` |
| **Full suite command** | `pytest tests/ -v` |
| **Integration run** | `KALSHI_INTEGRATION=true pytest tests/ -v -m integration` |
| **Estimated runtime** | ~10 seconds (unit only) |

---

## Sampling Rate

- **After every task commit:** Run `pytest tests/test_kalshi_client.py -v -m "not integration"`
- **After every plan wave:** Run `pytest tests/ -v`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 10 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| auth-sig | auth | 1 | R1.1 | unit | `pytest tests/test_kalshi_client.py::test_auth_signature -x` | ❌ Wave 0 | ⬜ pending |
| auth-headers | auth | 1 | R1.1 | unit | `pytest tests/test_kalshi_client.py::test_auth_headers -x` | ❌ Wave 0 | ⬜ pending |
| config-demo-url | config | 1 | R1.2 | unit | `pytest tests/test_kalshi_client.py::test_base_url_demo -x` | ❌ Wave 0 | ⬜ pending |
| config-prod-url | config | 1 | R1.2 | unit | `pytest tests/test_kalshi_client.py::test_base_url_prod -x` | ❌ Wave 0 | ⬜ pending |
| market-discovery | client | 2 | R1.3 | unit | `pytest tests/test_kalshi_client.py::test_market_discovery_pagination -x` | ❌ Wave 0 | ⬜ pending |
| market-integration | client | 2 | R1.3 | integration | `KALSHI_INTEGRATION=true pytest tests/test_kalshi_client.py::test_integration_discover_nba_games -x` | ❌ Wave 0 | ⬜ pending |
| get-event | client | 2 | R1.4 | unit | `pytest tests/test_kalshi_client.py::test_get_event -x` | ❌ Wave 0 | ⬜ pending |
| orderbook-parse | client | 2 | R1.5 | unit | `pytest tests/test_kalshi_client.py::test_orderbook_parse -x` | ❌ Wave 0 | ⬜ pending |
| orderbook-integration | client | 2 | R1.5 | integration | `KALSHI_INTEGRATION=true pytest tests/test_kalshi_client.py::test_integration_orderbook -x` | ❌ Wave 0 | ⬜ pending |
| place-order | client | 2 | R1.6 | unit | `pytest tests/test_kalshi_client.py::test_place_order_payload -x` | ❌ Wave 0 | ⬜ pending |
| place-cancel-integration | client | 2 | R1.6 | integration | `KALSHI_INTEGRATION=true pytest tests/test_kalshi_client.py::test_integration_place_cancel -x` | ❌ Wave 0 | ⬜ pending |
| cancel-order | client | 2 | R1.7 | unit | `pytest tests/test_kalshi_client.py::test_cancel_order -x` | ❌ Wave 0 | ⬜ pending |
| get-balance | client | 2 | R1.8 | unit | `pytest tests/test_kalshi_client.py::test_get_balance -x` | ❌ Wave 0 | ⬜ pending |
| retry-config | client | 1 | R1.9 | unit | `pytest tests/test_kalshi_client.py::test_retry_config -x` | ❌ Wave 0 | ⬜ pending |
| missing-env | client | 1 | R1.10 | unit | `pytest tests/test_kalshi_client.py::test_missing_env_var -x` | ❌ Wave 0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_kalshi_client.py` — stub test file with all test function stubs (collect passes, no implementations yet)
- [ ] `tests/conftest.py` — shared fixtures including RSA key generation
- [ ] `pytest.ini` — pytest config with `integration` marker defined
- [ ] `pip install pytest-mock responses` (if not already installed; fallback to `unittest.mock`)

*Wave 0 creates all test stubs before any implementation so every commit has a test to run.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| GitHub repo created and remote added | Phase 1 GitHub setup | Requires GitHub credentials and user preference (public/private) | `git remote -v` shows `origin` pointing to github.com/*/kalshi-bot |
| `.env` file loads correctly with real credentials | R1.1 | Requires real RSA key + Kalshi account | Set KALSHI_PRIVATE_KEY_PATH and KALSHI_API_KEY; run `python -c "from src.config import Config; c = Config(); print(c.base_url)"` |
| Demo API auth succeeds end-to-end | R1.2 | Requires real Kalshi demo credentials | `KALSHI_INTEGRATION=true pytest tests/test_kalshi_client.py -v -m integration` |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 10s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
