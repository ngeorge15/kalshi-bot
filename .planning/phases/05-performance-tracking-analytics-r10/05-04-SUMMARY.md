# 05-04 SUMMARY — recalibrate.py + snapshot.py

**One-liner:** R10.5 as a comparison that provably cannot deploy, and R10.7 as a versioned,
domain-agnostic JSON contract for Phase 6.

**Completed:** 2026-09-04 · 439 passed / 13 skipped / 0 failed (was 389/13/0) · +50 tests

## Delivered

| Artifact | Lines | Tests |
|---|---|---|
| `src/analytics/recalibrate.py` | 268 | 26 |
| `src/analytics/snapshot.py` | 264 | 24 |

## Deviation: extracted `build_training_data()` from three models

**Not in the plan, and it touches Phase 3 files.**

R10.5 needs to fit a candidate on the same features the incumbent was built from, but D-01
reserves `train()` for `pipeline.py`. The alternative — rebuilding feature assembly inside
`recalibrate.py` — is not merely duplication: the moment the two implementations drifted, the
comparison would be scoring a candidate trained on *different features* against the incumbent and
reporting the difference as a data-recency effect. That is a silently wrong answer, which is worse
than no answer.

So the `[build X, y]` block was lifted out of `train()` in `nba_game.py`, `nba_totals.py` and
`nba_props.py` into a `build_training_data()` method, with `train()` now calling it. Pure
extraction, no behaviour change; `train()` still owns splitting, fitting and saving. **D-01 is not
weakened** — building data is not training, and `recalibrate.py` still never calls `train()` or
`ModelStore.save()`. Verified by the existing model tests (30 passed, 3 skipped) before proceeding.

## The no-persist guarantee is tested, not just documented

D5-01 is only worth anything if it holds under change. Three tests enforce it:

- `test_never_persists` monkeypatches `ModelStore.save` to raise `AssertionError`, then runs a full
  recalibration
- `test_never_calls_model_train` does the same to `NBAGameModel.train`
- `test_split_is_temporal_not_shuffled` captures the training matrix and asserts it is exactly
  `X[:320]` — a shuffled split would leak future data into the candidate and flatter it

## Design notes

- **`MIN_BRIER_IMPROVEMENT = 0.005`.** A candidate must clear the incumbent by this margin to be
  recommended. Brier differences below it are indistinguishable from resampling noise at these
  sample sizes, and acting on them is precisely how a model gets fitted to its own test split.
  `test_marginal_improvement_holds` pins that a tie recommends "hold" at the default threshold.
- **Two bugs caught by reading the `WalkForwardValidator` contract** rather than assuming it:
  `validate()` returns a *generator*, so the `try/except` had to wrap `list(...)` or errors would
  escape the guard entirely; and each window's `metrics` is the dict returned by `test_fn`, not a
  scalar. Both were wrong in the first draft.
- **The snapshot is versioned** (`SNAPSHOT_SCHEMA_VERSION = 1`) so Phase 6's parsing can evolve
  without silently misreading an older file.
- **`generate_snapshot` rebuilds `daily_pnl` itself** rather than assuming someone ran the
  aggregator — otherwise Sharpe would silently report `None` on a fresh database.
- **Config absence is not an error.** The `config` singleton is `None` without Kalshi credentials,
  the normal state in tests; the snapshot records `None` and carries on.
- **`recalibrate_all` isolates failures** so one dead data source cannot abort the sweep.

## D5-07

`test_new_market_type_appears_without_code_change` inserts an `nfl_props` prediction and outcome
into an otherwise empty database and asserts both `by_market_type.brier` and `by_model` pick it up.
That is the milestone-2 sport expansion working end-to-end through the evaluator's contract, with
no code in Phase 5 or Phase 6 aware the sport exists.
