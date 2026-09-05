---
gsd_state_version: 1.0
milestone: v1.0
milestone_name: milestone
current_phase: 06
current_plan: 0
status: ready to plan
last_updated: "2026-09-04T00:00:00.000Z"
last_activity: 2026-09-04
progress:
  total_phases: 7
  completed_phases: 5
  total_plans: 19
  completed_plans: 19
---

# Project State

**Current Phase:** 06
**Status:** Ready to plan (Phase 5 complete)
**Last Activity:** 2026-09-04
**Current Plan:** 0

## Current Position

Phases 01-05 complete (19/19 plans). Phase 06 (Evaluator Bot [R11, R12]) not yet planned.

Phase 05 was planned and executed directly on 2026-09-04 without the GSD agent workflow, at the
user's request (token cost). Artifacts were written by hand and match the existing format:
05-CONTEXT.md plus PLAN/SUMMARY pairs for 05-01 through 05-05. Full suite: 469 passed, 13 skipped,
0 failed (was 253/13/0 at the start of the phase).

Phase 04 (Trading Engine) code was implemented directly on 2026-04-02 (commit `4a8c6c1`) without going through `/gsd:plan-phase`/`/gsd:execute-phase`, which left this file's frontmatter claiming completion while ROADMAP.md, SUMMARY.md files, and the phase-04 directory itself were never created. This was reconciled on 2026-08-02 during a `/gsd:resume-work` session: retroactive PLAN.md/SUMMARY.md pairs were written for 03-01 through 03-04 and 04-01, ROADMAP.md checkboxes were corrected, and two real functional gaps discovered in Phase 3 during that audit were closed (see Decisions below) rather than just papered over. Full test suite: 253 passed, 13 skipped, 0 failed.

Completed all 3 plans in Phase 01. Phase 01 complete: Config singleton, RSA-PSS auth, SQLite schema + database layer, full KalshiClient with market discovery, orderbook, order placement, and portfolio operations. All 16 unit tests pass.

## Decisions

- RSA key fixture is session-scoped (generate once per test session) — avoids 2048-bit keygen overhead per test
- Integration tests grouped in TestIntegration class with pytestmark so -m integration selects cleanly
- skip_without_integration is explicit fixture (not autouse) — makes integration dependency visible in test signature
- python-dotenv pinned at >=1.0.1; installed 1.2.2 (latest, compatible)
- Config(env_override=) parameter avoids importlib.reload() side-effects in tests where load_dotenv bleeds across test boundaries
- test_auth_strips_query_string uses time.time mock + cryptographic signature verification instead of patching Rust extension sign() method (read-only)
- _load_config() returns None on missing env vars so module-level singleton does not crash CI imports; tests instantiate Config() directly after monkeypatching
- [Phase 01]: yes_price always sent on YES side; NO orders use 100-price_cents per Kalshi API convention
- [Phase 01]: post_only=True default on place_limit_order enforces maker-only bias per design decision D-02
- [Phase 02]: CACHE_DIR is a module-level attribute so tests can monkeypatch without changing function signatures
- [Phase 02]: .gitignore data/ anchored to /data/ so src/data and tests/data are tracked as source code
- [Phase 02]: cache_get_stale ignores TTL entirely — returns any cached value as upstream-failure fallback (D-04)
- [Phase 02-data-pipelines-nba-weather-r2-r3]: RuntimeError for missing NOAA_API_TOKEN is pre-validated before try/except so it propagates rather than being swallowed as a transient failure
- [Phase 02-data-pipelines-nba-weather-r2-r3]: NWS gridpoint resolution cached 86400s separately from forecast (3600s); grid cell assignments are permanent so longer TTL is correct
- [Phase 02]: ESPN JSON API as primary injury source with nba_api fallback per D-07
- [Phase 02]: GTD/Questionable players flagged confidence=low not dropped per D-08
- [Phase 02]: pipeline.py iterates STATIONS dict so adding a 5th station auto-includes it in both historical and daily refresh
- [Phase 02]: Integration tests use @pytest.mark.integration + skip_without_integration fixture — consistent with Phase 01 pattern
- [Phase 03]: xfail(strict=True) chosen over pytest.raises(ImportError) for stub tests — strict mode fails if tests unexpectedly pass before implementation
- [Phase 03]: GuardViolation takes guard_name + message + optional details dict — callers identify which guard fired and extract numeric context
- [Phase 03]: BootstrapModeError is separate from GuardViolation to distinguish runtime bootstrap status from guard enforcement
- [Phase 03]: NBA/weather models use GradientBoostingClassifier + CalibratedClassifierCV(method="sigmoid") for Platt calibration (D-05); all five models are module-level singletons (D-14)
- [Phase 03]: ModelStore persists via joblib + a versions.json sidecar, not SQLite — constructor only accepts model_name and store_dir, no db_path
- [Phase 03]: R6.8 regime-change guard implemented as check_regime(sample1, sample2) using scipy.stats.ks_2samp, reusing the 0.05 SIGNIFICANCE_THRESHOLD constant; added 2026-08-02 after being found missing from the 2026-04-02 direct commit
- [Phase 03]: force_override(guard_name, reason, model_name) requires a non-empty reason and logs a 'guard_override' row to the improvements table (D-07) — added 2026-08-02
- [Phase 03]: pipeline.py lives at src/models/pipeline.py (not src/validation/) and is the only code path allowed to call model train() methods (D-01) — added 2026-08-02
- [Phase 04]: Trading engine (edge_detector, position_sizer, risk_manager, order_manager, ledger) built directly against ROADMAP deliverables without a plan-phase pass; audited 2026-08-02 against every R7-R9 sub-requirement with no gaps found
- [Reconciliation 2026-08-02]: When code is committed directly without /gsd:plan-phase or /gsd:execute-phase, always diff the plan's must_haves/artifacts against what's actually on disk before writing a retroactive SUMMARY — Phase 3's "feat(03)" commit looked complete (144 tests passing) but was silently missing R6.8, metrics.py, and pipeline.py

- [Phase 05]: D5-01 recalibrate.py shadow-trains in memory and never calls ModelStore.save() or a model's train() — Phase 3's D-01 chokepoint survives; enforced by tests that monkeypatch both to raise
- [Phase 05]: D5-02 feature importances and model metadata go to SQLite (feature_importance table + the previously dead model_versions table); versions.json stays authoritative for artifact loading only, because R11.1 confines the evaluator to SQL reads
- [Phase 05]: D5-03 edge decay reads a dedicated price_observations table carrying hours_to_close, not repeated predictions rows — it captures markets that were scanned but never traded, which is where decay signal lives
- [Phase 05]: D5-04 calibration is data-first — calibration_bins() returns dicts; render_reliability_diagram() is a thin Agg-backend layer, so core logic is testable headless
- [Phase 05]: D5-05 Sharpe uses daily P&L returns annualised by sqrt(252); per-trade Sharpe is ill-defined for binary contracts with varying hold times
- [Phase 05]: D5-06 the predictions<->outcomes join lives in exactly one helper (_settled_predictions), preferring prediction_id and falling back to the most recent prediction for the ticker
- [Phase 05]: D5-07 analytics never enumerates market types — all breakdowns use SELECT DISTINCT, and the seeder plants a third domain (cbb_games) so per-market-type code is proven not to assume {nba, weather}
- [Phase 05]: Added Database.transaction() — execute() opens a connection per statement, which made bulk writes pathological (15s for 84 seeded trades, now 0.01s)
- [Phase 05]: Extracted build_training_data() from train() in nba_game/nba_totals/nba_props so recalibration fits candidates on identical features; building data is not training, so D-01 is intact
- [Phase 05]: The weather models are not ML models — WeatherTempModel fits an additive bias correction, WeatherPrecipModel blends a historical base rate with NWS PoP. Neither has feature_importances_, so R10.4 covers only the six GBM-backed stores
- [Phase 05]: **Calibration-bias detection is sample-gated.** Drift standard error is sqrt(p(1-p)/n), so an 8% bias is ~1.1 sigma at n=50 and only separates cleanly around n=500. min_out_of_sample_predictions is 50 and R11.12 auto-rejects proposals under 30 samples — meaning Phase 6 would be asking an LLM to diagnose calibration drift inside its own noise floor. **Phase 6 must either raise the gate for calibration-type claims or make R12.7's prompt explicit that drift at n=30-50 cannot support a bias-correction proposal.** No locked Phase 3 constant was changed.
- [Phase 05]: Phase 6's working test should seed ~2000 trades, not a week — the ROADMAP's "simulate a week" is sufficient to verify breakdowns reconcile but not to make the planted patterns detectable

## Performance Metrics

| Phase | Plan | Duration | Tasks | Files |
|-------|------|----------|-------|-------|
| 01 | 01-01 | 2min | 2 | 11 |
| 01 | 01-02 | 4min | 2 | 5 |
| 01 | 01-03 | 2min | 1 | 2 |
| Phase 02 P01 | 3min | 1 tasks | 13 files |
| Phase 02-data-pipelines-nba-weather-r2-r3 P03 | 5min | 2 tasks | 6 files |
| Phase 02 P02-04 | 4min | 1 tasks | 2 files |
| Phase 02 P05 | 2min | 2 tasks | 3 files |
| Phase 03 P03-00 | 3min | 2 tasks | 16 files |
| Phase 03 P03-01/02 | unknown (direct commit) | n/a | 18 files |
| Phase 03 P03-03/04 gap-closure | ~65min | 3 tasks | 4 files |
| Phase 04 P04-01 | unknown (direct commit) | n/a | 12 files |

## Sessions

Last session: 2026-09-04 — Phase 5 (Performance Tracking & Analytics, R10) planned and executed
directly without GSD agents at the user's request. All five plans complete: schema v3 + seeder,
performance.py, feature importance + edge decay, shadow recalibration + snapshot, daily report CLI
+ e2e. Suite grew 253 -> 469 passing, 13 skipped, 0 failed. Two bugs found by running code rather
than by tests (Sharpe silently n/a in the report; walk-forward generator escaping its try/except).
Also discussed expanding to other sports — conclusion recorded in 05-CONTEXT.md "Out of scope":
trading/analytics/evaluator layers already generalise, expansion cost sits in src/data/<sport>/ and
src/models/, and phases should be prioritised by settled-events-per-season because the evaluator
loop is sample-gated. Next: Phase 6 (Evaluator Bot) discuss/plan — read the two Phase 05 notes above
about sample size before designing its working test.
