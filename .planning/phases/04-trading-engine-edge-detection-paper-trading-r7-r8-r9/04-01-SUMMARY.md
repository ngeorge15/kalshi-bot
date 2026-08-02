---
phase: 04-trading-engine-edge-detection-paper-trading-r7-r8-r9
plan: "01"
subsystem: trading-engine
tags: [edge-detection, kelly-sizing, risk-management, paper-trading, ledger]
dependency_graph:
  requires: ["03-01", "03-02", "03-03"]
  provides: [src/trading/edge_detector.py, src/trading/position_sizer.py, src/trading/risk_manager.py, src/trading/order_manager.py, src/trading/ledger.py]
  affects: ["Phase 5 analytics", "Phase 6 evaluator"]
tech_stack:
  added: []
  patterns: [dataclass Signal/SizedOrder value objects, per-market-type configurable thresholds, post_only=True maker-bias orders, dry_run mode for safe testing]
key_files:
  created:
    - src/trading/__init__.py
    - src/trading/edge_detector.py
    - src/trading/ledger.py
    - src/trading/order_manager.py
    - src/trading/position_sizer.py
    - src/trading/risk_manager.py
  modified: []
decisions:
  - "Edge ranking formula edge x sqrt(liquidity) x confidence (R7.6) — sqrt dampens the influence of very high liquidity so edge magnitude still dominates ranking"
  - "Per-market-type thresholds (min edge, max spread, min volume, position caps) are all read from config via _get_* helper methods rather than hardcoded, so evaluator-proposed config_tweak improvements (Phase 6) can adjust them without code changes"
  - "OrderManager.submit_order supports dry_run=True so the full pipeline can be exercised without hitting the Kalshi API, independent of the demo/prod config switch"
  - "Ledger records every evaluated signal (acted on or skipped) via record_prediction(), not just executed trades — required for Phase 5 calibration analysis and Phase 6 evaluator diagnosis, which both need the full population of predictions vs only the subset that became trades"
metrics:
  duration: unknown (direct commit, not executed via /gsd:plan-phase / /gsd:execute-phase)
  completed_date: "2026-04-02"
  tasks_completed: 1
  files_created: 6
---

# Phase 04 Plan 01: Trading Engine — Edge Detection, Position Sizing, Risk Management, Order Execution, Ledger

Implemented the complete trading logic layer: edge detector, fractional-Kelly position sizer, risk manager with kill switch, order manager, and paper trading ledger. 87 new tests, 231 total passing at the time (0 failures, 14 skipped).

## Reconciliation Note

**This entire phase was executed without ever running `/gsd:plan-phase 4`.** The code was written and committed directly (commit `4a8c6c1`, 2026-04-02) straight from the ROADMAP.md Phase 4 deliverables list — there was no discuss-phase, no plan-phase, no phase-04 directory, and no verifier run at the time. STATE.md's frontmatter was hand-edited afterward to claim `current_phase: 04, status: complete`, but ROADMAP.md, REQUIREMENTS.md traceability, and this directory were never created to match.

This PLAN.md + SUMMARY.md pair was reconstructed on 2026-08-02 during a `/gsd:resume-work` session specifically to close that bookkeeping gap. Unlike Phase 3 (see `03-03-SUMMARY.md` / `03-04-SUMMARY.md`), no functional code gaps were found here — the implementation was checked against every R7/R8/R9 sub-requirement and matches (see requirements-mapping in `04-01-PLAN.md`). Nothing in `src/trading/` was modified during this reconciliation.

## What Was Built

See `04-01-PLAN.md`'s `<what-was-built>` section for the full breakdown. Summary:

- **edge_detector.py** — `EdgeDetector`, `Signal` dataclass. Scans markets, computes edge, applies per-type min-edge/liquidity/confidence filters, ranks by `edge x sqrt(liquidity) x confidence`.
- **position_sizer.py** — `PositionSizer`, `SizedOrder` dataclass. 0.25x fractional Kelly, per-type position caps, portfolio exposure cap, same-game/same-city correlation caps.
- **risk_manager.py** — `RiskManager`, `KillSwitchTriggered`. Daily loss limit, max concurrent positions, `emergency_halt()` cancelling all open orders.
- **order_manager.py** — `OrderManager`. Submits `post_only=True` limit orders to Kalshi, `dry_run` mode, fill tracking.
- **ledger.py** — `Ledger`. Records trades/predictions/outcomes/daily P&L to the SQLite tables defined in Phase 1's schema.

## Deviations from Plan

- No PLAN.md existed at execution time — this entire "plan" is a post-hoc reconstruction. All deviation tracking that would normally happen live (task-by-task acceptance criteria checks) was replaced by a single retroactive requirements-mapping audit on 2026-08-02.

## Verification (retroactive, 2026-08-02)

- `python -m pytest tests/trading/ -q` — all green (87 tests: edge_detector, ledger, order_manager, position_sizer, risk_manager)
- Manually cross-checked every R7.1-R7.7, R8.1-R8.7, R9.1-R9.6 sub-requirement against the implementation; no gaps found (unlike Phase 3, where R6.8 and two files were missing)
- `src/db/schema.sql` confirmed to already contain all 8 tables R9.1 requires (`trades`, `predictions`, `outcomes`, `daily_pnl`, `model_versions`, `improvements`, `evaluator_runs`, `holdout_results`)
- Full suite (`python -m pytest -q`): 253 passed, 13 skipped, 0 failed
