# Findings: is there a tradeable edge in Kalshi weather and NBA contracts?

**Short answer: no, and the evidence is specific.** Six independent routes were
tested on Kalshi's daily-high-temperature brackets (four cities, ~1,500 settled
events, Oct 2024 - Dec 2025) and on NBA game markets. All six lose after fees. Each
was predeclared or controlled, and each failed for a different, documented reason.

The 2026 data was deliberately never touched. Nothing here is a claim about live
trading; it is a claim about what public forecasts can and cannot do against these
markets' own prices.

| # | Route | Result | Why it fails | Doc |
|---|---|---|---|---|
| 1 | Better point forecast (NBM + fitted normal) | Brier 0.1222 vs market 0.1036 | Market is genuinely skilled (0.194 vs 0.247 climatology) | [weather-backtest-validation](weather-backtest-validation.md) |
| 2 | Take the mispricing | ROI -12% | Bias is real but smaller than the ~3.4c spread | [weather-backtest-validation](weather-backtest-validation.md), [longshot-bias](longshot-bias.md) |
| 3 | Provide liquidity | -1.56c/contract | 100% of eventual winners fill vs 26.8% of losers (adverse selection) | [maker-simulation](maker-simulation.md) |
| 4 | Ladder arbitrage | 0 opportunities in 91 instants | Complete ladders sum to 105-110c; never within 5c of profitable | [market-opportunities](market-opportunities.md) |
| 5 | Six-model ensemble, sigma fitted to model disagreement | Closes 47% of the gap, still loses (CI wholly below zero) | The standard professional toolkit, correctly applied, is not enough | [multi-model-forecasting](multi-model-forecasting.md) |
| 6 | Intraday floor (bracket already below observed high) | Falsified; 7 trades in 14 months | Market zeroes 99.8% of dead brackets before you can act | [intraday-floor](intraday-floor.md) |
| 7 | Implied-distribution shape (PIT) | Shape mispricing is real; edge at mid 0.58c vs 1.88c spread | Efficient to within its own transaction costs | [implied-distribution](implied-distribution.md) |

NBA: model loses to the market (-0.0439 Brier) and to a plain ELO baseline
(-0.0287) - [nba-market-comparison](nba-market-comparison.md).

## What makes these negatives credible

A negative result is only worth something if the instrument could have found a
positive. Controls that were run:

- **Oracle control**: give the model the true answer and it earns +122% ROI, so the
  fee and settlement machinery is not what is losing.
- **Lead-time control**: a 2-day-old forecast scores worse than a 1-day-old one.
- **Null control**: the PIT instrument returns 0.0838 against a theoretical 1/12 on
  a market calibrated by construction.
- **Synthetic-effect control**: the ensemble machinery recovers a planted
  spread-skill relationship, so a null on real data is interpretable.
- **Predeclaration**: the falsification test and primary deliverable were committed
  to git before the run ([intraday-floor](intraday-floor.md) is the clearest case).

## Mistakes found and corrected along the way

Kept in the record rather than smoothed over:

- An API that looked like a forecast archive but stitches the first hours of each
  model run, which is a near-nowcast and leaks same-day information
  ([historical-forecasts](historical-forecasts.md), CORRECTION block).
- `status == "settled"` never fires: Kalshi returns `finalized`. Test fixtures had
  encoded the same wrong assumption, so every test passed.
- Fee rounded per contract instead of per order; corrected, conclusion unchanged.
- A cache write that was not atomic; its only symptom was mild slowness.
- Reading markets between `created_time` and `open_time` as "untraded"
  ([liquidity-incentive-program](liquidity-incentive-program.md), CORRECTION block).
- One accidental peek at test-period statistics, disclosed in the validation doc.

## What is not claimed

- No live-trading result. Everything is replay or paper.
- No claim that an edge cannot exist elsewhere (other contract categories,
  cross-venue, or with proprietary data).
- The one hypothesis left open is a quality-controlled intraday floor, which
  cannot be tested on the data that motivated it and would need fresh data plus
  its own predeclaration.
