---
phase: 03-prediction-models-overfitting-guards-r4-r5-r6
plan: "03"
subsystem: overfitting-guards-and-training-pipeline
tags: [overfitting, temporal-split, walk-forward, ks-test, regime-detection, cli]
dependency_graph:
  requires: [03-00]
  provides: [src/validation/splitter.py, src/validation/walk_forward.py, src/validation/guards.py, src/validation/metrics.py, src/models/pipeline.py]
  affects: [03-04, "Phase 6 evaluator"]
tech_stack:
  added: []
  patterns: [TimeSeriesSplit for walk-forward windows, Kolmogorov-Smirnov two-sample test for regime detection, hard-block guards with mandatory-reason force override logged to SQLite]
key_files:
  created:
    - src/validation/metrics.py
    - src/models/pipeline.py
  modified:
    - src/validation/guards.py
    - tests/validation/test_guards.py
decisions:
  - "R6.8 regime detection implemented as OverfittingGuards.check_regime(sample1, sample2) using scipy.stats.ks_2samp; raises GuardViolation(guard_name='regime_change') when KS p-value < 0.05 (same SIGNIFICANCE_THRESHOLD constant as check_significance)"
  - force_override(guard_name, reason, model_name) added per D-07 — requires a non-empty reason, writes a 'guard_override' row to the improvements table (risk_level='high', status='applied') for evaluator-visible accountability rather than silently bypassing
  - metrics.py implements brier_score/accuracy/calibration_error as plain numpy functions (no class) since they're stateless and consumed by both the validator and the Phase 5 analytics layer
  - "pipeline.py CLI (python -m src.models.pipeline --train/--status) is the only code path that calls model train() methods, per D-01 — models never train themselves from predict()"
  - "pipeline --train runs OverfittingGuards.check_sample_size() before treating a model as fully deployed; below 50 OOS predictions it logs a bootstrap-mode warning instead of blocking, per D-08"
metrics:
  duration: ~45min (gap-closure session)
  completed_date: "2026-08-02"
  tasks_completed: 2
  files_created: 2
  files_modified: 2
---

# Phase 03 Plan 03: Overfitting Guards (6 checks) + Training Pipeline CLI

Completed the overfitting protection system: temporal splitter and walk-forward validator (already present from the 2026-04-02 direct commit), plus the pieces that commit left out — the 6th guard (regime change detection), `force_override()`, `metrics.py`, and the `pipeline.py` training CLI.

## Reconciliation Note

This plan was **not** fully covered by the 2026-04-02 direct commit (`e720c4d`). That commit delivered `splitter.py`, `walk_forward.py`, and a `guards.py` with only 5 of the 6 required guards (missing R6.8 regime detection) — `metrics.py` and `src/models/pipeline.py` did not exist at all. This gap was discovered during a `/gsd:resume-work` session on 2026-08-02 and closed in the same session before this SUMMARY.md was written. Constant names in the shipped `guards.py` (`MAX_PARAM_CHANGE_PCT`, per-check log style) already diverged slightly from this plan's original code sample — new methods were written to match the existing file's conventions rather than overwriting it verbatim.

## What Was Built (this session, 2026-08-02)

- `OverfittingGuards.check_regime(sample1, sample2)` — two-sample KS test; raises `GuardViolation(guard_name="regime_change")` on `p < 0.05`. Added to `src/validation/guards.py` alongside the 5 pre-existing checks.
- `OverfittingGuards.force_override(guard_name, reason, model_name)` — logs a `guard_override` row to `improvements`; raises `ValueError` on empty reason, `RuntimeError` if no `db` was supplied.
- `src/validation/metrics.py` — `brier_score()`, `accuracy()`, `calibration_error()` (Expected Calibration Error over configurable bins).
- `src/models/pipeline.py` — CLI with `--train [--model X] [--force REASON]` and `--status`. `run_train()` trains one/all of the 5 models, runs the sample-size guard, and supports guard bypass with a logged reason. `run_status()` prints each model's active version and bootstrap status.
- 6 new tests in `tests/validation/test_guards.py` (regime pass/fail, force_override empty-reason/logging) and 7 new tests in `tests/validation/test_metrics.py`.

## What Was Already Present (from commit `e720c4d`, 2026-04-02)

- `src/validation/splitter.py` — `TemporalSplitter`, 60/20/20 chronological split, `access_holdout(allow_holdout=True)` returning an index array (write-protected otherwise).
- `src/validation/walk_forward.py` — `WalkForwardValidator` using `sklearn.model_selection.TimeSeriesSplit`, yields per-window `{window, train_size, test_size, train_end_idx, test_start_idx, metrics}`.
- `check_sample_size`, `check_dampening`, `check_cooldown`, `check_staleness`, `check_significance` in `guards.py` with exact R6 threshold constants (50 OOS, 20% dampening, 30-trade cooldown, 30-day staleness, p<0.05 significance).

## Deviations from Plan

- `pipeline.py` lives at `src/models/pipeline.py` (not `src/validation/pipeline.py`) — matches this plan's own `files_modified` frontmatter and `python -m src.models.pipeline` usage string, which is what the code and 03-04's tests actually reference.
- CLI arg parsing uses `argparse` subparser-free boolean flags (`--train`, `--status`) rather than the plan sample's manual `sys.argv` re-parsing — functionally equivalent, cleaner implementation.

## Verification

- `python -m pytest tests/validation/ -q` — 23 passed
- `python -c "from src.validation.guards import OverfittingGuards; from src.validation.splitter import TemporalSplitter; from src.validation.walk_forward import WalkForwardValidator; from src.validation.metrics import brier_score"` — exits 0
- `python -m src.models.pipeline --status` — exits 0, lists all 5 models as `untrained` / bootstrap mode
- Full suite (`python -m pytest -q`): 253 passed, 13 skipped, 0 failed
