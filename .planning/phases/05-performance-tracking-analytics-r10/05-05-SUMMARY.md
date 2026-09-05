# 05-05 SUMMARY — daily report, reliability diagram, phase e2e

**One-liner:** R10.8 plus R10.2's image half, and the ROADMAP working test — which caught a real
Sharpe bug and forced an honest correction to what "simulate a week" can actually prove.

**Completed:** 2026-09-04 · 469 passed / 13 skipped / 0 failed (was 439/13/0) · +30 tests

## Delivered

| Artifact | Note |
|---|---|
| `performance.render_reliability_diagram()` | Agg backend selected before pyplot import; renders headless |
| `src/analytics/daily_report.py` | `build_report()` + CLI: `--date`, `--snapshot`, `--plot` |
| `tests/integration/test_phase5_e2e.py` | 30 tests |

## Bug found by running it: Sharpe was silently `n/a`

`build_report` rebuilt `daily_pnl` for the requested date only. The ALL TIME section's Sharpe reads
the whole daily series, so with one row it fell below the two-point minimum and reported `n/a` —
on a database holding a full week. It now calls `recompute_all_daily_pnl()` first and reports 3.44
on the seeded week. `test_sharpe_is_computed_not_na` pins it.

Worth noting how this was found: not by a test, but by running the CLI against real seeded data and
reading the output. The unit tests all passed while the report was quietly wrong.

## Correction: a simulated week cannot prove the planted patterns

The first e2e draft asserted `precipitation` shows negative P&L, since the seeder plants a 15%
overconfidence there. It failed: +190 cents. The test was wrong, not the code — at the 8
precipitation trades a seeded week produces, the P&L sign is a coin flip, and the seeder's manifest
correctly reported the positive figure.

This is the 05-01 sample-size finding resurfacing at the integration level, and it has a direct
consequence for Phase 6. The ROADMAP's Phase 5 working test says "simulate a week of paper trading",
which is the right scope for checking breakdowns reconcile. But **Phase 6's working test asks the
evaluator to diagnose these same planted patterns**, and a week does not carry the samples to
support either claim.

The tests were therefore split along what the data can actually support:

- `TestPerMarketTypeBreakdowns` — runs on the week and asserts *reconciliation*: every count, P&L
  and drift matches ground truth exactly, and the parts sum to the whole. This is what the ROADMAP
  asks for and it is fully verifiable at that volume.
- `TestPatternDetectionRequiresVolume` — runs on ~2000 trades and asserts the planted patterns
  become *detectable*: precipitation loses money, props and precipitation drift positive, calibrated
  types stay flat, and the biased types rank above the calibrated ones. `test_week_alone_is_underpowered`
  documents the limitation explicitly rather than leaving it implicit.

**Phase 6 should seed at the higher volume for its diagnosis test**, or it will be asking an LLM to
find signal that is not present in the data.

## Design notes

- **Report is presentation only.** Every number comes from `performance`, `edge_decay` or
  `snapshot`, so the human report and the evaluator's snapshot cannot disagree.
- **`n/a`, never `None`.** `test_report_never_prints_raw_none` guards the formatting helpers;
  a bare `None` in a report is a bug, an explicit `n/a` is information.
- **Agg backend is selected before importing pyplot**, so rendering works over SSH and in CI.
- **Empty calibration bins are skipped, not plotted at zero** — plotting them would draw an observed
  frequency of 0 where there is simply no data.

## Sanity signal

The seeded week reports edge persistence at 24.4% against the seeder's
`PERSISTENT_EDGE_FRACTION = 0.25` — two independent code paths agreeing.
