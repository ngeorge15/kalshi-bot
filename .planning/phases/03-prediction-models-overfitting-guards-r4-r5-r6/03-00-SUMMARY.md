---
phase: 03-prediction-models-overfitting-guards-r4-r5-r6
plan: "00"
subsystem: models-validation-scaffold
tags: [wave0, scaffold, dependencies, exceptions, test-stubs, xfail]
dependency_graph:
  requires: [02-05]
  provides: [src/models/__init__.py, src/models/exceptions.py, src/validation/__init__.py, test stubs for all Phase 3 modules]
  affects: [03-01, 03-02, 03-03, 03-04]
tech_stack:
  added: [scikit-learn==1.5.1, scipy==1.13.1, numpy==1.26.4, pandas==2.2.2]
  patterns: [xfail(strict=True) stub tests, module-level package inits, custom exception hierarchy]
key_files:
  created:
    - requirements.txt (updated)
    - src/models/__init__.py
    - src/models/exceptions.py
    - src/validation/__init__.py
    - tests/models/__init__.py
    - tests/models/test_nba_game.py
    - tests/models/test_nba_totals.py
    - tests/models/test_nba_props.py
    - tests/models/test_weather_temp.py
    - tests/models/test_weather_precip.py
    - tests/models/test_model_store.py
    - tests/validation/__init__.py
    - tests/validation/test_splitter.py
    - tests/validation/test_walk_forward.py
    - tests/validation/test_guards.py
    - tests/integration/test_phase3_e2e.py
  modified: []
decisions:
  - scikit-learn 1.5.1 and scipy 1.13.1 pinned per plan spec; numpy and pandas also pinned for reproducible builds
  - xfail(strict=True) chosen over pytest.raises(ImportError) for stub tests — strict mode means tests fail if they unexpectedly pass before implementation
  - GuardViolation takes guard_name + message + optional details dict so callers can identify which guard fired and extract numeric context
  - BootstrapModeError is a separate exception class (not a subtype of GuardViolation) to distinguish runtime bootstrap status from guard enforcement
metrics:
  duration: 3min
  completed_date: "2026-03-30"
  tasks_completed: 2
  files_created: 16
---

# Phase 03 Plan 00: Wave 0 Bootstrap — Dependencies, Package Structure, Stub Tests

Wave 0 bootstrapping: scikit-learn/scipy dependencies installed, models and validation package inits created, 4 custom exception classes defined, and 30 xfail stub tests written across 11 test files covering all Phase 3 modules.

## Tasks Completed

| Task | Name | Commit | Files |
|------|------|--------|-------|
| 1 | Update requirements.txt and create package structure | ce610e1 | requirements.txt, src/models/__init__.py, src/models/exceptions.py, src/validation/__init__.py |
| 2 | Write Wave 0 stub test files | 7723118 | 13 test files across tests/models/, tests/validation/, tests/integration/ |

## What Was Built

### Task 1: Dependencies and Package Structure

- Updated `requirements.txt` with scikit-learn==1.5.1, scipy==1.13.1, numpy==1.26.4, pandas==2.2.2
- Created `src/models/__init__.py` package init
- Created `src/validation/__init__.py` package init
- Created `src/models/exceptions.py` with 4 custom exception classes:
  - `GuardViolation(guard_name, message, details)` — hard-block for overfitting guards (D-07)
  - `InsufficientDataError(message, n_available, n_required)` — insufficient training data
  - `ModelNotFoundError(model_name, version)` — model not trained yet
  - `BootstrapModeError` — code path accessed during bootstrap mode (D-08)

### Task 2: Wave 0 Stub Tests

Created 11 stub test files + `tests/integration/__init__.py`:
- `tests/models/test_nba_game.py` — 3 tests: predict returns calibrated prob, raises ModelNotFoundError when untrained, moneyline and spread contexts
- `tests/models/test_nba_totals.py` — 2 tests: predict returns over probability, raises ModelNotFoundError when untrained
- `tests/models/test_nba_props.py` — 2 tests: predict points prop, all prop types supported
- `tests/models/test_weather_temp.py` — 3 tests: bracket probs sum to 1, threshold probability, bias correction applied (D-10)
- `tests/models/test_weather_precip.py` — 1 test: predict returns valid probability
- `tests/models/test_model_store.py` — 3 tests: save/load roundtrip, rollback restores previous version, bootstrap mode detection (D-08/D-09)
- `tests/validation/test_splitter.py` — 3 tests: temporal ordering, 60/20/20 ratio, holdout write protection
- `tests/validation/test_walk_forward.py` — 2 tests: per-window metrics, no future leakage
- `tests/validation/test_guards.py` — 7 tests: sample_size_gate (R6.3), dampening (R6.4), cooldown (R6.5), staleness (R6.6), significance (R6.7)
- `tests/integration/test_phase3_e2e.py` — 4 xfail stubs for e2e integration

**pytest result:** 30 tests collected, 26 xfailed, 4 skipped, 0 errors

## Deviations from Plan

None — plan executed exactly as written.

## Known Stubs

None — this plan IS the stub creation plan. All test files are intentionally stubbed (xfail) and will be implemented in plans 03-01 through 03-04.

## Self-Check: PASSED

Files verified:
- `src/models/exceptions.py` — exists, all 4 exception classes importable
- `src/models/__init__.py` — exists
- `src/validation/__init__.py` — exists
- `tests/models/test_nba_game.py` — exists, contains `test_predict_returns_calibrated_probability`
- `tests/validation/test_guards.py` — exists, contains `test_sample_size_gate_blocks_below_50`
- `tests/integration/test_phase3_e2e.py` — exists, collected by pytest

Commits verified:
- `ce610e1` — chore(03-00): install scikit-learn/scipy deps and create model package structure
- `7723118` — test(03-00): add Wave 0 stub test files for Phase 3 models and validation
