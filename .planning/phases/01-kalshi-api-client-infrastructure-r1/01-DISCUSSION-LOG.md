# Phase 1: Kalshi API Client & Infrastructure - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-03-28
**Phase:** 01-kalshi-api-client-infrastructure-r1
**Areas discussed:** Kalshi client foundation, Sync vs async I/O, Integration test gating, RSA-PSS signing details, SQLite schema scope, Config loading approach

---

## Kalshi Client Foundation

| Option | Description | Selected |
|--------|-------------|----------|
| Raw requests + custom auth | Build directly on `requests` with our own RSA-PSS signing. Full control, no SDK drift. | ✓ |
| kalshi-python SDK wrapper | Use official SDK, patch in custom auth. Saves endpoint boilerplate but SDK may be stale. | |

**User's choice:** Raw requests + custom auth
**Notes:** User asked for clarification on what was being asked before deciding. Chose raw HTTP for full control.

---

## Sync vs Async I/O

| Option | Description | Selected |
|--------|-------------|----------|
| Sync with requests | Simple, sequential. Fast enough for scheduled bot. | ✓ |
| Async with httpx | Parallel market scanning, but async propagates through all phases. | |

**User's choice:** Sync with requests
**Notes:** Bot runs on a schedule, not real-time. Simplicity preferred.

---

## Integration Test Gating

| Option | Description | Selected |
|--------|-------------|----------|
| Opt-in via KALSHI_INTEGRATION=true | Default pytest uses mocks. Demo API only when flag set. | ✓ |
| Always run against demo API | Every pytest run hits demo. Simpler but requires credentials + network always. | |

**User's choice:** Opt-in via env var
**Notes:** User asked for clarification. Preferred opt-in after explanation.

---

## RSA-PSS Signing Details

| Option | Description | Selected |
|--------|-------------|----------|
| Load key once at startup | Read .pem once, cache in KalshiAuth object. | ✓ |
| Load key per request | Re-read .pem every API call. Allows hot-swap. | |

| Option | Description | Selected |
|--------|-------------|----------|
| Unix milliseconds as integer | Matches Kalshi's documented format. | ✓ |
| ISO 8601 string | Human-readable but not what Kalshi expects. | |

**User's choice:** Load once at startup; Unix milliseconds timestamp

---

## SQLite Schema Scope

| Option | Description | Selected |
|--------|-------------|----------|
| Full schema now — all tables | All 7-phase tables designed in Phase 1. No mid-project migrations. | ✓ |
| Minimal now, extend per phase | Only Phase 1 tables now, add later. Less upfront design but more migrations. | |

**User's choice:** Full schema upfront
**Notes:** ROADMAP also explicitly calls for "full SQLite schema" as Phase 1 deliverable.

---

## Config Loading Approach

| Option | Description | Selected |
|--------|-------------|----------|
| Module-level singleton | `from src.config import config` anywhere. Loads at import. | ✓ |
| Passed-in via constructor | Config object threaded through every class constructor. More explicit, more verbose. | |

**User's choice:** Singleton
**Notes:** User asked for clarification. Chose singleton after explanation.

---

## Claude's Discretion

- HTTP session management and timeouts
- Retry logic implementation details
- Rate limiting approach
- Private key PEM loading library choice
