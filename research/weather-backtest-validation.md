# Weather backtest — validation period (2025-07-01 to 2025-12-31)

Run 2026-09-17. **Result: no tradeable edge. The model is worse than the Kalshi market at
pricing temperature brackets, and the trading rule loses roughly 13% of capital deployed.**
The 2026 held-out period was NOT used and remains untouched.

## Setup

- **Fit**: per-station bias and sigma of (observed − forecast), fitted on 2024-12-01 to
  2025-06-30 only. Fitted values: KNYC +1.15/2.59F, KMDW +1.72/2.85F, KMIA +2.04/1.51F,
  KAUS +0.94/3.32F (n ~200 days each).
- **Decision instant**: local-standard day start minus one minute. A 24h-lead forecast hour
  is used only if its run would have been published by then (2h latency allowance),
  otherwise the 48h-lead value. Market quote is the most recent candle ending at or before
  that instant.
- **Trading rule**: buy the single highest-EV side of one bracket per city-day when
  EV/100 >= min_edge, 10 contracts, Kalshi quadratic taker fees, settled on the real result.
- **Data**: 10,224 market-price rows, 97% with a usable quote at the decision instant;
  mean spread 3.4 cents. 732 city-day events, 4,392 bracket markets scored.

## Results

| Variant | min_edge | Trades | Hit rate | ROI on cost | P&L/event CI low (cents) |
| --- | --- | --- | --- | --- | --- |
| lead1_guarded | 0.02 | 721 | 0.42 | **-11.6%** | -85.8 |
| lead1_guarded | 0.05 | 679 | 0.42 | **-11.4%** | -84.7 |
| lead1_guarded | 0.10 | 556 | 0.41 | **-13.6%** | -97.3 |
| lead2 | 0.02 | 730 | 0.41 | **-12.6%** | -89.4 |
| lead2 | 0.05 | 706 | 0.41 | **-12.1%** | -86.7 |
| lead2 | 0.10 | 632 | 0.40 | **-12.9%** | -93.0 |

(Rerun 2026-09-17 after fixing a fee-rounding bug: fees had been rounded up per
contract rather than once per order, overcharging up to fivefold on deep-priced
trades. Correcting it trims the loss by 1-2 points of ROI and changes nothing
qualitative. Brier scores are unaffected — they are a probability metric, not a
cost one.)

Probability quality, independent of the trading rule (732 events, 4,392 markets):

| Variant | Model Brier | Market Brier | Improvement | 95% CI |
| --- | --- | --- | --- | --- |
| lead1_guarded | 0.1222 | 0.1036 | **-0.0186** | (-0.0214, -0.0159) |
| lead2 | 0.1307 | 0.1036 | **-0.0271** | (-0.0300, -0.0241) |

Every interval excludes zero. Raising the edge threshold does not help, which is what a
miscalibrated-sharpness problem looks like: the trades the model is most confident about are
not the ones it gets right. The model is reasonably calibrated in aggregate (predicted 0.041
-> observed 0.024; 0.152 -> 0.136; 0.246 -> 0.284) but less sharp than the market.

## Controls

- **Lead ordering (no-leak check)**: the 48h-lead variant scores worse than the guarded 24h
  variant (0.1307 vs 0.1222), the expected direction. A leak would typically invert this.
- **Oracle control**: substituting the observed daily high as the "forecast" (sigma 0.8F)
  produces 711 trades, a 99.6% hit rate, +122% ROI and Brier 0.0259 against the same market
  prices. The harness can detect an edge when one exists, so the negative result above is
  not a plumbing artifact.
- **Partition check**: 0 events where bracket probabilities failed to sum to 1.

## Why this is a believable negative

The market is doing something the model cannot: it prices a full day ahead using more than
one deterministic forecast, and it updates. A single NBM point forecast wrapped in a normal
distribution of fitted width is a weak competitor. Fees are not the cause — the model is
already behind on probabilities alone, before any cost is applied.

## Disclosure

While diagnosing a data-quality defect, the orchestrator viewed test-period (2026) forecast
error summary statistics — per-station and per-season bias and standard deviation — before
the held-out protocol was declared. No parameter was selected from them: bias and sigma are
computed by code from the train slice only, and the variant/threshold grid above was fixed
in advance. It is recorded here because "we did not use it" is a claim a reader should be
able to weigh for themselves.

## What was NOT done

The 2026 held-out period was not scored. A held-out set is for confirming a candidate that
already looks good, and nothing here qualifies. It stays unused for a future strategy.
