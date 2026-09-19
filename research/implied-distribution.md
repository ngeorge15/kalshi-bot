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
