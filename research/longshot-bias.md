# Favourite-longshot bias in Kalshi weather brackets

Analysis 2026-09-17 over 9,252 settled bracket markets across 1,580 city-day events
(2024-12-01 to 2025-12-31; the 2026 holdout is untouched). Quotes are the no-look-ahead
snapshot at local-standard day start minus one minute — the same instant the weather
backtest uses.

**Headline: the bias is real and large, but it sits inside the bid-ask spread. A taker
cannot capture it. A liquidity provider can.**

## The bias, at the midpoint

| Mid band | n | Mean mid | Settled YES | Difference |
| --- | --- | --- | --- | --- |
| 0-2c | 1094 | 0.011 | 0.003 | -0.008 |
| 2-5c | 2188 | 0.029 | 0.015 | -0.014 |
| 5-10c | 1156 | 0.070 | 0.049 | -0.021 |
| 10-20c | 1261 | 0.145 | 0.120 | -0.025 |
| 20-35c | 1599 | 0.274 | 0.252 | -0.022 |
| 35-50c | 1422 | 0.414 | 0.418 | +0.003 |
| 50-65c | 454 | 0.554 | 0.610 | +0.056 |
| 65-80c | 61 | 0.699 | 0.770 | +0.072 |

Longshots are overpriced, favourites underpriced — the classic pattern, in the direction
the literature predicts.

## Taking liquidity: no edge

Selling a cheap bracket means hitting the **bid**, not the mid. Net of Kalshi's taker fee
(computed once per 10-contract order, not per contract):

| Bid band | n | Mean bid | True P(yes) | Net per contract |
| --- | --- | --- | --- | --- |
| 1-2c | 1034 | 1.00 | 0.0116 | -0.26 |
| 2-3c | 669 | 2.00 | 0.0164 | +0.16 |
| 3-5c | 720 | 3.41 | 0.0306 | +0.06 |
| 5-8c | 603 | 5.93 | 0.0730 | -1.76 |
| 8-12c | 579 | 9.41 | 0.0829 | +0.52 |
| 12-20c | 915 | 15.26 | 0.1628 | -1.92 |
| 20-35c | 1618 | 27.05 | 0.2621 | -0.56 |

Mean spread is 3.8 cents (median 3.0) and the mispricing is 1-3 cents, so the spread eats
it. This is consistent with the weather backtest's negative result: a taker strategy pays
more to cross than the mispricing is worth.

## Providing liquidity: an edge appears

Selling the same bracket at the **ask** instead — i.e. being the resting offer that a buyer
crosses into — net of fees:

| Ask band | n | Mean ask | True P(yes) | Net per contract |
| --- | --- | --- | --- | --- |
| 2-4c | 1251 | 2.67 | 0.0088 | +1.49 |
| 4-6c | 1377 | 4.44 | 0.0145 | +2.69 |
| 6-10c | 945 | 7.37 | 0.0392 | +2.95 |
| 10-15c | 838 | 11.76 | 0.0704 | +3.91 |
| 15-25c | 1176 | 19.24 | 0.1420 | +3.94 |
| 25-40c | 1634 | 32.20 | 0.2797 | +2.63 |

It is not a single-city or single-season artifact (ask 2-25c subset, net per contract):

| Subset | n | Net |
| --- | --- | --- |
| All | 5587 | +2.89 |
| Volume >= 100 | 1876 | +2.42 |
| Volume >= 1000 | 259 | +1.17 |
| KNYC / KMDW / KMIA / KAUS | 1390 / 1419 / 1303 / 1475 | +2.03 / +4.28 / +2.90 / +2.25 |
| 2024-12..2025-06 | 2917 | +2.71 |
| 2025-07..2025-12 | 2670 | +3.08 |

The mirror side works too: buying favourites at the bid earns +2.68 per contract in the
40-60c band (n=1066).

The edge shrinks as volume rises (+2.89 overall, +1.17 on the most-traded markets), which is
what you would expect if the mispricing is retail flow and better-informed participants
compete it away where there is size.

## What this does NOT establish

- **Fills are assumed, not modelled.** Resting an offer does not mean it trades. You are
  filled when someone chooses to cross into you, and that choice is not random: the fills
  you get are enriched with informed buyers. The realistic return is below every number
  above, and the honest version of this test has to model queue position and adverse
  selection, or be run forward.
- **Tail risk is real.** Selling a 5c bracket wins 5 cents about 95% of the time and loses
  95 cents the rest. Sizing and inventory limits matter more than the average edge.
- **One quote per day.** Everything here uses the snapshot at local midnight. Intraday
  behaviour is unexamined.
- No result here is a live trading result.

## Next steps this points to

1. Simulate maker fills in the existing paper broker, which already models resting orders,
   depth, later-quote fills and fees — with deliberately pessimistic fill assumptions.
2. Run it forward on live quotes, where the fill question answers itself.
3. Revisit the intraday idea separately: the observed high so far is a hard floor, and the
   market may be slower to reprice it than the model.
