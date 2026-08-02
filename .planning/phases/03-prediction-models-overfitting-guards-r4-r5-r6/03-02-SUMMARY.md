---
phase: 03-prediction-models-overfitting-guards-r4-r5-r6
plan: "02"
subsystem: weather-prediction-models-and-model-store
tags: [weather, nws, bias-correction, gaussian, model-versioning, joblib]
dependency_graph:
  requires: [03-00]
  provides: [src/models/weather_temp.py, src/models/weather_precip.py, src/models/model_store.py]
  affects: [03-01, 03-04, "Phase 4 edge_detector"]
tech_stack:
  added: []
  patterns: [additive bias correction per station x month (D-10/D-12), Gaussian fit for bracket probabilities (D-11), joblib + versions.json sidecar for model persistence]
key_files:
  created:
    - src/models/weather_temp.py
    - src/models/weather_precip.py
    - src/models/model_store.py
  modified:
    - tests/models/test_weather_temp.py
    - tests/models/test_weather_precip.py
    - tests/models/test_model_store.py
decisions:
  - WeatherTempModel learns per-station x per-month (offset, sigma) bias corrections from NOAA historical data, falling back to (0.0, 5.0) when fewer than min_observations are available (bootstrap-safe)
  - predict_brackets() centers a Gaussian (scipy.stats.norm) on the bias-corrected NWS point forecast and integrates over each bracket, then renormalizes so probabilities sum to exactly 1.0
  - WeatherPrecipModel combines NWS PoP with historical base rates, lead-time weighted (R5.5)
  - ModelStore uses joblib for model/calibrator artifacts and a versions.json sidecar (not SQLite) for version metadata — simpler rollback via list index manipulation, no DB dependency for model loading
  - ModelStore.is_bootstrap_mode() returns True whenever no active version exists OR the active version's recorded n_oos_predictions < 50
metrics:
  duration: unknown (direct commit, not executed via /gsd:execute-phase)
  completed_date: "2026-04-02"
  tasks_completed: 1
  files_created: 3
---

# Phase 03 Plan 02: Weather Models + ModelStore Versioning

Implemented the weather prediction models (temperature brackets via NWS ensemble + bias correction; precipitation threshold) and the `ModelStore` versioning layer shared by all five Phase 3 prediction models.

## Reconciliation Note

This plan's code was written and committed directly (commit `e720c4d`, 2026-04-02) without going through `/gsd:plan-phase` → `/gsd:execute-phase`. This SUMMARY.md was created retroactively during a `/gsd:resume-work` session on 2026-08-02 to bring GSD's tracking in line with what was actually built. No functional changes were needed to satisfy this plan's scope specifically.

## What Was Built

- `src/models/weather_temp.py` — `WeatherTempModel`, singleton `weather_temp_model`. `fit_bias_correction(min_observations=30)` learns per-station x per-month offset/sigma from NOAA data; `predict_brackets(station, forecast_date, brackets=None)` returns a dict of bracket → probability summing to ~1.0; `predict_threshold(station, threshold_f, forecast_date)` returns `P(high > threshold)`.
- `src/models/weather_precip.py` — `WeatherPrecipModel`. Combines NWS probability-of-precipitation with historical base rates.
- `src/models/model_store.py` — `ModelStore(model_name, store_dir="data/models")`. `save()`, `load_latest()`, `get_active_version()`, `rollback()`, `is_bootstrap_mode()`. Persists trained models as versioned joblib files under `data/models/<name>/` with a `versions.json` metadata sidecar.

## Deviations from Plan

- Executed as a direct commit rather than through the GSD execute pipeline — no per-task commits, no automated verification loop at execution time. Verified retroactively instead (see below).
- `ModelStore.__init__` does not accept a `db_path` parameter (only `model_name` and `store_dir`) — version metadata lives entirely in the joblib/`versions.json` sidecar, not SQLite. This was discovered during retroactive verification (03-04 integration tests originally assumed a `db_path` kwarg and were adjusted to match).

## Verification (retroactive, 2026-08-02)

- `python -m pytest tests/models/test_weather_temp.py tests/models/test_weather_precip.py tests/models/test_model_store.py -q` — all green
- Human-checkpoint sanity script (bias-corrected bracket probabilities for KNYC) confirmed probabilities sum to 1.0 and all fall in `[0, 1]`
- Full suite (`python -m pytest -q`) green after this plan plus 03-01/03-03/03-04: 253 passed, 13 skipped, 0 failed
