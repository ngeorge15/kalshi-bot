# The intraday floor — predeclared design

*Written 2026-09-18, **before any result was computed**. The discipline is the
one that saved `maker-simulation.md` from shipping a wrong conclusion: name the
falsification test and the primary deliverable in advance, so that no parameter
can be tuned after seeing a number. Results are appended below in a separate
section, and this section is not edited once they exist.*

## The idea

Every strategy this repo has tested so far needed to forecast the weather better
than the market, and none did (`weather-backtest-validation.md`: model Brier
0.1222 vs market 0.1036). This one needs no forecast at all.

Kalshi's daily-high markets partition the day's **maximum** temperature into
brackets. The maximum is a *running* quantity: it only ever goes up. So once the
temperature observed so far today has risen above a bracket's upper bound, that
bracket can no longer contain the day's high. Its YES contract is not
"unlikely" — it is worth exactly zero, by arithmetic, with no model involved.

The question is entirely empirical: **does the market zero those brackets out
immediately, or does a bid survive long enough and deep enough to be worth
taking?**

If a dead bracket still shows a YES bid of *b* cents, the executable trade is to
buy NO at (100 − *b*), which settles at 100. Gross profit is *b* cents per
contract on (100 − *b*) cents of capital, less the Kalshi taker fee
`ceil_to_cent(0.07 × C × P × (1−P))` charged once per order.

## Why this is not the maker trade that already failed

`maker-simulation.md` found that resting offers get filled precisely by the
brackets about to win — adverse selection, structural and unavoidable. This is
the mirror image: here we **take** liquidity, and we take it only on brackets
whose outcome is already determined by observation rather than prediction. There
is no adverse selection in buying something that has already happened. The risk
is not being wrong about the future; it is being wrong about the *present* —
which is a data-source question, not a forecasting one, and that is what the
falsification test below is aimed at.

## Data

- **Observations**: Iowa Environmental Mesonet ASOS hourly METAR (`report_type=3`),
  free, no auth. Timestamped at :53; a decision may only use an observation
  whose valid time is at least **10 minutes** in the past, since the report is
  transmitted after the hour it describes.
- **Prices**: Kalshi historical candlesticks via `src/data/kalshi_history.py`,
  using only candles whose interval **ends at or before** the decision instant.
- **Settlement truth**: the same source the existing backtest settles against,
  honouring `SETTLEMENT_SOURCE_SWITCH_DATE = 2026-08-13` (NWS CLI before,
  The Weather Company after).

## The hazard this design exists to measure

ASOS and Kalshi's settlement source are **not the same instrument**. A bracket
that ASOS says is dead could still settle YES if the settlement source reports a
different (lower) daily maximum, or if ASOS reports a spurious high. Each such
case is a ~97¢ loss against ~3¢ wins, so a handful of them would sink the
strategy exactly as 23 losses sank the maker trade.

This is therefore the **primary deliverable**, declared in advance:

> **Primary deliverable**: the count and rate of brackets that ASOS-observed
> data declared impossible but which nonetheless settled YES, broken out by
> safety margin. **Not** the headline P&L.

> **Falsification**: if the ASOS-dead / settled-YES rate is non-zero at a safety
> margin of 2°F, the premise is broken and the strategy is dead, whatever the
> P&L says.

## Predeclared parameters — fixed before the run

| Parameter | Value |
|---|---|
| Safety margin *M* (°F above the bracket's upper bound before it is called dead) | reported at 0, 1, 2, 3 — **2 is the headline**, chosen before the run as one ASOS rounding step plus one |
| Observation latency allowance | 10 minutes |
| Decision instants | every hourly observation within the local standard day |
| Price used | the YES bid from the last candle ending at or before the decision instant |
| Minimum bid to trade | 1¢ |
| Fee | `src/paper/fees.py`, quadratic, charged once per order |
| Contracts per order | 10, matching `DEFAULT_CONTRACTS_PER_TRADE` |
| Stations | the four mapped stations (KNYC, KMDW, KMIA, KAUS) |
| Period | training window only (through `TRAIN_END = 2025-12-31`); the 2026 holdout stays unspent |
| Uncertainty | event-clustered bootstrap, as elsewhere in this repo |

## Secondary measurements, also predeclared

1. **Availability**: how many dead-bracket opportunities exist per city-day at
   all, and what fraction show a bid ≥ 1¢. A correct premise with zero
   opportunities is still a dead end, and that is a perfectly acceptable answer.
2. **Staleness decay**: profit bucketed by minutes elapsed since the observation.
   If the whole edge lives in the first two minutes, it is a latency race this
   project cannot win and should not pretend to.
3. **Depth**: bid size at the decision instant. A 3¢ edge on 2 contracts is not
   a strategy.
4. **Bound check**: how often the final settled high was *below* the
   ASOS running max — the direction of disagreement that causes the losses.

## What a positive result would and would not mean

It would mean the market is slow to zero out arithmetically-dead brackets, and
that the slowness is exploitable after fees at realistic size. It would **not**
mean the project has a forecasting edge; it is a mechanical claim about
bookkeeping, not about weather. It would still have to survive forward paper
collection before any conclusion is drawn about live trading.

## Results

*Not yet run. This section will be appended, not substituted for the above.*

---

# Results

*Run 2026-09-19 over 2024-10-24 to 2025-12-31, four stations, 1,736 event-days.
The design section above has not been edited.*

## Verdict: FALSIFIED, as predeclared

| safety margin | brackets called dead | settled YES | rate |
|---|---|---|---|
| 0.0F | 4386 | 1 | 0.0002 |
| 1.0F | 3566 | 1 | 0.0003 |
| **2.0F (headline)** | **2803** | **1** | **0.0004** |
| 3.0F | 2067 | 1 | 0.0005 |

The predeclaration was explicit: *a non-zero ASOS-dead / settled-YES rate at a
safety margin of 2F breaks the premise and the strategy is dead, whatever the
P&L says.* The rate is non-zero. The strategy is dead.

## The single case, diagnosed

One event carries the entire verdict, so it is worth knowing what it was.

**KNYC, 2024-12-29, `KXHIGHNY-24DEC29-B59.5`, bounds [58.5, 60.5).** ASOS
running max at the 18:01Z decision instant: **68.0F** from 13 observations. The
market settled into that very bracket.

The day's ASOS sequence (local day starts 05:00Z):

```
05:51Z 53   09:51Z 51   13:51Z 52   17:51Z 68   <- last observation of the day
06:51Z 53   10:51Z 51   14:51Z 56
07:51Z 56   11:51Z 51   15:51Z 56
08:51Z 53   12:51Z 51   16:51Z 61
```

The official NWS CLI daily maximum for KNYC that day — the value the contract
settled against, and the value in this repo's own observed-max dataset — was
**60.0F**. So the settlement was correct and ASOS was the outlier, by 8F.

The reading has the signature of bad data rather than a genuine source
disagreement: a 7F jump in one hour, at the end of a day whose station record
stops abruptly at 17:51Z with no further observations. A station fault, with
the faulty value recorded just before the record ends.

**That diagnosis does not rescue the result.** Dropping the one case that
breaks a hypothesis, after seeing that it breaks it, is precisely the move the
predeclaration exists to prevent. What it does is define a *different*
hypothesis: the same strategy with an observation quality-control filter — for
example refusing a reading that jumps more than some threshold in an hour
unless a later observation confirms it. That is a new idea. It would need its
own predeclaration and its own evaluation, and it must not be tested on this
same data, where the one case it is designed to catch is already known.

## It dies a second time, independently

Even granting a perfect premise, the strategy is not a strategy.

| margin | opportunities / city-day | fraction still bid >=1c | trades in 1,736 event-days |
|---|---|---|---|
| 0.0F | 2.53 | 1.1% | 50 |
| 1.0F | 2.05 | 0.5% | 17 |
| **2.0F** | **1.62** | **0.2%** | **7** |
| 3.0F | 1.19 | 0.1% | 3 |

Dead brackets are common — about 1.6 per city-day at the headline margin, some
2,800 over the period. Bids on them are almost nonexistent: **99.8% of
arithmetically dead brackets have already been zeroed out by the time we could
act.** Seven trades in fourteen months across four cities is not a strategy
regardless of its hit rate, and the market's speed here is the reason.

For completeness, the P&L was positive — 7 trades, 100% hit rate, +250c at the
headline margin, concentrated in the first 15 minutes after the observation.
That number is reported because it was predeclared, not because it means
anything at n=7 under a falsified premise.

## The limitation that stands

Bid **depth** could not be computed. Kalshi candlesticks carry close price,
volume and open interest, never a bid size, so "is there enough size to matter"
is unanswerable retrospectively. No proxy was substituted. Top-of-book size is
available live through `src/live/recorder.py`, which is where that question
would have to be answered — and given the availability numbers above, it is not
worth answering.

## What this closes

This was the fifth route tested and the fifth to fail, and it failed in the
most useful way available: on a predeclared test, for a reason that was named
in advance as the thing most likely to kill it. The bound check found the same
single event from the other direction (1 of 1,736 event-days where the settled
high fell below the ASOS running max), which is a consistency check on the
measurement rather than an independent finding.

The honest summary is that the market zeroes out arithmetically dead brackets
essentially immediately, and the one time our observation feed disagreed with
settlement, our feed was wrong.
