# 05-03 SUMMARY — Feature importance + edge decay

**One-liner:** R10.4 and R10.6 delivered, plus the discovery that R10.4 applies to six model
stores rather than all five models.

**Completed:** 2026-09-04 · 389 passed / 13 skipped / 0 failed (was 335/13/0) · +54 tests

## Delivered

| Artifact | Contents |
|---|---|
| `src/analytics/performance.py` (+314 lines) | `gbm_feature_names`, `extract_feature_importances`, `record_feature_importances`, `record_model_version`, `get_importance_history`, `importance_drift` |
| `src/analytics/edge_decay.py` (288 lines) | `record_observation`, `decay_curve`, `decay_by_bucket`, `edge_persistence`, `decay_by_market_type`, `summarize_decay` |
| `tests/analytics/test_feature_importance.py` | 28 tests |
| `tests/analytics/test_edge_decay.py` | 26 tests |

## Finding: the weather models are not ML models

The 05-01 plan noted "weather models have no FEATURE_NAMES constant" and assumed the fix was to
add one. Inspecting them showed the real situation: **neither weather model holds a scikit-learn
estimator at all.**

- `WeatherTempModel` — `fit_bias_correction()` computes an additive forecast bias adjustment
- `WeatherPrecipModel` — blends a historical base rate with the NWS probability of precipitation

Neither exposes `feature_importances_`, so R10.4 simply does not apply to them. Confirmed against
`pipeline._train_single`, which for these two calls `fit_bias_correction()` / constructs the model
and returns early, never touching `ModelStore.save()`.

R10.4's real scope is the six GBM-backed stores: `nba_game`, `nba_totals`, and
`nba_props_{pts,reb,ast,3pm}` (props keeps a separate store per prop type). `gbm_feature_names`
raises a clear `ValueError` for the weather models rather than returning an empty list, and a test
asserts that.

**No FEATURE_NAMES constants were added to the weather models** — the 05-01 plan's note was wrong,
and adding names for a model with no feature vector would have been fiction.

## Design notes

- **Fold averaging is verified, not assumed.** `CalibratedClassifierCV(cv=3)` keeps importances on
  three inner estimators at `calibrated_classifiers_[i].estimator` (confirmed against sklearn
  1.5.1). `test_matches_manual_fold_average` recomputes the average independently, which would
  catch the easy bug of reading only fold 0.
- **Mismatched feature counts raise.** Silently zipping 5 importances against 7 names would
  mislabel every feature and quietly poison the evaluator's reasoning.
- **`record_model_version` finally populates `model_versions`,** dead since schema v1. `versions.json`
  stays authoritative for loading artifacts; SQL now carries the metadata because R11.1 confines the
  evaluator to SQLite reads.
- **Edge decay measures magnitude, not signed edge.** A mispricing in either direction is signal;
  `test_sign_flip_counts_by_magnitude` pins that an edge inverting from +20 to -20 has not decayed.
- **Tiny edges are excluded from persistence stats** (`MIN_INITIAL_EDGE_CENTS = 3`) — proportional
  decay of a 1-cent edge is noise.
- **`record_observation` is deliberately in the analytics layer, not the ledger.** The ledger records
  what we did; observations record what the market did, including in markets we never touched.

## D5-07

`test_accepts_unknown_market_type` records an `nfl_props` observation and asserts it round-trips —
direct evidence that the decay layer takes a new sport with no code change.
