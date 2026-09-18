# Maker simulation — does the ask-side edge survive a realistic fill rule?

Run 2026-09-17 over the same 2024-12-01 to 2025-12-31 window as
`research/longshot-bias.md` (2026 holdout untouched). **Result: no. The edge
disappears under a realistic fill rule, and the fills a resting order gets are
exactly the wrong ones.**

## Method

Post one sell-YES offer per city-day event (== buy NO) at T = local-standard day
start minus one minute (`weather_backtest`'s decision instant); no forecast is used
anywhere — this trades purely off the market's own bid/ask.

**Selection.** Among an event's usable-quote brackets, post on the single
**cheapest tradeable ask** (ties by ticker) — deliberately *not* the 10-25c band
`longshot-bias.md` already found best, to avoid tuning toward a known answer.

**Fill rule.** Walk the bracket's candles forward from T to close. A fill fires only
when a later candle's **bid high** reaches the offer: strictly above -> always a
fill; exactly equal (a touch) -> filled only for a fraction `queue_fill_fraction`
(default 0.5), chosen **deterministically by a seeded hash of the ticker** (not
per-run randomness), modeling queue position. No candle at or before T can fill.

**Candlestick fields.** `kalshi_history.fetch_candlesticks` normalizes every candle
to its **close** only, and the cache already built for this window (backtest /
longshot-bias work) holds nothing more. Real payloads carry full OHLC per side
(confirmed live on both `/historical/.../candlesticks` and
`/series/.../candlesticks`: `yes_bid: {open, high, low, close}`, `_dollars`-suffixed
live). The fill rule needs the bid's **high**, so `src/research/maker_sim.py`
fetches/normalizes candlesticks itself (`fetch_ohlc_candles`) instead of editing
`kalshi_history.py`, cached under its own namespace (`maker_sim_ohlc_candles`):
~1,580 fresh fetches (one per event, selected bracket only), 60-minute candles,
~1 req/s. No finer sample was needed for the checks below.

**Costs & guard.** Kalshi's quadratic fee, once per order at real contract count,
`is_maker=True` (re-confirmed weather's `fee_type` is plain `quadratic` — no maker
discount, so this is a no-op today, but the correct call regardless). 10 contracts,
1 offer/event. No forecast-archive guard applies; any date after 2025-12-31 is
hard-refused without a predeclared, verified `src.paper.protocol` naming this
experiment — none was declared, 2026 untouched.

## Results (offer=ask, contracts=10, queue_fill_fraction=0.5)

| Metric | Value |
| --- | --- |
| Events / offers posted | 1,580 |
| Fills / fill rate | 441 / **27.9%** |
| Hit rate (of fills) | 94.8% |
| Total P&L | **-8,214c (-$82.14)** |
| ROI on capital at risk | **-1.93%** |
| P&L/event 95% CI | (-39.3c, +1.1c) — straddles zero |
| Max drawdown / worst loss | -10,055c / -982c |

27.9% is not "fills nearly everything" (1,139 of 1,580 offers never traded).

## The deliverable: filled vs. unfilled settlement

| Group | n | YES-settlement rate | Hypothetical P&L/contract (no fees) |
| --- | --- | --- | --- |
| Filled | 441 | **5.2%** (CI 3.2-7.3%) | **-1.56c** |
| Unfilled | 1,139 | **0.0%** (CI 0.0-0.0%) | **+3.13c** |

This is the whole finding. Had every posted offer filled, it would have earned
+3.13c/contract — almost exactly `longshot-bias.md`'s ask-side edge (+2.89c). But of
the 23 brackets here that ultimately settled YES (the outcome a YES seller loses
on), **all 23 got filled**; of 1,139 that never crossed the offer, **zero** settled
YES. The market only trades through a cheap resting offer when the true probability
is rising toward the seller's losing outcome — hand-verified on
`KXHIGHAUS-24DEC25-T64` (offer 5c): bid sat at 0-4c most of the day, crossed 5c once
at 22c, then climbed to 99c by close.

## Sensitivity grid

Total P&L (cents) and ROI on capital at risk, full window, 10 contracts/offer:

| offer | queue_fill_fraction | Fill rate | Total P&L | ROI | P&L/event CI |
| --- | --- | --- | --- | --- | --- |
| ask | 0.00 | 23.7% | -10,123c | -2.81% | (-53.4, -5.0) |
| ask | 0.25 | 26.3% | -9,002c | -2.25% | (-45.6, -0.4) |
| ask | 0.50 (default) | 27.9% | -8,214c | -1.93% | (-39.3, +1.1) |
| ask | 0.75 | 30.5% | -7,120c | -1.53% | (-34.5, +2.7) |
| ask | 1.00 (upper bound) | 32.5% | -6,357c | -1.28% | (-31.5, +4.3) |
| ask-1 (undercut) | 0.50 | 45.8% | -6,772c | -0.96% | (-22.4, +2.9) |
| ask+1 (pad) | 0.50 | 21.1% | -8,090c | -2.55% | (-54.1, +1.4) |

**Every cell loses money.** `queue_fill_fraction=1.0` is a deliberately generous
upper bound (more optimistic than any real queue position) and still loses -1.28%.
Undercutting roughly halves the loss (more fills dilute the adverse selection
somewhat) but never flips the sign; padding it is worse, filtering out the small,
mostly-safe fills. `unfilled_yes_rate` is exactly 0.0% in every cell — not a
one-parameter artifact.

## What was checked

- **Fills aren't free**: 27.9% fill rate; most resting offers never trade.
- **Field is the bid, not an ask/trade price**: confirmed against raw payloads
  (`yes_bid.high[_dollars]`) before writing the normalizer, and spot-checked one
  real fill's candle history by hand.
- **Settlement scored on the right side**: hand-verified trade's
  `100*10 - 95*10 - 4 = -954c` matches the report exactly;
  `trading_fee_cents(95, 10, ...) == 4` independently confirmed.
- **Queue decision is deterministic, not a lucky seed**: reproduced the exact hash
  for one real `queue_touch` fill; result holds across the full 0.0-1.0 grid.
- **Not a single-band artifact**: loss holds in the 2-5c band (bulk of volume,
  -6,713c) and the 10-20c band (-1,718c); only the thin 5-10c band is positive
  (+606c, n=84).

## What this cannot establish

- Selection (cheapest ask, one/event) is a disclosed, non-tuned choice; a real
  maker quoting every bracket or sizing by depth would behave differently.
- `queue_fill_fraction` is a modeling parameter, not a measurement — candlesticks
  carry no order-book depth; the grid brackets a range, not the truth.
- 60-minute candles, not continuous time; a within-hour touch-and-reverse could be
  mis-classified either way, and no finer sample was pulled.
- One quote, one offer per event, no intraday order management; the
  filled-vs-unfilled bootstrap is not fully event-clustered once
  `max_offers_per_event > 1` (unused in the headline numbers above).
- No result here is a live trading result; 2026 remains untouched.

**Bottom line.** The maker edge in `research/longshot-bias.md` does not survive a
realistic fill rule: fills you get are, almost perfectly, the ones about to lose.
Every point in the sensitivity grid loses money net of fees.
