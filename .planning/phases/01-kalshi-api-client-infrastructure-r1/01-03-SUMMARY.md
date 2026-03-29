---
phase: 01
plan: 03
subsystem: kalshi-api-client
tags: [api-client, requests, retry, orderbook, orders, portfolio]
dependency_graph:
  requires: [01-02]
  provides: [src/kalshi/client.py, KalshiClient]
  affects: [all-subsequent-phases]
tech_stack:
  added: [requests.Session, urllib3.Retry, HTTPAdapter]
  patterns: [auth-base-composition, cursor-pagination, dollars-to-cents-conversion]
key_files:
  created: [src/kalshi/client.py]
  modified: [tests/test_kalshi_client.py]
decisions:
  - "yes_price always set on YES side; NO orders use 100-price_cents per Kalshi API convention"
  - "Retry(allowed_methods) used (not deprecated method_whitelist) for urllib3 compatibility"
  - "post_only=True default on place_limit_order enforces maker-only bias per D-02 / CONVENTIONS.md"
metrics:
  duration: "2min"
  completed: "2026-03-29"
  tasks: 1
  files: 2
---

# Phase 01 Plan 03: Kalshi API Client (All CRUD Operations) Summary

**One-liner:** Full KalshiClient with pagination, orderbook _dollars parsing, limit order placement, and Retry(total=3) session — all 16 unit tests pass.

## What Was Built

`src/kalshi/client.py` implements `KalshiClient`, the core Kalshi REST API v2 client that all subsequent phases depend on for market interaction.

### Architecture

- `_build_session()` mounts a urllib3 `Retry(total=3, backoff_factor=1, status_forcelist=[429,500,502,503,504])` adapter on the `https://` prefix, so transient 5xx and rate-limit responses are automatically retried.
- `_auth = KalshiAuth(key_id, private_key_path)` is attached to the session — every request is automatically RSA-PSS signed.
- Private HTTP helpers `_get`, `_post`, `_delete` wrap `session.{method}` and call `raise_for_status()`.

### Methods Implemented

| Method | Requirement | Description |
|--------|-------------|-------------|
| `get_markets_by_series(series_ticker, ...)` | R1.3 | Cursor-paginated market discovery by series |
| `get_market(ticker)` | R1.3 | Single market lookup |
| `get_event(event_ticker)` | R1.4 | Event (bracket) retrieval |
| `get_orderbook(ticker)` | R1.5 | Orderbook with `_dollars` → cents conversion |
| `place_limit_order(...)` | R1.6 | Limit order with post_only maker bias |
| `cancel_order(order_id)` | R1.7 | Order cancellation via DELETE |
| `get_orders(status)` | R1.7 | List open/filled orders |
| `get_fills(ticker, limit)` | R1.7 | Fill history |
| `get_balance()` | R1.8 | Balance in cents |
| `get_positions(status)` | R1.8 | Current market positions |
| `get_settlements(limit)` | R1.8 | Settlement history |
| `get_exchange_status()` | utility | Exchange open/closed status |
| `from_config(config)` | — | Convenience classmethod from Config object |

### Orderbook Parsing

`get_orderbook` converts Kalshi's `orderbook_fp.yes_dollars` / `no_dollars` fixed-point string arrays into `(int_cents, float_qty)` tuples using `dollars_to_cents`. This handles the March 2026 Kalshi breaking change (removal of integer price fields).

### Order Price Normalization

`place_limit_order` always sends `yes_price` in the payload. For a YES order, `yes_price = price_cents`. For a NO order, `yes_price = 100 - price_cents`. This matches Kalshi's API convention where all prices are expressed on the YES side.

## Test Results

All 16 unit tests passed (0 failures, 0 errors):

```
tests/test_kalshi_client.py::test_auth_signature PASSED
tests/test_kalshi_client.py::test_auth_headers PASSED
tests/test_kalshi_client.py::test_auth_strips_query_string PASSED
tests/test_kalshi_client.py::test_base_url_demo PASSED
tests/test_kalshi_client.py::test_base_url_prod PASSED
tests/test_kalshi_client.py::test_missing_env_var PASSED
tests/test_kalshi_client.py::test_dollars_to_cents PASSED
tests/test_kalshi_client.py::test_market_discovery_pagination PASSED
tests/test_kalshi_client.py::test_get_event PASSED
tests/test_kalshi_client.py::test_orderbook_parse PASSED
tests/test_kalshi_client.py::test_place_order_payload PASSED
tests/test_kalshi_client.py::test_cancel_order PASSED
tests/test_kalshi_client.py::test_get_balance PASSED
tests/test_kalshi_client.py::test_get_positions PASSED
tests/test_kalshi_client.py::test_get_fills PASSED
tests/test_kalshi_client.py::test_retry_config PASSED

16 passed, 6 deselected in 1.36s
```

## Deviations from Plan

None — plan executed exactly as written.

## Known Stubs

None — all methods are fully implemented and wired to real API endpoints.

## Self-Check: PASSED

- `src/kalshi/client.py` exists: FOUND
- Task commit 7a71d23 exists: FOUND
- All 16 unit tests pass: CONFIRMED
