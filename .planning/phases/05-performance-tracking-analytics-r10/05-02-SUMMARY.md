# 05-02 SUMMARY — performance.py

**One-liner:** All of R10.1-R10.3 in one module built on a single join helper, plus the
`daily_pnl` aggregator that Phase 4 left unwritten.

**Completed:** 2026-09-04 · 335 passed / 13 skipped / 0 failed (was 291/13/0) · +44 tests

## Delivered

`src/analytics/performance.py` (486 lines), `tests/analytics/test_performance.py` (44 tests).

| Requirement | Functions |
|---|---|
| R10.1 | `brier_summary`, `brier_by_market_type`, `brier_by_model`, `rolling_brier` |
| R10.2 | `calibration_bins` |
| R10.3 | `pnl_curve`, `win_rate`, `edge_on_wins_vs_losses`, `pnl_by_market_type`, `drawdown`, `sharpe_ratio` |
| gap | `recompute_daily_pnl`, `recompute_all_daily_pnl` |

## Design notes

- **One join, one place (D5-06).** `_settled_predictions()` prefers `outcomes.prediction_id` and
  falls back to a correlated subquery taking the *most recent* prediction for the ticker. Outcomes
  with no resolvable prediction are dropped and counted in a debug log rather than silently
  vanishing or crashing. Both paths have direct test coverage.
- **Empty is not an error.** Every metric returns `n: 0` with `None` values on an empty slice.
  `src.validation.metrics.brier_score` raises on empty arrays, so each caller guards before
  delegating. This matters because the evaluator will query market types that have no settled
  trades yet.
- **`sharpe_ratio` returns `None`, not `inf`,** when the daily series has zero variance or fewer
  than two days. An undefined ratio reported as a number would be a real hazard once Phase 6 starts
  feeding these figures to an LLM.
- **`calibration_bins` has a fixed shape** — always `n_bins` entries regardless of data, with the
  final bin closed on the right so `p == 1.0` is not silently dropped. Stable shape keeps the
  plotting layer and the snapshot schema simple.
- **Scalars delegate** to `src.validation.metrics` rather than being recomputed, so the analytics
  layer and the walk-forward validator cannot drift apart.
- **D5-07 held:** market types come from `SELECT DISTINCT`. `test_third_domain_present_in_breakdown`
  asserts `cbb_games` reaches the P&L breakdown with no code aware of it.

## Verification highlights

`test_drift_matches_seeder_manifest` closes the loop end to end: the seeder records what it planted,
and the analytics layer independently recovers the same number through SQL. That is the property
Phase 6 depends on.

Metrics are asserted against hand-computed values (Brier 0.20 from p=0.8/yes and p=0.6/no; Sharpe
against an independent numpy computation), not merely against types.

## Note

Suite runtime rose 18s -> 49s. No single test exceeds 0.9s; the cost is `Database()` re-running
`executescript` per instantiation and opening a connection per query, multiplied across fixtures.
Not addressed — it is a test-ergonomics issue, not a production one.
