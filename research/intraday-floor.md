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
