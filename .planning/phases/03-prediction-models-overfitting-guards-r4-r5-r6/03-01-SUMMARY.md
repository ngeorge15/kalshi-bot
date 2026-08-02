---
phase: 03-prediction-models-overfitting-guards-r4-r5-r6
plan: "01"
subsystem: nba-prediction-models
tags: [nba, gbm, platt-calibration, elo, moneyline, totals, player-props]
dependency_graph:
  requires: [03-00]
  provides: [src/models/nba_game.py, src/models/nba_totals.py, src/models/nba_props.py]
  affects: [03-04, "Phase 4 edge_detector"]
tech_stack:
  added: []
  patterns: [GradientBoostingClassifier + CalibratedClassifierCV (Platt scaling), module-level singleton per D-14, ModelStore-backed persistence]
key_files:
  created:
    - src/models/nba_game.py
    - src/models/nba_totals.py
    - src/models/nba_props.py
  modified:
    - tests/models/test_nba_game.py
    - tests/models/test_nba_totals.py
    - tests/models/test_nba_props.py
decisions:
  - GBM (GradientBoostingClassifier) + CalibratedClassifierCV(method="sigmoid") for all three NBA models per D-05 — Platt-calibrated probabilities returned on every predict() call
  - nba_game feature vector is 7 features (home/away ELO, home_court, home/away rest days, home/away net rating); ELO updated sequentially in temporal order during train() to avoid future leakage
  - nba_props.train(prop_type=...) trains one GBM per prop type (pts/reb/ast/3pm) rather than a single multi-output model, per per-prop calibration requirement (R4.3)
  - All three models raise InsufficientDataError below a minimum historical sample (200 games for nba_game) rather than silently training on too little data
metrics:
  duration: unknown (direct commit, not executed via /gsd:execute-phase)
  completed_date: "2026-04-02"
  tasks_completed: 1
  files_created: 3
---

# Phase 03 Plan 01: NBA Prediction Models (Game, Totals, Props)

Implemented all three NBA prediction models: game moneyline/spread (ELO + team stats + rest via GBM + Platt calibration), totals over/under (pace-adjusted GBM), and player props (per-player GBM per prop type with matchup context). All three expose a consistent `predict()` API and persist through `ModelStore`.

## Reconciliation Note

This plan's code was written and committed directly (commit `e720c4d`, 2026-04-02) without going through `/gsd:plan-phase` → `/gsd:execute-phase`. This SUMMARY.md was created retroactively during a `/gsd:resume-work` session on 2026-08-02 to bring GSD's tracking in line with what was actually built. The code itself was verified against this plan's `must_haves` and acceptance criteria at that time; no functional changes were made to satisfy this plan specifically (see 03-03/03-04 summaries for gaps that *did* require new code).

## What Was Built

- `src/models/nba_game.py` — `NBAGameModel`, singleton `nba_game_model`. `predict(features, context="moneyline")` returns Platt-calibrated probabilities; `train()` fetches historical results, builds a 7-feature vector with sequentially-updated ELO, fits GBM + `CalibratedClassifierCV`, and saves via `ModelStore("nba_game")`.
- `src/models/nba_totals.py` — `NBATotalsModel`, singleton `nba_totals_model`. Pace-adjusted over/under projection using combined offensive/defensive efficiency; same GBM + Platt calibration pattern.
- `src/models/nba_props.py` — `NBAPropsModel`, singleton `nba_props_model`. `predict(features, prop_type="pts")` and `train(prop_type=...)` support points/rebounds/assists/threes independently, each with its own trained GBM.
- Stub tests in `tests/models/test_nba_*.py` turned from `xfail` to real assertions against the implemented `predict()`/`train()` behavior.

## Deviations from Plan

- Executed as a direct commit rather than through the GSD execute pipeline — no per-task commits, no automated verification loop at execution time. Verified retroactively instead (see below).

## Verification (retroactive, 2026-08-02)

- `python -m pytest tests/models/test_nba_game.py tests/models/test_nba_totals.py tests/models/test_nba_props.py -q` — all green
- `python -c "from src.models.nba_game import nba_game_model; from src.models.nba_totals import nba_totals_model; from src.models.nba_props import nba_props_model"` — exits 0
- Full suite (`python -m pytest -q`) green after this plan plus 03-02/03-03/03-04: 253 passed, 13 skipped, 0 failed
