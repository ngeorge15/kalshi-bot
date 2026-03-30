# Phase 3: Prediction Models + Overfitting Guards - Context

**Gathered:** 2026-03-29
**Status:** Ready for planning

<domain>
## Phase Boundary

Build prediction engines for NBA (game moneyline/spread, totals over/under, player props) and weather (temperature brackets, precipitation), plus a comprehensive overfitting protection system (train/test/holdout splitter, walk-forward validator, guards: sample size gate, dampening, cooldown, staleness, significance tests, regime detection). All models output calibrated probabilities. Model store handles versioning, persistence, and rollback.

This phase does NOT include edge detection, position sizing, or trade signal generation — those are Phase 4.

</domain>

<decisions>
## Implementation Decisions

### Model Training Trigger
- **D-01:** Training is **CLI-only** — triggered manually via `python -m src.models.pipeline --train [--model nba_game|nba_totals|nba_props|weather_temp|weather_precip|all]`. No auto-training at startup or runtime.
- **D-02:** `predict()` always reads from model_store. It never triggers training. Clean separation of training vs. inference.
- **D-03:** `--train` supports **individual model selection** (`--model nba_game`, `--model weather_temp`, etc.) and `--model all`. Lets you retrain a single model without touching others.

### Probability Calibration
- **D-04:** Use **Platt scaling** (sigmoid calibration) applied to GBM output. Standard for gradient boosted models, cheap to compute, interpretable.
- **D-05:** Calibration is **always applied at predict() time** — predict() always returns Platt-calibrated probabilities. Raw GBM/LR scores are internal only. Downstream edge detector (Phase 4) always receives calibrated output.
- **D-06:** Calibration curves (Platt scaler parameters) are stored in `model_store.py` alongside trained model parameters, versioned with the model.

### Overfitting Guard Enforcement
- **D-07:** Guards are **hard blocks** — they raise `GuardViolation` exception, preventing deployment. A `--force` flag bypasses with a **mandatory reason string** that is logged to the `improvements` SQLite table. Creates accountability trail.
- **D-08:** During **bootstrap mode** (< 50 out-of-sample predictions), the bot trades but uses reduced/minimum position sizing. Guards log "bootstrap mode" status but do not block trading. Once 50 OOS predictions exist, full guard enforcement kicks in.
- **D-09:** Bootstrap threshold (50) and all other guard parameters (dampening 20%, cooldown 30 trades, staleness 30 days, significance p < 0.05) are the exact values from REQUIREMENTS.md §R6 — do not soften or relax them.

### Weather Temperature Model Architecture
- **D-10:** NWS systematic forecast error is corrected via **additive bias correction per station × month** — `correction_offset[station][month] = mean(actual_high - nws_forecast_high)` computed from 3 years of NOAA data. Applied as: `adjusted_forecast = nws_forecast + offset`.
- **D-11:** After bias correction, bracket probabilities are generated via a **Gaussian fit**: fit a normal distribution centered on the bias-corrected NWS point forecast, with standard deviation learned from historical forecast error variance per station. `P(bracket_i) = integral of N(mu, sigma) over bracket i's range`.
- **D-12:** The 12-month granularity (not 4-season) is used for correction offsets, capturing within-season variation. ~90 observations per station/month from 3 years of NOAA history is sufficient.

### Carrying Forward from Prior Phases
- **D-13:** All model code is **synchronous** (no asyncio) — consistent with Phase 1 D-02 and Phase 2 D-11.
- **D-14:** Model classes follow the **module-level singleton pattern** established in Phase 1 D-05.
- **D-15:** Integration tests for live model runs (with real data pull) are **opt-in** via `KALSHI_INTEGRATION=true`, marked `@pytest.mark.integration` — consistent with Phase 1 D-07 and Phase 2 D-13.
- **D-16:** The full SQLite schema (model_versions, holdout_results, improvements tables) was defined in Phase 1 D-06 and already exists in `src/db/schema.sql`. No new tables needed — write to existing schema.

### Claude's Discretion
- Feature engineering specifics — which nba_api/stat fields become model features, normalization, one-hot encoding strategy
- GBM hyperparameters (n_estimators, max_depth, learning_rate) — pick standard defaults, tune in later phases
- Logistic regression baseline details — feature selection, regularization strength
- Walk-forward window size — pick based on NBA season length and weather seasonality (research can advise)
- Regime change detection method — statistical test for distributional shift (KS test, CUSUM, etc.)
- Significance test implementation — binomial test for win rate, t-test for edge (standard implementations)
- Exact `model_store.py` serialization format — pickle + JSON metadata, or joblib, etc.

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Project Specs
- `.planning/CONVENTIONS.md` — directory structure (`src/models/`, `src/validation/`), Python conventions, config file format
- `.planning/REQUIREMENTS.md` §R4 — NBA model requirements (R4.1–R4.7): game model, totals, props, calibration, versioning
- `.planning/REQUIREMENTS.md` §R5 — Weather model requirements (R5.1–R5.7): temperature brackets, precip, bias correction, DST awareness
- `.planning/REQUIREMENTS.md` §R6 — Overfitting protection requirements (R6.1–R6.8): splitter, walk-forward, guards
- `.planning/ROADMAP.md` §Phase 3 — deliverables list and working test definition
- `.planning/PROJECT.md` — tech stack (scikit-learn, pandas, numpy), architecture overview

### Existing Code (patterns to follow)
- `src/config.py` — module-level singleton, `trading_config.json` loading
- `src/db/database.py` — SQLite data access layer (Database class with execute/fetchall)
- `src/db/schema.sql` — existing full schema; model_versions, holdout_results, improvements tables already defined
- `src/data/nba/teams.py` — get_team_stats(), get_todays_schedule(), EloTracker (data source for NBA game model)
- `src/data/nba/players.py` — get_player_stats(), get_player_game_logs(), get_matchup_context() (data source for player prop model)
- `src/data/nba/history.py` — get_historical_results() (training data for NBA models)
- `src/data/weather/nws.py` — get_nws_forecast(), get_nws_griddata() (input to weather_temp model)
- `src/data/weather/noaa.py` — NOAA historical in SQLite (training data for bias correction)
- `src/data/weather/station_map.py` — STATIONS dict (KNYC, KMDW, KMIA, KAUS lat/lon)

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `src/db/database.py` — `Database` class with `execute()` / `fetchall()`. Model store reads/writes to `model_versions`, `holdout_results` tables via this interface.
- `src/data/cache.py` — `cache_get/cache_set/cache_get_stale`. Feature assembly calls can use this to cache daily feature vectors.
- `src/config.py` — `config.cache` for TTL values, `config.trading` for guard thresholds.
- All data modules (`teams.py`, `players.py`, `history.py`, `nws.py`, `noaa.py`) — direct data providers for feature assembly.

### Established Patterns
- Synchronous Python throughout — no asyncio or httpx in model code
- Module-level singleton: `from src.models.nba_game import nba_game_model` (consistent with data modules)
- `@pytest.mark.integration` + `skip_without_integration` fixture for any test requiring live data

### Integration Points
- `src/models/` → reads from `src/data/` (feature assembly) and `src/db/database.py` (training data + model persistence)
- `src/validation/` → reads from `src/db/database.py` (holdout data, cooldown tracker)
- Phase 4 (`src/trading/edge_detector.py`) will call `predict()` on model instances — the predict() API must be consistent across all 5 model modules

</code_context>

<specifics>
## Specific Ideas

- `predict()` API should be consistent across all models — downstream edge_detector.py (Phase 4) needs a uniform interface
- The `--force` override for guard violations must log reason string to SQLite `improvements` table — creates accountability trail for the evaluator bot (Phase 6) to review
- DST awareness for weather temperature markets (R5.7): Kalshi uses local standard time, not midnight-to-midnight — weather_temp.py must account for this in feature assembly (use local standard time cutoff for "high temperature of day")
- Bootstrap mode (< 50 OOS predictions) reduces position sizing but does not block trading — the position sizer (Phase 4) reads bootstrap status from model_store

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope.

</deferred>

---

*Phase: 03-prediction-models-overfitting-guards-r4-r5-r6*
*Context gathered: 2026-03-29*
