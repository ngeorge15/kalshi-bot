# Audit and work plan — 2026-09-04

Scope: repository review focused on exchange transport, configuration, execution,
sizing, ledger lifecycle, and model validation boundaries. This is not a full
security or profitability certification. Existing work reaches Phase 5 analytics;
there is no unified trading entry point or evaluator package yet.

## Findings and changes implemented

| Priority | Finding | Resolution |
| --- | --- | --- |
| High | Any environment other than `demo` selected production, including typos. | Reject values outside `demo` and `production`. |
| High | Order/position queries read only the first page, so cancellation and exposure calculations could omit orders/positions. | Fetch all pages, retain filters, reject repeated cursors. |
| High | POST status/read retries could replay an order after an ambiguous failure; requests could wait indefinitely. | Exclude POST from status/read retries; attach unique client order IDs; add 5-second connect/30-second read timeouts. Connection retries remain enabled. |
| High | Sizing forced at least one contract even when the Kelly budget could not afford it. | Floor to affordable quantity; also cap by available cash. |
| Medium | Invalid side, quantity, or price reached the exchange. | Validate integral price/count and side/action before submitting. |
| Medium | A local `.env` backup with a suffix was visible as untracked. | Ignore `.env*`, preserving `.env.example`; no credential contents inspected. |

## Remaining findings

1. **High — current API contract migration.** `src/kalshi/client.py` uses legacy
   create/cancel endpoints and historical base URLs. Official documentation describes
   `/portfolio/events/orders` using bid/ask and fixed-point strings, and says the
   legacy creation endpoint will be deprecated no earlier than May 6, 2026. This
   does not establish whether the legacy endpoint is currently disabled. Migrate
   create/cancel together, normalize responses, and verify demo contracts.
2. **High — fill accounting and restart recovery.** `OrderManager.check_fills()`
   reads only 50 fills and marks an order executed after any fill. There is no
   persisted fill-ID deduplication, cumulative fill quantity, fee accounting, or
   durable submission intent before HTTP. A timeout is recorded as failed even
   when exchange acceptance is unknown. Generated client IDs alone do not solve
   recovery across calls or restarts.
3. **High — risk checks are not enforced at submission.** `submit_order()` does
   not call `RiskManager`; the halt is in memory. Concurrent submissions need
   reserved cash/exposure and a persisted halt checked at the execution boundary.
4. **Medium — correlation identity.** Weather correlation ignores `date_str`;
   NBA correlation relies on ticker prefixes. Use explicit event/city/date metadata.
5. **Medium — model evidence.** Historical totals/props training uses placeholder
   context features. `TemporalSplitter.split()` directly returns holdout arrays,
   bypassing its separate access gate. Review temporal feature availability and
   all training/calibration paths before allowing evaluator-driven changes.
6. **Medium — reproducibility.** Default Python 3.14 lacks pytest; an existing
   Anaconda environment runs the suite. Dependencies include old binary pins and
   an unbounded dotenv dependency. Add a documented supported Python environment
   and CI, then review dependency updates separately.

## Paper-first scope correction (user direction, 2026-09-04)

Paper trading must precede any real-money activity. Real-money execution is outside
current authorization and requires a later explicit user decision. The existing
`environment: demo` setting is not a complete paper simulator or a production
execution guard; do not treat it as either.

Next milestone supersedes the ordering below:

1. Build a local paper broker with virtual cash, persisted simulated orders/fills,
   fees, settlement, and a separate paper ledger. Inject a read-only market-data
   client; paper execution must never call exchange order/cancel endpoints.
2. Add Washington-aware market selection. Exclude NBA/sports from the initial
   operational universe; use weather as a candidate only after checking current
   account/platform availability. Unknown eligibility means skip, not assume.
   Retain historical NBA code for offline research.
3. Test realistic fills (spread, depth, partial fills, stale quotes), restart
   recovery, risk limits, and end-to-end paper P&L. A logged dry-run order is not
   evidence that an executable fill occurred.
4. Run and evaluate the paper workflow before any exchange-write integration.
   Demo integration is optional and separate; production remains out of scope.

Washington research checked September 4, 2026: reports describe restrictions on
sports, elections/politics, entertainment/culture, technology/science, and mentions.
The state AG reports geofencing deadlines of August 19 and September 2. This is
not an exhaustive certification of permitted markets or account eligibility.

Sources:
- https://www.atg.wa.gov/news/news-releases/judge-orders-kalshi-cease-numerous-washington-operations
- https://www.theblock.co/news/regulation/2026-08-13/washington-court-orders-kalshi-411785

## Prioritized future work

### 1. Execution reliability (next, before Phase 6)

- [x] Implement transport/configuration/sizing safeguards above with regressions.
- [ ] Migrate exchange write contracts and URLs; fixture tests for all four
  YES/NO buy/sell mappings, fixed-point quantities, create/cancel responses.
- [ ] Add durable order intents and unique client IDs before submission; represent
  unknown acceptance explicitly and reconcile before any resubmission.
- [ ] Add fill table with unique exchange fill IDs, quantity/price/fees; paginate
  fill and settlement history and reconcile partial fills and cancellation.
- [ ] Enforce persisted halt and reserved exposure within submission orchestration.

Acceptance: simulate an accepted order with a lost response, restart, duplicate
fills, partial fill then cancellation, and multi-page histories; prove one logical
order, correct inventory/P&L, and no new orders while halted.

### 2. Runnable demo workflow and validation

- [ ] Add a unified CLI with explicit dry-run/demo modes, scan and reconciliation
  commands, graceful shutdown, and structured logs.
- [ ] Resolve correlation metadata and historical feature availability.
- [ ] Enforce a real holdout boundary and record split/training provenance.
- [ ] Add README setup instructions and CI on the supported Python version.

Acceptance: deterministic offline scan-to-settlement test, then an explicitly
requested demo smoke run; daily report agrees with reconciled ledger totals.

### 3. Evaluator (resume existing Phase 6)

- [ ] Start with observation-only snapshots and schema-validated proposals.
- [ ] Add sample-size/cooldown/budget enforcement and a review queue.
- [ ] Add bounded application and rollback only after execution and validation
  acceptance criteria above pass.

## Sources checked

- https://docs.kalshi.com/api-reference/orders/create-order-v2
- https://docs.kalshi.com/getting_started/quick_start_create_order
- https://docs.kalshi.com/getting_started/pagination

## Validation

Full offline suite: 490 passed, 13 integration tests deselected (73.55 seconds).
Command: `/opt/anaconda3/bin/python -m pytest tests -q -m "not integration"`.
Targeted regression/client/sizing run: 57 passed, 6 integration tests deselected.
`git diff --check` passed.
All checks use mocked exchange interactions. No bot process, exchange order,
live model retraining, or production configuration change was initiated.

## Paper implementation update — 2026-09-04

Implemented a standalone paper broker, event journal, report, CLI, strict weather
baseline, and one-pass public-data observer. See README.md for runnable examples
and explicit limitations. The historical exchange module findings above remain
open for any future exchange integration; the paper path does not use them.

Paper milestone validation: 60 focused paper tests passed; full offline suite
550 passed, 13 live tests excluded, in 58.28 seconds. `git diff --check` and
standard-library paper CLI smoke checks passed.

No actual market watchlist was verified or exercised, no process was scheduled, and no exchange
orders were placed. The only P&L observed here is a synthetic accounting fixture.
