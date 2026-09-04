# Phase 5 Context: Performance Tracking & Analytics [R10]

**Planned:** 2026-09-04 (direct planning session, not `/gsd:discuss-phase` — user opted out of GSD agent overhead)

## Goal

Build the analytics foundation that both the human operator and the Phase 6 evaluator bot
consume. Phase 5 owns the **data contract** the evaluator reads, so decisions here constrain
Phase 6.

## Prior art this phase builds on

| Asset | Location | Note |
|---|---|---|
| `brier_score`, `accuracy`, `calibration_error` | `src/validation/metrics.py` | Already written for this phase — docstring says "used by ... the analytics layer (Phase 5)" |
| Ledger read helpers | `src/trading/ledger.py` | `get_trades`, `get_predictions`, `get_outcomes`, `get_total_pnl` |
| `WalkForwardValidator` | `src/validation/walk_forward.py` | Reused by `recalibrate.py` |
| `TemporalSplitter` | `src/validation/splitter.py` | Holdout write-protection must stay intact during recalibration |
| `ModelStore` | `src/models/model_store.py` | `versions.json` + joblib; stays the source of truth for *artifacts* |

## Gaps found during planning (not in the ROADMAP, real work)

1. **Nothing computes `daily_pnl`.** `Ledger.update_daily_pnl()` is an upsert with no caller and
   no aggregator. R10.3 (P&L curve, Sharpe) and R10.8 (daily report) both depend on that table
   being populated. `performance.recompute_daily_pnl()` takes ownership.
2. **`model_versions` and `holdout_results` tables are dead** — created in schema v1, never written.
   Phase 6 (R11.1) is restricted to "SQLite reads and file writes only", so model metadata must
   reach SQL.
3. **Weather models have no `FEATURE_NAMES` constant** (NBA models do). Needed for R10.4 to
   produce named importances rather than `feature_0`.
4. **`matplotlib` is installed in the anaconda env but unpinned** in `requirements.txt`.

## Decisions

- **D5-01 — Recalibration shadow-trains, never persists.** Phase 3's D-01 ("`pipeline.py` is the
  only code path allowed to call model `train()`") stays intact. `recalibrate.py` fits a candidate
  in memory, scores it against the active model on the **test set (never holdout)**, and returns a
  recommendation. It must not call `ModelStore.save()`. Only `python -m src.models.pipeline --train`
  persists a version. Rationale: an analytics command must never be able to silently swap the live
  model or bypass `OverfittingGuards`.
- **D5-02 — Model metrics and feature importances go to SQLite.** Schema v3 adds a
  `feature_importance` table; Phase 5 also begins populating the existing empty `model_versions`
  table. `versions.json` remains authoritative for artifact loading only. Rationale: R11.1 confines
  the evaluator to SQL reads.
- **D5-03 — Edge decay reads a dedicated `price_observations` table**, not repeated `predictions`
  rows. Carries `hours_to_close` so decay is bucketable by time-to-settlement, and records markets
  we never traded — which is where decay signal actually lives.
- **D5-04 — Calibration is data-first.** `calibration_bins()` returns plain dicts (consumed by
  `snapshot.py` and tests); `render_reliability_diagram()` is a thin matplotlib/Agg layer writing
  PNGs to `data/reports/`. Core logic stays testable headless.
- **D5-05 — Sharpe uses daily returns from `daily_pnl`, annualized by sqrt(252).** Per-trade Sharpe
  is ill-defined for binary contracts with varying hold times.
- **D5-06 — One join helper.** `predictions` <-> `outcomes` resolves via `prediction_id` when set,
  falling back to ticker match. All metrics build on a single `_settled_predictions()` helper so the
  join exists in exactly one place.
- **D5-07 — Domain-agnostic by construction.** Verified during planning that `src/trading/` contains
  zero hardcoded domain strings and `market_type` is free-form TEXT. Phase 5 must not regress this:
  no analytics function may enumerate `{nba, weather}`, the snapshot JSON must not use a fixed
  market-type key set, and the seed script seeds a third domain (`cbb_games`) so per-market-type
  breakdown code is *proven* not to assume two. Rationale: `REQUIREMENTS.md` files additional sports
  under V2; the cost of not baking in the assumption is zero today and material later.

## Out of scope (deferred)

- Additional sports (NFL/MLB/CBB) — V2 per `REQUIREMENTS.md`. Discussed 2026-09-04; conclusion was
  that the trading/analytics/evaluator layers already generalize, so expansion cost sits in
  `src/data/<sport>/` and `src/models/`. Revisit at milestone 2 opening with a generalization
  refactor, prioritized by settled-events-per-season (the evaluator learning loop is sample-gated).
- `analytics/slippage.py` — listed in CONVENTIONS under `analytics/` but belongs to Phase 7 (R13.6),
  which compares live vs paper fills.
- `scripts/market_recon.py` (Kalshi liquidity survey by category) — proposed, not scheduled.

## Plan breakdown

| Plan | Scope | Requirements |
|---|---|---|
| 05-01 | Schema v3, `src/analytics/` package, `scripts/seed_test_data.py`, pin matplotlib | foundation |
| 05-02 | `performance.py` — Brier, calibration bins, P&L, Sharpe, win rate, `recompute_daily_pnl` | R10.1, R10.2, R10.3 |
| 05-03 | Feature importance extraction + history; `edge_decay.py` | R10.4, R10.6 |
| 05-04 | `recalibrate.py` shadow-train; `snapshot.py` evaluator contract | R10.5, R10.7 |
| 05-05 | `daily_report.py` CLI, reliability PNG, e2e integration test | R10.8 + working test |

## Environment note

Tests run under `/opt/anaconda3/bin/python3` (system `python3` has no scikit-learn).
