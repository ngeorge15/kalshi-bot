# The ladder as a distribution — and a sign conflict with the published literature

*Researched 2026-09-18. The two external figures below were fetched and read
directly, not taken from a summary.*

## The reframing

Every test in this repo so far has treated brackets **independently**: is this
contract mispriced? But a Kalshi daily-high event is a *partition* — 10-12
mutually exclusive brackets spanning the real line whose prices sum to ~100c.
The market is therefore quoting an entire **implied probability distribution**
over tomorrow's maximum temperature, and we have never once looked at it as one.

This matters because it is a different kind of bet. Beating the distribution's
**location** needs a better forecast, which has failed four times. Beating its
**shape** only needs the market's *uncertainty* to be miscalibrated — a much
weaker requirement, and a well-documented phenomenon elsewhere. A bracket is
structurally a butterfly spread; a full ladder is a discretised density. The
options literature on extracting risk-neutral densities
(Breeden-Litzenberger and successors) is the direct analogue.

## The external evidence, verified

Le, *Decomposing Crowd Wisdom: Domain-Specific Calibration Dynamics in
Prediction Markets*, arXiv:2602.19520 (Feb 2026), analyses Kalshi and Polymarket
by domain. Table 3, **Kalshi weather**, calibration slope by time to resolution:

| Horizon | Slope | Reading |
|---|---|---|
| 0-1h | **0.69** | strongly overconfident |
| 1-3h | 0.84 | overconfident |
| 3-6h | 0.74 | overconfident |
| 6-12h | 0.87 | overconfident |
| 12-24h | 0.91 | mildly overconfident |
| 24-48h | 0.97 | ~calibrated |
| 2d-1w | 1.20 | underconfident |
| 1w-1mo | 1.20 | underconfident |
| 1mo+ | 1.37 | underconfident |

Verbatim: *"Weather markets exhibit the opposite pattern: overconfidence at short
horizons (slopes 0.69-0.97 within 48 hours), where prices are too extreme."*

A slope below 1 means prices are **too extreme**: a contract priced at 2c
settles YES *more* often than 2% of the time. Equivalently, the implied
distribution is **too narrow** — too little probability in the tails.

## The conflict with our own measurement

`longshot-bias.md` measured the same thing on our own four-city temperature data,
at the day-ahead decision instant (~12-36h to resolution, spanning Le's 12-24h
and 24-48h buckets, where he reports 0.91 and 0.97):

| Mid band | n | Mean mid | Settled YES | Difference |
|---|---|---|---|---|
| 0-2c | 1094 | 0.011 | 0.003 | **-0.008** |
| 2-5c | 2188 | 0.029 | 0.015 | **-0.014** |
| 5-10c | 1156 | 0.070 | 0.049 | -0.021 |
| 50-65c | 454 | 0.554 | 0.610 | +0.056 |
| 65-80c | 61 | 0.699 | 0.770 | +0.072 |

Cheap brackets settle YES **less** often than priced; expensive ones **more**.
Fitting a slope through the endpoints gives roughly **1.11** — prices compressed
toward 50%, i.e. **underconfident**, the implied distribution **too wide**.

**That is the opposite sign from Le at the same horizon.** Both cannot describe
the same population.

### Candidate reconciliations, none yet tested

1. **Different universes.** Le's "weather" domain covers all Kalshi weather
   contracts; ours is daily-high *temperature brackets* at four stations. Rain,
   snow and hurricane markets may dominate his sample and behave differently.
2. **A single slope hides band structure.** Our table is not a straight line: it
   is negative below 35c and positive above. A regression slope through a
   pattern like that reports one number for two opposite effects, and which
   number you get depends on where the price mass sits.
3. **Different periods and different base rates.** He reports a 24.0% weather
   base rate; a 12-bracket partition has a base rate near 1/12.
4. **Our own sample may simply be too small in the tails**, which is exactly
   where the disagreement is largest.

Resolving this is cheap — it uses only data already on disk — and it is worth
doing *before* any shape trade is designed, because the two readings imply
**opposite trades**.

## Why this also matters for the intraday floor

Le's strongest overconfidence is at **0-6 hours** to resolution (0.69-0.84) —
the intraday window where `intraday-floor.md` operates. If prices there really
are too extreme, then cheap contracts settle YES *more* often than their price
implies, and the intraday floor trade — buying NO on brackets observation has
declared dead, i.e. selling the cheap side — would be selling something
systematically underpriced.

That is a genuine caution, and it sharpens the predeclared primary deliverable
rather than changing it. The intraday floor conditions on an *observation* that
makes the bracket arithmetically impossible, which is information no
aggregate calibration statistic contains. But it raises the stakes on the
dead-but-settled-YES rate: if Le's effect is real at 0-1h and is not purely an
artefact of correct convergence, some of it should show up there.

## The fee structure decides the trade shape

Kalshi's taker fee is `ceil_to_cent(0.07 x C x P x (1-P))` (`kalshi-fees.md`),
which **peaks at 50c and vanishes in the tails**. This cuts directly against the
obvious shape trade: a symmetric condor sells the middle, where the fee is
largest and the market is most efficient, and buys the wings, where it is
smallest. The fee structure favours a **tail-only** structure instead.

So the first experiment should drop the condor framing entirely.

## Predeclared first experiment

*Written before running anything, per this repo's standard.*

**Compute**: for each settled event in the existing four-city dataset, build the
discretised implied CDF from the full ladder at the decision instant, once from
mid prices and once from the executable side (bid to sell, ask to buy). Compute
the randomised PIT value of the settled outcome per event. Histogram them.

**Primary deliverable**: the PIT histogram shape and its chi-squared statistic
against uniformity, at a **discounted effective sample size** — brackets within
a day and cities within a day are correlated, so the raw event count overstates
independence. Use the event-clustered bootstrap already in `uncertainty.py`
rather than a nominal N. U-shaped means too narrow; hump-shaped means too wide;
flat means no shape edge exists.

**Falsification — any one of these kills it:**
1. The PIT histogram is statistically uniform at the discounted N.
2. The sign flips between the mid-price PIT and the executable-price PIT. That
   would make it a spread artefact, the identical failure mode that closed the
   taking-side test in `longshot-bias.md`.
3. A tail-only trade's net P&L confidence interval includes zero after fees.
4. Our own PIT lands closer to uniform than either Le's finding or our own
   longshot table implies — meaning both prior readings were noise.

**Stated in advance**: the honest prior is that this resolves to no detectable
shape mispricing surviving costs, the same non-result as ladder arbitrage seen
from a different angle. It is worth running anyway because it is nearly free —
the data is already on disk — and because it resolves a documented conflict
between our own measurement and a published one, which is a publishable result
regardless of which way it falls.

## What was NOT verified

- The two papers most likely to answer the weather-specific question directly
  ("State price densities implied from weather derivatives"; "The Implied Market
  Price of Weather Risk") are paywalled and were not read.
- No source was found of anyone running a PIT, rank-histogram or CRPS analysis
  on Kalshi weather ladders, or executing a condor or strangle on them. Absence
  of evidence here is weak evidence of absence — such work would not be
  published if it worked.
- One practitioner claim of cross-market volatility mispricing is anecdotal and
  unbacktested. One source asserting Kalshi weather longshot bias runs backwards
  provides no data and discloses that its author's firm trades that position.

## Sources

- [Le, *Decomposing Crowd Wisdom: Domain-Specific Calibration Dynamics in Prediction Markets*, arXiv:2602.19520](https://arxiv.org/html/2602.19520v1) — Table 3 read directly
- [Cardozo & Rivero-Wildemauwe, *The Favorite-Longshot Bias in Prediction Markets: Evidence from Polymarket*, arXiv:2609.12878](https://arxiv.org/html/2609.12878)
- [Gneiting et al., calibration and sharpness / rank histograms](https://arxiv.org/pdf/1310.0236)
- [On the number of bins in a rank histogram, arXiv:2005.09018](https://arxiv.org/pdf/2005.09018)
- This repo: `research/longshot-bias.md`, `research/kalshi-fees.md`, `research/intraday-floor.md`

---

# Results

*Run 2026-09-19, 2024-10-24 to TRAIN_END 2025-12-31, four stations, **1,511
usable events** (8 skipped for missing prices, 217 for stale prices, 0 for an
incomplete ladder — the partition check never had to reject one). The 2026
holdout is untouched. The predeclared design above is unedited.*

## The shape mispricing is real

Primary deliverable — PIT histogram, mid prices, 10 bins:

```
[95, 123, 163, 166, 144, 147, 186, 175, 163, 149]     expected ~151/bin
```

Clustered E[(PIT − 0.5)²] = **0.0760**, 95% CI **(0.0724, 0.0795)**, against the
uniform reference 1/12 = 0.0833. The interval lies entirely below the reference:
**hump-shaped, the implied distribution is too wide.** The market puts more
probability in the tails than the weather actually delivers.

It survives every robustness check we set for it:

- **Executable prices agree**: CI (0.0684, 0.0753), same direction. This is the
  check that killed the taking-side result in `longshot-bias.md`, where a signal
  present at the midpoint reversed once you priced it at the executable side.
  Here it does not reverse.
- **Tail handling does not matter**: dropping the unbounded terminal brackets
  entirely (`truncate` mode, n=1307) gives the same verdict for both price
  fields. The conclusion is not an artefact of how open tails are treated,
  which was the ambiguity most likely to manufacture a result.

**Overround**, recorded rather than normalised away: ladders sum to a mean of
**107.75c** at mid (median 106.50c) and **120.51c** at executable prices (median
116.00c). The full partition costs 8-20% more than the 100c it pays.

## And it is three times too small to trade

Falsification check 3 killed it. The direction was read off the PIT — too wide
means **sell** the tails — and asserted in code against the diagnostic, so it
could not be chosen to make the P&L positive.

Selling the tails: 2,040 trades over 1,414 events, 90.3% hit rate, total
**−35,625c**, mean **−17.46c** per order, event-clustered 95% CI
**(−40.71, −10.17)c** per event. Entirely negative — a confident loss, not an
inconclusive one.

The decomposition is the whole story:

| | cents |
|---|---|
| mean **mid** of the traded tail brackets | 10.24 |
| **realised** settle rate of those brackets | 9.66 |
| the actual edge at mid | **0.58** |
| mean **bid** — what you receive selling | 8.36 |
| the spread you cross | **1.88** |

The market's midpoint really does overprice these tails, by 0.58c. The spread
is **3.2x** that. And the effect is cleanly monotone where it matters:

| sold at YES price | n | settled YES | mean mid |
|---|---|---|---|
| 0-2c | 658 | 1.22% | 2.66c |
| 2-4c | 536 | 2.24% | 3.93c |
| 4-6c | 200 | 3.50% | 6.32c |

Cheap tails settle YES at roughly half their quoted midpoint, consistently.
There is a genuine, measurable, sign-stable overpricing of improbable outcomes —
and it lives entirely inside the bid/ask spread.

## The sign conflict, resolved

`implied-distribution.md`'s open question was that Le (arXiv:2602.19520) reports
Kalshi weather as **too narrow** at 12-48h while our own `longshot-bias.md`
implied **too wide**. Measured directly on this run's own bracket data by the
identical band-slope method:

| source | calibration slope | distance from 1.0 |
|---|---|---|
| our `longshot-bias.md` prior | 1.11 | 0.11 |
| **this run** | **1.0503** | **0.0503** |
| Le, 24-48h | 0.97 | 0.03 |

Our new measurement sits **between** the two priors, on our own side of 1.0. So
the effect is real but smaller than our earlier table implied, and the
disagreement with Le is narrower than it looked — closer to a difference of
degree than of direction. The most likely explanation remains the one recorded
before the run: a single fitted slope summarising a relationship that changes
sign across the price range.

## Why this is the most informative of the six negatives

The first five routes failed because the signal was absent, or was an artefact,
or the opportunity did not exist. This one failed with the signal **present,
robust, and correctly signed** — confirmed at the midpoint, confirmed at
executable prices, confirmed with and without the open tails, and consistent
with a documented literature on variance risk premia.

It still loses, because a 0.58c edge cannot pay a 1.88c spread. That is not a
modelling failure or a measurement failure. It is the market being efficient to
within its own transaction costs, which is the strongest form of the same
conclusion every other route reached.

## What was not done

- No condor or symmetric structure was tested, deliberately: the quadratic fee
  peaks at 50c, so a structure that sells the middle pays the most fee exactly
  where the market is most efficient. Only tail-only trades were evaluated.
- A maker version — resting an offer rather than crossing the spread — was not
  tested here. `maker-simulation.md` already measured what happens to resting
  offers in these markets (adverse selection, −1.56c/contract), and a 0.58c edge
  does not survive that either, but it has not been measured directly for this
  specific structure.
