# ROADMAP

## Milestone 1: Autonomous Trading Bot with Evaluator

### Phase 1: Kalshi API Client & Infrastructure [R1]

Build the Kalshi API client with RSA-PSS auth, market/event discovery across all categories (NBA games, props, futures; weather temperature, precip, severe), orderbook retrieval, order management, and portfolio tracking. Set up project skeleton: directory structure per CONVENTIONS.md, config management, `.env` loading, SQLite schema, GitHub remote, `.gitignore`, `requirements.txt`. All development targets demo API.

**GitHub Setup (do first):**
- Initialize git repo, create `.gitignore` (exclude `.env`, `data/`, `__pycache__/`, `*.pem`, `.venv/`)
- Create GitHub repo `kalshi-bot` (public or private per user preference)
- Add remote, push initial commit with project skeleton
- All subsequent GSD commits push to `origin main` after each plan execution

**Deliverables:**
- Project skeleton matching CONVENTIONS.md directory structure
- `src/kalshi/client.py` — authenticated API client with all CRUD operations
- `src/kalshi/auth.py` — RSA-PSS request signing
- `src/config.py` — centralized config, env var loading, demo/prod URL switching
- `src/db/schema.sql` — full SQLite schema
- `src/db/database.py` — data access layer with schema versioning
- `.env.example` — template with all required env vars
- `trading_config.json` — default config per CONVENTIONS.md
- `requirements.txt` — pinned dependencies
- `tests/test_kalshi_client.py` — pytest tests for API client (mocked + demo integration)
- Working test: auth to demo API, discover NBA game + prop + futures markets, discover weather temperature markets, pull an orderbook, place and cancel a test limit order

**Plans:** 3 plans

Plans:
- [x] 01-01-PLAN.md — Project skeleton and Wave 0 test scaffolding
- [x] 01-02-PLAN.md — Config singleton, RSA-PSS auth, SQLite schema + database layer
- [x] 01-03-PLAN.md — Kalshi API client with all CRUD operations

### Phase 2: Data Pipelines (NBA + Weather) [R2, R3]

Build data ingestion for both domains. NBA pipeline: team stats, player stats (per-game averages + game logs), schedules, injuries, historical results, ELO tracker, matchup context (opponent defensive rating vs position). Weather pipeline: NWS probabilistic forecasts, ensemble data, station-to-ticker mapping (KNYC, KMDW, KMIA, KAUS), NOAA historical, multi-model comparison. Both pipelines cache responses.

**Deliverables:**
- `data/nba/teams.py` — team stats, schedule, ELO, rest tracking
- `data/nba/players.py` — player stats, game logs, injury reports, matchup context
- `data/nba/history.py` — historical game results for training
- `data/weather/nws.py` — point forecasts, ensemble/gridpoint data, multi-model pull
- `data/weather/noaa.py` — historical daily data
- `data/weather/station_map.py` — Kalshi ticker → NWS station mapping
- `data/cache.py` — TTL-based response caching
- Working test: pull today's NBA schedule with team stats + player props data for a specific game, pull NYC temperature forecast with ensemble probabilities, map both to Kalshi market tickers

**Plans:** 5/5 plans complete

Plans:
- [x] 02-01-PLAN.md — Cache module, schema additions, config updates, dependency install
- [x] 02-02-PLAN.md — NBA teams, schedule, ELO, rest tracking, historical results
- [x] 02-03-PLAN.md — Weather pipeline: station map, NWS forecasts, NOAA historical
- [x] 02-04-PLAN.md — NBA players: stats, game logs, injuries, matchup context
- [x] 02-05-PLAN.md — CLI entry point (--refresh-history) and integration tests

### Phase 3: Prediction Models + Overfitting Guards [R4, R5, R6]

Build prediction engines and the overfitting protection system. NBA: game model (moneyline/spread via ELO + team stats + injuries), totals model (pace-adjusted efficiency), player prop model (per-player projection with opponent matchup). Weather: temperature bracket model (NWS ensemble + bias correction), precipitation model. All models output calibrated probabilities.

Overfitting guards: train/test/holdout splitter with temporal ordering, walk-forward validator, minimum sample size gates, parameter change dampening, cooldown tracker, staleness detector, significance tests, regime change detection.

**Deliverables:**
- `models/nba_game.py` — moneyline/spread logistic regression + gradient boosted model
- `models/nba_totals.py` — over/under pace-adjusted projection
- `models/nba_props.py` — player prop projections (pts, reb, ast, 3pm)
- `models/weather_temp.py` — temperature bracket probabilities with bias correction
- `models/weather_precip.py` — precipitation threshold probabilities
- `models/model_store.py` — versioning, persistence, rollback
- `validation/splitter.py` — train/test/holdout with temporal ordering, holdout write-protection
- `validation/walk_forward.py` — rolling window validation, per-window metrics
- `validation/guards.py` — sample size gate, dampening, cooldown, staleness, significance test, regime detection
- Working test: predict tonight's NBA games (moneyline + total + 3 player props), predict tomorrow's NYC temperature bracket, run walk-forward validation on historical data, verify holdout is never touched during training

**Plans:** 5 plans

Plans:
- [ ] 03-00-PLAN.md — Wave 0: test scaffolds, exceptions, scikit-learn/scipy install
- [ ] 03-01-PLAN.md — NBA models: nba_game, nba_totals, nba_props with Platt calibration
- [ ] 03-02-PLAN.md — Weather models: weather_temp, weather_precip, model_store versioning
- [ ] 03-03-PLAN.md — Validation system: splitter, walk_forward, guards (6 checks), metrics, pipeline CLI
- [ ] 03-04-PLAN.md — Integration tests: Phase 3 end-to-end + human checkpoint

### Phase 4: Trading Engine (Edge Detection + Paper Trading) [R7, R8, R9]

Build the trading logic. Edge detector compares model probabilities to market prices across ALL market types, applies per-type minimum edge thresholds and liquidity filters, ranks signals by quality. Position sizer applies fractional Kelly with per-type caps and correlation limits. Paper trading ledger records everything to SQLite and integrates with Kalshi demo API for realistic execution.

**Deliverables:**
- `trading/edge_detector.py` — model vs market comparison, signal generation, market type ranking
- `trading/position_sizer.py` — fractional Kelly, per-type caps, correlation awareness, allocation limits
- `trading/order_manager.py` — submit limit orders to Kalshi demo, track fills
- `trading/risk_manager.py` — daily loss limit, position limits, kill switch
- `trading/ledger.py` — SQLite trade journal with market_type tagging
- Working test: scan all open NBA + weather markets (games, props, futures, temperature, precip), rank by edge × liquidity × confidence, paper trade top signals as limit orders, log to database

### Phase 5: Performance Tracking & Analytics [R10]

Build the analytics foundation that both human and evaluator bot consume. Brier scores per model per market type, calibration plots, P&L analytics with market type breakdown, feature importance, edge decay analysis. Performance snapshot generator outputs structured JSON for evaluator. Daily CLI report.

**Deliverables:**
- `analytics/performance.py` — Brier scores (overall, rolling, per-model, per-market-type), calibration plots, P&L curves, Sharpe
- `analytics/snapshot.py` — structured JSON performance snapshot (the evaluator's data contract)
- `analytics/recalibrate.py` — retrain on latest data, compare on test set (not holdout), run walk-forward
- `analytics/edge_decay.py` — track whether edges persist or close pre-settlement
- `analytics/daily_report.py` — CLI daily summary with market type breakdown
- Working test: simulate a week of paper trading, generate full performance report + snapshot JSON, verify per-market-type breakdowns are correct

### Phase 6: Evaluator Bot [R11, R12]

Build the separate evaluator process. Reads SQLite, sends structured snapshots to Claude, receives improvement proposals as JSON, manages the improvement queue. Includes auto-approval for safe config tweaks, human review CLI, improvement tracking, rollback detection, and evaluator self-tracking. Anti-overfitting protections: minimum sample size enforcement on proposals, cooldown enforcement, dampening enforcement, explicit anti-overfitting instructions in the LLM prompt.

**Deliverables:**
- `evaluator/evaluator.py` — main entry point, schedule or manual
- `evaluator/monitor.py` — watches for settled trades, triggers evaluation
- `evaluator/diagnosis.py` — build report from snapshot, call LLM via provider interface, parse JSON response
- `evaluator/llm_provider.py` — abstracted LLM interface with implementations for Gemini (default), Claude, OpenAI. Swap via `EVALUATOR_LLM_PROVIDER` env var
- `evaluator/prompts/system_prompt.md` — quant analyst role with anti-overfitting instructions and few-shot examples
- `evaluator/prompts/report_template.md` — structured template filled with real data
- `evaluator/schemas/improvement_schema.json` — JSON schema for proposals
- `evaluator/actions.py` — improvement queue manager, auto-approval within bounds
- `evaluator/applicator.py` — apply approved improvements to trading bot config
- `evaluator/rollback.py` — detect post-improvement degradation, propose rollback
- `evaluator/self_tracker.py` — track evaluator suggestion hit rate, observation-only mode trigger
- `evaluator/review_cli.py` — `python evaluator.py --review` interactive approval
- `evaluator/cost_tracker.py` — LLM API token usage tracking, monthly budget cap
- `evaluator_config.json` — safe bounds, cooldowns, budget, trigger thresholds
- Working test: seed database with 60+ simulated trades with known patterns (e.g., NBA prop model overconfident by 8%, weather model underconfident on cold days, one market type consistently losing). Run evaluator. Verify it: (a) correctly diagnoses the patterns, (b) proposes bias correction for props, (c) proposes market exclusion for the losing type, (d) rejects a proposal with < 30 sample trades, (e) auto-approves safe tweaks, (f) flags risky changes for human review

### Phase 7: Production Migration [R13]

Production safeguards. Config-driven env switch. Pre-flight checks. Gradual rollout (10% position sizes for first 50 trades). Live alerting. Kill switch. Slippage analysis. Evaluator runs identically in live mode.

**Deliverables:**
- `config.py` updates — env-driven switch with pre-flight checks
- `trading/kill_switch.py` — emergency halt
- `analytics/slippage.py` — live vs paper comparison
- `main.py` — unified entry point: `python main.py --mode paper|live --markets nba,weather --market-types all|games|props|futures|temperature|precip`
- Working test: switch to production API, verify auth, place minimum-size trade with real money, confirm fill matches expectations, verify evaluator runs against live data
