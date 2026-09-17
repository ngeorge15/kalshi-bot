# Market-family opportunity scan (non-temperature, non-NBA)

Method: enumerated all 14,129 live series via `GET /series` (elections.kalshi.com
trade-api/v2, 2026-09-16), then pulled `GET /markets?series_ticker=...&status=open`
for representative series to read live yes_bid/yes_ask, `volume_fp`, `open_interest_fp`,
`frequency`, `fee_type`, and `settlement_sources`. All calls were unauthenticated GETs.
No orders, no auth. WA tags reflect only the category list given in the brief
(sports, elections/politics, entertainment/culture, tech/science, mentions reported
restricted as of Sept 2026 per AUDIT.md) — full legal analysis is a separate agent's job.

## Ranked table

| Family | Edge source (hypothesis) | Liquidity (typical spread / vol) | Events/mo | Fee drag @ typical price | Data cost | WA tag | Effort | Score |
|---|---|---|---|---|---|---|---|---|
| Weekly initial jobless claims (`KXJOBLESSCLAIMS`) | DOL weekly release vs market's pre-release drift; market is thin far from consensus | Tight near consensus (1-2c spread, vol 1.8k-5.9k contracts/strike near ATM), near-zero away from it | ~4.3 | 7%·P·(1-P) ≈1.7c/contract at P=0.5 (plain `quadratic`, no maker fee) | Free (DOL press release, same-day) | Not in restricted list (unverified) | Low-medium | **Highest** |
| Weekly continuing claims (`KXCONTCLAIMS`) | Same as above, one week lagged, thinner | Wide/no quotes on most strikes (many 0 vol), a few strikes w/ 300+ vol | ~4.3 | Same formula, plain `quadratic` | Free (DOL) | Not restricted (unverified) | Low-medium | High |
| AAA weekly gas price bracket (`KXAAAGASW`) | AAA publishes daily; EIA weekly retail gasoline survey leads it; market is a slow random walk | Good: ATM strikes show 6k-13k contract volume, 1-5c spreads | ~4.3 | Plain `quadratic`, ~1.7c/contract at P=0.5 | Free (AAA site scrape or EIA data) | Not restricted (unverified) | Low | High |
| 30Y mortgage rate weekly (`KX30YMORTW`) | Freddie Mac PMMS / MBS-spread models can lead the weekly print | Moderate: 100-800 vol/strike, 1-10c spreads | ~4.3 | Plain `quadratic` | Free (FRED, MBA) | Not restricted (unverified) | Low-medium | Medium-high |
| CPI / core CPI monthly (`KXCPI`, `KXCPICORE`) | Cleveland Fed Inflation Nowcast, Truflation, gas/shelter components public before BLS print | Good on the money: 2.5k-64k vol on near-consensus strikes | ~1 (12/yr; distinct sub-buckets per release, not independent events) | `quadratic_with_maker_fees` — pros post liquidity here too | Free (Cleveland Fed nowcast, Truflation is paywalled tiered) | Not restricted (unverified) | Medium-high | Medium |
| Nonfarm payrolls monthly (`KXPAYROLLS`) | ADP private payrolls report 2 days early; various nowcasts | Thin: 250-700 vol/strike | ~1 (12/yr) | `quadratic_with_maker_fees` | ADP is free headline, paid for detail | Not restricted (unverified) | Medium-high | Medium |
| U.S. unemployment rate monthly (`KXU3`) | Documented poorest calibration in the one study found (Brier 0.13) — suggests genuine room, but also genuine difficulty | Thin: 50-900 vol/strike | ~1 (12/yr) | `quadratic_with_maker_fees` | Free (BLS, same household survey used for headline) | Not restricted (unverified) | High | Medium |
| Core PCE / headline PCE monthly (`KXPCECORE`,`KXPCEHEAD`) | Derivable almost exactly from CPI + PPI components 2-3 weeks ahead of BEA release | Thin-moderate: 50-1250 vol/strike | ~1 (12/yr) | Plain `quadratic` | Free (BLS CPI/PPI detail) | Not restricted (unverified) | High | Medium |
| Fed funds rate decision (`KXFEDDECISION`) | CME FedWatch (futures-implied) as an independent read on the same event | Best liquidity of the macro set on the front meeting (25k-77k vol) | ~0.67 (8 FOMC/yr) | `quadratic_with_maker_fees`; near-certain outcomes (P>0.9) still cost ~0.6c/contract | Free (CME FedWatch page; futures data itself is licensed) | Not restricted (unverified) | Low (spread compare) but slow to validate | Medium (as a monitor, not a strategy) |
| Quarterly GDP (`KXGDP`) | Atlanta Fed GDPNow tracks the BEA print closely all quarter | Thin-moderate: 250-11k vol on a few strikes | ~0.33 (4/yr) | `quadratic_with_maker_fees` | Free (GDPNow page + methodology) | Not restricted (unverified) | Medium | Low (frequency kills it) |
| S&P 500 daily/weekly close bracket (`KXINX`) | SPX options risk-neutral density (Breeden-Litzenberger) vs Kalshi bracket prices | Near-money strikes had **zero** live quotes/volume when sampled (pre-open); this varies heavily by time of day | ~20-22 (daily) | Plain `quadratic` | SPX chain is free (15-min delayed) or cheap real-time | Not restricted (unverified) | High (needs options math + live feed) | Medium — documented in the literature but likely thin edge after costs |
| Nasdaq-100 daily/weekly close bracket (`KXNASDAQ100`) | Same options-implied approach | Similar to S&P, thinner | ~20-22 | Plain `quadratic` | Same as above | Not restricted (unverified) | High | Low-medium |
| WTI / Brent / Gold / Silver daily brackets | Futures curve + realized-vol GBM as of settlement time | Moderate near ATM (250-1000 vol), thin in the wings | ~20-22 each | Plain `quadratic` | Futures quotes are free with a broker feed; delayed CME is free | Not restricted (unverified) | High (needs a futures/options feed) | Low-medium (professional MMs price these) |
| BTC/ETH hourly & daily brackets | Spot vol is well modeled (GBM/GARCH), 24/7 | Not sampled directly but Kalshi advertises this as a flagship high-volume product | ~700+ (hourly) | Plain `quadratic` | Free (Coinbase/Binance data) | Not restricted (unverified) | Medium | Low (crypto MMs are extremely fast and well-capitalized here) |
| TSA daily/weekly checkpoint throughput | TSA publishes the prior day's number on its own site every morning | No open markets found at scan time (`KXTSA`/`TSAW` returned 0 open) | Unknown (series exists, frequency daily/weekly) | Plain `quadratic` | Free (tsa.gov) | Not restricted (unverified) | Low | Unrankable (liquidity unknown) |

## Assessments

**Weekly jobless/continuing claims** are the strongest candidate for a genuine,
demonstrable edge with this project's tooling. The settlement source (Dept. of
Labor / NY Fed) is a single objective number, the release is free and same-day,
volume on near-consensus strikes is real (thousands of contracts), the fee is the
plain 7%·P·(1-P) formula (~1.7¢/contract at 50¢, less at the extremes), and at
~4.3 independent weekly events/month the project's 120-event min could be reached
in under two years of live collection — far faster than any monthly series. The
open question isn't settlement objectivity, it's whether a public nowcast (there
isn't one as clean as CPI's) actually leads the market; this needs its own
literature check, which this report did not have space to do exhaustively.

**AAA weekly gas price and the 30-year mortgage rate** are slow-moving, boringly
objective, well-suited to this repo's existing "public data plausibly leads the
market" pattern (structurally similar to the NWS-forecast weather thesis), and
both showed real order-book depth. The edge case is weak, though: AAA's own daily
average is nearly a random walk day-to-day, and any lead a solo dev can compute
from EIA/AAA public feeds is likely already priced in by the time the weekly
Kalshi contract opens, since the contract itself often opens after several days of
the AAA series are already known. Worth a cheap first experiment, not a bet on
alpha.

**CPI, payrolls, PCE, unemployment, and GDP** are exactly the family a serious
quant would target, and the literature found here (Krause SSRN 2026, "Makers or
Takers" 2026) shows real, measured favourite-longshot bias in this bucket, with
Fed funds calibration near-perfect (Brier 0.0001) but unemployment miscalibrated
(Brier 0.13) and longshots under $0.30 overpriced by ~7.2 points (p<0.001,
UNVERIFIED — single secondary source, not independently checked against the raw
paper). The catch is event frequency: monthly releases mean 120 independent
events takes ~10 years, and quarterly GDP ~30 years, which conflicts hard with
this project's event-clustered, min_events≈120 validation discipline. These are
markets to *watch and paper-trade opportunistically* (each release is a real,
scoreable event) rather than to build a statistically validated strategy around
in any reasonable timeframe. Kalshi also charges `quadratic_with_maker_fees` on
CPI/payrolls/unemployment/GDP/Fed-decision specifically (maker fee = 25% of
taker per secondary sources, UNVERIFIED against the primary PDF, which returned
HTTP 429 on fetch) — i.e., Kalshi's own fee schedule signals it expects
sophisticated market-making on exactly this bucket, consistent with the
near-perfect Fed-funds calibration finding.

**Fed decisions vs CME FedWatch** is a clean, low-modeling-effort comparison
(pull FedWatch's published probability, compare to Kalshi's `KXFEDDECISION`
yes price) but the literature (financefeeds.com, Sept 2026, UNVERIFIED) shows
gaps of 6-10 points between CME-futures-implied and prediction-market-implied
probabilities exist and are known, meaning any persistent gap is more likely a
structural risk-premium/liquidity-preference difference between instruments than
a free lunch, and it re-occurs only 8 times a year — too slow to validate
statistically inside this project's framework even if real.

**S&P 500 / Nasdaq-100 / commodity brackets against options-implied
distributions** is the single most literature-supported idea found (a named
Stevens Institute working paper applies Breeden-Litzenberger risk-neutral
densities specifically to Kalshi S&P 500 buckets and reports it as the most
effective method for flagging mispriced contracts — the paper's page itself
could not be fetched directly, HTTP 404, so treat this as UNVERIFIED beyond the
search-engine summary). It is also the most competed-over: SPX/Nasdaq options
are one of the most heavily arbitraged markets in the world, and when this scan
sampled `KXINX`/`KXNASDAQ100` near-the-money strikes they showed **zero** live
volume and quotes at the moment sampled (thin book outside market hours or a
particular refresh window) — liquidity needs re-checking at different times of
day before committing effort. This is real modeling work (options chain,
volatility surface) for a solo dev, and any edge that survives Kalshi's fee and
the bid-ask has to beat professional options market makers who already run this
exact calculation.

**BTC/ETH brackets** are Kalshi's highest-frequency, highest-volume non-sports
product (hourly cadence, thousands of events/month) but crypto spot/vol modeling
is a commodity skill with well-capitalized 24/7 market makers already present;
frequency is not the bottleneck here, competition is.

**TSA checkpoint throughput** is conceptually attractive (free, same-day
authoritative federal number, near-zero settlement ambiguity) but this scan found
**no open markets** on `KXTSA`/`TSAW` at all when queried — either the series is
dormant, seasonal, or was renamed; this needs a fresh `/events` check before any
further work, not investment based on this report alone.

## Literature and evidence (dated, with verification status)

All items below marked UNVERIFIED were read from search-engine summaries only;
the source page itself returned an HTTP error (403/404/429) on direct fetch, so
figures should be treated as approximate pending independent confirmation.

- **Bonini et al., "Watching the FedWatch," Journal of Futures Markets, 2026** —
  peer-reviewed futures- vs prediction-market-implied Fed probability comparison.
  UNVERIFIED (topic only). https://onlinelibrary.wiley.com/doi/10.1002/fut.70077
- **Krause, "Calibration and Forecast Accuracy of Macroeconomic Prediction
  Markets: Evidence from Kalshi, 2025-2026," SSRN, 2026** — Fed Funds Rate
  markets near-perfectly calibrated (Brier 0.0001) vs Unemployment Rate markets
  poorly calibrated (Brier 0.1302); longshot bias -0.0721 (p<0.001) for
  contracts under $0.30. UNVERIFIED (403 on fetch).
  https://papers.ssrn.com/sol3/papers.cfm?abstract_id=7117919
- **"Makers or Takers: The Economics of the Kalshi Prediction Market" (GWU /
  CEPR VoxEU), 2026** — 300,000+ contract transaction-level study; clear
  favourite-longshot bias (cheap contracts win less than breakeven after fees,
  expensive contracts modestly profitable), prices sharpen near close.
  UNVERIFIED beyond search summary. https://www2.gwu.edu/~forcpgm/2026-001.pdf,
  https://cepr.org/voxeu/columns/economics-kalshi-prediction-market
- **"Decomposing Crowd Wisdom: Domain-Specific Calibration Dynamics in
  Prediction Markets," arXiv 2602.19520, 2026** — 353M trades/429K contracts,
  Kalshi + Polymarket; political markets compress toward 50%, short-horizon
  weather markets bias the opposite way — bias direction is domain-specific.
  UNVERIFIED beyond abstract. https://arxiv.org/pdf/2602.19520
- **Kalshi Research, "Calibration in Prediction Markets: Theory and
  Evidence"** — Kalshi's own PDF, not independent, not fetched. UNVERIFIED.
  https://kalshi.com/research/kalshi-research-calibration.pdf
- **Stevens Institute, "Event Contract Mispricing via Options-Implied
  Probabilities"** — Breeden-Litzenberger risk-neutral density applied to
  Kalshi S&P 500 year-end buckets vs SPX options; RND reportedly beats a
  GBM/ATM-vol approach at flagging mispriced contracts. UNVERIFIED (404 on
  fetch; author/date/cost-survival unconfirmed).
  https://fsc.stevens.edu/event-contract-mispricing-via-options-implied-probabilities/
- **Kalshi fee formula** — converges across independent 2026 secondary sources
  (PredictReport, pm.wiki, botforkalshi, OddsShopper) on
  `fee = round_up_to_cent(0.07 * contracts * price * (1-price))` per contract
  for takers, maker fee = 25% of taker, peaking at 1.75¢/contract at 50¢.
  Primary source `kalshi.com/docs/kalshi-fee-schedule.pdf` returned HTTP 429
  (UNVERIFIED against primary doc). First-party and independently confirmed
  from the live `/series` API itself: `fee_type` is plain `quadratic` (no
  maker fee) for jobless/continuing claims, PCE, gas, mortgage, S&P, Nasdaq,
  WTI, gold, silver, BTC/ETH — but `quadratic_with_maker_fees` specifically for
  CPI, payrolls, unemployment, GDP, and Fed-decision series.
- Note: this project's paper broker (`src/paper/config.py`) currently models
  fees as a flat `fee_per_contract_cents = 2`, not the price-dependent quadratic
  formula above. README.md already flags this explicitly as "adjustable
  experiment assumptions, not Kalshi's current fee tariff" — worth keeping in
  mind since the quadratic formula matters most exactly at 50¢ (worst case,
  matches the flat assumption) and matters least at extreme prices (flat 2¢
  overstates cost on cheap/expensive contracts).

## Top 3 recommendations

1. **Weekly initial jobless claims (`KXJOBLESSCLAIMS`).** First experiment:
   collect the Kalshi bracket implied distribution each Wednesday (before
   Thursday's DOL release) alongside the most recent 4-week average and any
   free consensus estimate (e.g., Investing.com/Trading Economics calendar
   median, free), score both against the settled outcome with the existing
   paired-Brier / event-clustered harness, run for the ~2 years it takes to
   reach ~100 weekly events, no model training required for the first pass —
   just "does the naive 4-week-average-adjusted forecast beat the market."
2. **AAA weekly gas price bracket (`KXAAAGASW`) and 30Y mortgage rate
   (`KX30YMORTW`).** First experiment: same paired-Brier harness, forecast
   input = latest EIA weekly retail gasoline survey / Freddie Mac PMMS proxy
   extrapolated forward one week using the last 8 weeks' trend, compared
   against Kalshi's bracket. This is the closest structural analog to the
   existing temperature-baseline thesis (public number leads a slow-moving
   Kalshi weekly market) and reuses the same evaluation code with the least
   new modeling work.
3. **S&P 500 / Nasdaq-100 close brackets vs SPX/NDX options-implied
   distribution.** First experiment (higher effort, do only after 1-2 pay
   off or as a parallel track): pull a free 15-minute-delayed SPX/NDX option
   chain, build a Breeden-Litzenberger risk-neutral density (or a simpler
   ATM-vol GBM as a first pass), compare implied bracket probabilities to
   Kalshi's daily-close market, and specifically check *before* investing
   further whether near-the-money strikes actually have tradable depth at the
   times you'd want to trade (this scan found zero live quotes on the sampled
   snapshot). Treat this as a research/paper-only track given professional
   options market makers already run this exact calculation.

## Likely efficient / avoid list

- **BTC/ETH hourly and daily brackets** — highest event frequency on the
  exchange, but crypto spot vol is the most commoditized, most competed-over
  modeling problem in this whole list; 24/7 well-capitalized market makers.
  Avoid as a primary edge target; fine only as a liquidity-testing sandbox.
- **WTI/Brent/Gold/Silver daily-to-hourly brackets** — same logic as
  equities: liquid, continuously-quoted underlyings with active professional
  market-making layered on top; a solo dev's futures-curve model is unlikely
  to beat the book.
- **Fed rate decision vs CME FedWatch** — the literature-documented gap
  between futures-implied and prediction-market-implied probabilities looks
  more like a real, priced difference in what the two instruments measure
  (risk premia vs direct bets) than an exploitable dislocation, and only 8
  events/year make it nearly impossible to validate statistically here even if
  real. Fine as a standing monitor/dashboard, not as a strategy to backtest.
  UNVERIFIED beyond a single 2026 news aggregator citing a specific 6.5-point
  Polymarket/FedWatch gap.
- **Quarterly GDP** — public GDPNow tracking is a real and free advantage, but
  4 events/year means ~30 years to reach 120 independent events under this
  project's own validation convention. Structurally a poor fit regardless of
  edge size.
- **S&P 500/Nasdaq-100 brackets, taken at face value** — flagged as a top
  recommendation above for the research value, but restated here as a caution:
  this is the single most-arbitraged asset class on the exchange, and the one
  supporting paper for it could not be independently verified in this pass
  (404 on fetch). Do not treat "SPX options exist and are liquid" as itself
  sufficient evidence of an exploitable gap without first checking realized
  fill quality against the book at the times you'd actually trade.
- **CPI/payrolls/unemployment/PCE, as a validated strategy (not as a watch
  list)** — real documented favourite-longshot bias exists in this bucket per
  the literature above, but Kalshi's own fee-schedule signal
  (`quadratic_with_maker_fees` specifically on this bucket) plus monthly/
  quarterly frequency make it a poor first project: 10-30 years to reach this
  project's own min_events≈120 bar even before considering whether a solo
  dev's public-data edge (ADP, Cleveland Fed nowcast, GDPNow) survives after
  professional traders who already watch the same free numbers.
