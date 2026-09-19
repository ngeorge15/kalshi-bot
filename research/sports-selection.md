# Sports selection study — predeclared design

*Written 2026-09-19, **before any sports data is examined**. The design section is
not edited once results are appended below it.*

## The question

Can a long-term strategy of *choosing where to bet* — by sport, market type, price
band and side — turn a profit on Kalshi sports markets after fees and spread, even
though forecasting-based edges failed on weather and NBA?

## The central risk, stated first

Scanning many subgroups for one that looks profitable is how false edges are
manufactured. With hundreds of (sport × market type × price band × side × snapshot)
cells, dozens will look profitable by chance alone. The protocol below is built
around that risk rather than around finding a winner:

1. **Discovery and confirmation are separated in time.** Subgroups are selected on
   the earlier 60% of events (chronologically) and evaluated **once** on the later
   40%. Nothing is tuned on the confirmation set.
2. **Multiple-comparison control** on the discovery set: Benjamini–Hochberg at a
   10% false-discovery rate across *every* cell tested. The total number of cells
   is counted and reported, including the ones that failed.
3. **A permutation null control.** The identical pipeline is run on outcomes
   shuffled within event, many times. The number of "discoveries" it makes on
   noise is reported next to the number made on real data. If the real count is not
   clearly above the null count, there is no signal.

## Unit of observation

One binary market, priced at a fixed snapshot before close, settled by Kalshi's own
`result`. A "bet" is a YES or NO purchase at the **executable** ask on that side,
10 contracts, taker fee charged once per order per the series' own `fee_type` and
`fee_multiplier` (read from the API per series, not assumed; sports uses
`quadratic_with_maker_fees`, MLB at 0.5).

## Fixed choices (none may be adjusted after results are seen)

| Choice | Value |
|---|---|
| Snapshots | **24h before close** and **1h before close** — both always reported, neither chosen post hoc |
| Subgroup dimensions | sport · market type (game winner / spread / total / player prop / other) · price band · side |
| Price bands (YES ask) | 1–10c, 10–30c, 30–70c, 70–90c, 90–99c |
| Minimum cell size | 150 markets in discovery, or the cell is not tested |
| Cost | executable ask + fee once per order; no midpoint results anywhere |
| Split | first 60% of events by close time = discovery; last 40% = confirmation |
| FDR | Benjamini–Hochberg, q = 0.10, over all tested cells |
| Uncertainty | bootstrap clustered by event and calendar day (games on one day are correlated) |
| Universe | the ~30 highest-volume sports series with settled history, sampled evenly; sampling rule recorded |
| Holdout | the most recent 30 days are **excluded from both sets** and left untouched |

## Primary deliverable (reported first)

1. Number of cells tested, number selected on discovery, number confirmed.
2. The **null-permutation discovery count** beside the real one.
3. For the confirmed set only: net ROI after fees with a clustered 95% CI.

## The idea is dead if any of these holds

1. The real discovery count is not clearly above the permutation-null count.
2. Fewer than one selected cell is confirmed, or the confirmed set's net ROI CI
   includes zero or lies below it.
3. Confirmed cells all sit in one snapshot, one sport, or one week (a chance
   cluster, not a strategy).

## Detectability, computed up front

Even a *real* edge is slow to prove. For a binary contract at price p the per-bet
standard deviation is sqrt(p(1−p)) dollars, so ~50c contracts have ~50c of noise per
bet. Bets needed to detect an edge of e cents at 95% confidence (one-sided):
n ≈ (1.645 × 50 / e)².

| true edge | bets to detect |
|---|---|
| 1c | ~6,700 |
| 2c | ~1,700 |
| 3c | ~750 |
| 5c | ~270 |

A 1c edge is **larger than the entire net edge found on weather tails (0.58c)** and
would need roughly 6,700 independent bets — at one bet a day, eighteen years. Any
strategy here has to clear that bar with its *discovery* evidence alone, because live
trading cannot confirm it in a useful time.

## Prior, stated in advance

Sports is the sharpest reference on the exchange: enormous volume, sportsbooks
pricing the same events, and maker fees that cut against liquidity provision. My
prior is that this returns a null. The permutation control exists so that a null is
distinguishable from an underpowered test.

## Results

*Not yet run.*
