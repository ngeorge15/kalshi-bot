# 05-01 SUMMARY — Analytics foundation

**One-liner:** Schema v3 (`feature_importance`, `price_observations`), the `src/analytics/`
package, a deterministic paper-trading seeder with recoverable planted biases, and a
`Database.transaction()` context manager that made the seeder ~1500x faster.

**Completed:** 2026-09-04 · 291 passed / 13 skipped / 0 failed (baseline was 253/13/0)

## Delivered

| Artifact | Note |
|---|---|
| `src/db/schema.sql` | v3: `feature_importance`, `price_observations` + 3 indexes. Additive only; v1/v2 untouched |
| `src/db/database.py` | New `transaction()` context manager (see deviation below) |
| `src/analytics/__init__.py` | Package skeleton |
| `scripts/seed_test_data.py` | 388 lines; `seed_database()` + CLI with `--force` guardrail |
| `tests/analytics/test_seed_data.py` | 38 tests across 6 classes |
| `requirements.txt` | `matplotlib==3.9.2` pinned |

## Deviations from plan

**Added `Database.transaction()` — not in the plan.** `Database.execute()` opens and closes a
connection per statement. The seeder issues 10 writes per trade, so 84 trades took ~15s and the
higher volumes the bias tests turned out to need would have taken minutes. A context manager
yielding one connection (preserving `cursor.lastrowid` for row linking, rolling back the batch on
error) brought 84 trades to 0.01s and 2000 trades to ~0.18s. General-purpose; benefits any future
bulk write.

## Finding: calibration bias detection is sample-gated

The first version of the bias tests failed, and the cause was not the threshold — it was
statistics. Drift (`mean_predicted_prob - actual_frequency`) is an unbiased estimator of the
planted bias, but its standard error is `sqrt(p(1-p)/n)`:

| n per market type | SE | 0.08 bias detectable? |
|---|---|---|
| 21 | 0.109 | No — invisible |
| 50 | 0.070 | 1.1 sigma — marginal |
| 160 | 0.039 | 2 sigma — borderline |
| 500 | 0.022 | 3.6 sigma — clean |

Measured across 8 seeds at n~509, props drift stayed in `[+0.042, +0.115]` while calibrated types
stayed inside `+/-0.04`.

**This has a consequence beyond the seeder.** `trading_config.json` sets
`validation.min_out_of_sample_predictions: 50`, and R11.12 auto-rejects evaluator proposals under
30 samples. At those sizes an 8% calibration bias is roughly 1 sigma — inside the noise. Phase 6
will be asking an LLM to diagnose exactly this kind of pattern, so either the gate needs raising
for calibration-type claims or the evaluator prompt (R12.7) must be explicit that a drift estimate
at n=30-50 cannot support a bias-correction proposal. **Flagged for Phase 6; not changed here** —
altering a locked Phase 3 guard constant is out of scope for this plan.

Tests therefore split: structural assertions run against the default 84-trade week, bias
assertions against a ~2000-trade fixture parameterised over 3 seeds so thresholds are not tuned to
one draw.

## D5-07 verification (domain-agnostic)

Confirmed by grep during planning that `src/trading/` contains zero hardcoded domain strings and
`market_type` is free-form TEXT with no CHECK constraint. The seeder plants a third domain
(`cbb_games`, weight 0.10) alongside NBA and weather, and `TestDomainAgnostic` asserts it reaches
all four tables — so per-market-type code in 05-02 onward is proven not to assume two domains.

## Notes for later plans

- `_seed_price_observations` writes both decaying and persistent edge populations
  (`PERSISTENT_EDGE_FRACTION = 0.25`); 05-03 needs both to exist.
- All `outcomes` rows carry non-null `prediction_id` and `trade_id`, so D5-06's preferred join is
  exercised. The ticker fallback still needs its own coverage in 05-02.
- Seeder P&L is arithmetically derived from fill price and settlement, never hand-set — asserted
  by `test_pnl_is_arithmetically_consistent`.
