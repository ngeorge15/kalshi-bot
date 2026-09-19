# Where to look next: what a scan of all 14,169 Kalshi series shows

*Scanned 2026-09-19 from the public `/series` endpoint with `include_volume=true`.
Every number below was read from the API, not from documentation.*

## Why this scan

Seven routes are closed on weather and NBA (see `README.md` in this directory). Both
have a sharp reference price, either a government forecast or a mature betting
market, and the market already contains it. The useful question is where that is
*not* true.

## Verified facts

**Fee assumption for weather was right.** All 396 climate series are
`quadratic`, multiplier 1.0, so the fee model used in every weather result stands.

**Fees are not uniform across Kalshi.** Per-series `fee_type` / `fee_multiplier`:

| fee structure | series |
|---|---|
| quadratic, 1.0 | 13,974 |
| quadratic_with_maker_fees, 1.0 | 159 |
| quadratic, 0.5 (mostly MLB) | 18 |
| **quadratic, 0 (fee-free)** | **14** |
| combo-maker variants | 4 |

The 14 fee-free series are all long-dated (BTC/ETH year-end range, annual GDP,
"Trump out as president", Greenland, layoffs). They resolve roughly once, so the
sample per series is about one event. Fee-free but statistically untestable.
Sports carries maker fees, which cuts against the maker strategies here.

**Volume by category** (contracts, all-time): Sports 121B, Crypto 30B, Politics
2.4B, Elections 1.9B, Mentions 1.2B, Economics 1.2B, Entertainment 1.1B, Weather 1.1B.

## Candidate directions, ranked

**1. Mentions ("will X say word Y").** Best candidate. 445 series, $1.2B. No sharp
reference price exists, and resolution comes from public transcripts. Top series
(`KXTRUMPMENTION`) is liquid: 30 of 30 open markets two-sided, median spread 2c,
prices spread across 19-50c so there is real uncertainty rather than 1c tails. The
live endpoint shows 1,263 settled markets over 40 events since mid-July (base rate
43% YES); older history would come from the historical endpoint.
*Candidate model:* per-word base rate from past comparable speeches, against the
market price, using the same paired-Brier + event-clustered bootstrap machinery.
*Cost:* a transcript source (scraping, with terms-of-service questions) and a
word-matching rule that mirrors Kalshi's resolution rules exactly, which is where
this will go wrong first.
*Honest prior:* Kalshi mentions attract informed traders with their own transcript
databases, so the market may already price base rates. Untested, not assumed.

**2. Crypto hourly range ladders (`KXBTC`, `KXETH`).** Same partition structure as
weather but 120 brackets per event and 24 events a day, so statistical power is
enormous and the reference (spot plus short-horizon volatility) is free. A live
ladder shows 1-2c spreads with hundreds of contracts through the middle, and
neither side sums to 100c (asks 451c, bids 89c), so arbitrage is out as it was for
weather. The market makers are fast algorithmic firms and we cannot compete on
latency, so the only testable question is the same shape one that worked as a
*signal* on weather: does the ladder overprice its tails. On weather that effect
was real (PIT 0.0760 vs 1/12) but 3x smaller than the spread, and crypto tails sit
at the 1c minimum tick, so the same arithmetic probably applies.
*Prior:* likely the same negative, but cheap to replicate, so a good test of
whether the weather result generalises.

**3. Economics releases (CPI, jobs, GDP).** 798 series, $1.2B. There is a real
reference (consensus forecasts) but few independent events per year, so power is
poor. Low priority.

**4. Forward paper collection.** The recorder exists. A few weeks of live
order-book data is the only genuinely out-of-sample test set. Slow, nearly free,
and worth starting regardless.

## Not worth pursuing

- **Fee-free long-dated series**: one event per series, cannot be tested.
- **Sports**: sharpest reference on the exchange, plus maker fees.
- **Anything needing latency**: the recorder polls REST; the firms quoting these
  ladders do not.

## Recommendation

Test Mentions first, with a predeclaration written before any transcript is
downloaded, because it is the only category where the "efficient reference price"
objection that killed weather and NBA does not obviously apply. If it is also
efficient, that is the eighth negative and the argument that Kalshi is broadly
efficient at this scale is materially stronger for it.
