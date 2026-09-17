# Kalshi public market-data API — research notes (2026-09-16)

All calls below are unauthenticated GETs against `https://external-api.kalshi.com/trade-api/v2`
(docs.kalshi.com's quick-start confirms this is the current recommended base URL). The legacy
`https://api.elections.kalshi.com/trade-api/v2` host **also still answers identically** (tested:
`GET /series/KXHIGHNY` returns byte-identical JSON) but is not the documented one — treat it as an
unverified alias, not a replacement for `KALSHI_MARKET_BASE` in `src/paper/venue.py`.

> **Orchestrator verification (2026-09-16).** Settlement-source switch pinned for KXHIGHNY by
> reading `rules_primary` per event: NWS Climatological Report through **KXHIGHNY-26AUG13**;
> The Weather Company from **KXHIGHNY-26AUG16** (Aug 14–15 not checked; other cities not checked).
> New rules still name the station ("recorded at New York City (CLINYC) ... according to The
> Weather Company"). Consequences: backtests before the switch use NWS CLI truth; anything after
> needs an empirical check that TWC-settled results agree with CLI values before reusing a
> CLI-trained model. The `"finalized"` status bug is fixed in `src/paper/venue.py`
> (`KALSHI_SETTLED_STATUSES`); `"determined"` is intentionally not treated as settled.
>
> **Settlement-source agreement check (2026-09-17).** For all 30 KXHIGHNY events from
> 2026-08-16 to 2026-09-14 — every one settled by The Weather Company — the bracket that
> resolved YES contains that day's NWS CLI maximum. Agreement is 30/30, so a model fitted
> against CLI observations stays usable on Weather-Company-settled markets. Caveat: brackets
> are 2 degrees wide, so a sub-degree systematic difference between the two sources would not
> show up in this test, and only NYC was checked.

## TL;DR — biggest actionable findings

1. **`observe.py`'s settlement check is dead code against the live API.** A fully resolved market's
   `status` field value is **`"finalized"`**, never the literal string `"settled"` — that string
   only works as a query-filter value, not a payload value. `observe.py`'s
   `if market["status"] == "settled":` will never fire against production data. Detail in §2.
2. **Weather settlement source changed underneath the series** — recently-settled markets cite NWS
   CLI, markets open today cite "The Weather Company." Not a sampling artifact. Detail in §2.
3. **Full trade/candlestick history requires the `/historical/*` endpoints** once older than the
   rolling ~3-month live window; the regular endpoints empty out silently rather than erroring.
   Detail in §3.
4. Station-code identifiers embedded in `rules_primary` (CLINYC, CLIMDW, CLIMIA, CLIAUS) agree with
   `station_map.py`'s stations — **no station mismatch found** for the 4 mapped stations.

---

## 1. Weather series

`GET /series/{ticker}` and `GET /series?category=Climate%20and%20Weather` (200 unauthenticated).
Confirmed daily-high series for the four mapped stations, plus siblings:

| Series | City | Station in `rules_primary` | station_map.py match |
| --- | --- | --- | --- |
| `KXHIGHNY` | NYC | CLINYC (Central Park) | KNYC ✓ |
| `KXHIGHCHI` | Chicago | CLIMDW (Midway) | KMDW ✓ |
| `KXHIGHMIA` | Miami | CLIMIA (Miami Intl) | KMIA ✓ |
| `KXHIGHAUS` | Austin | CLIAUS (Bergstrom) | KAUS ✓ |
| `KXHIGHLAX`, `KXHIGHDEN`, `KXHIGHPHIL` | LA, Denver, Philadelphia | exist, same shape | not mapped |

Plus a large second generation of single-city series with a `T` inserted (`KXHIGHTDC`,
`KXHIGHTBOS`, `KXHIGHTATL`, `KXHIGHTPHX`, `KXHIGHTSEA`, `KXHIGHTSFO`, `KXHIGHTDAL`,
`KXHIGHTSATX`, `KXHIGHTLV`, `KXHIGHTOKC`, `KXHIGHTNOLA`, `KXHIGHTHOU`, …) plus at least three
differently-named Houston series (`KXHOUHIGH`, `KXHIGHHOU`, `KXHIGHOU`) coexisting — Kalshi has
renamed/duplicated this product family more than once. Full `/series?category=...` dump saved to
scratchpad (`kalshi-samples/`) is not included here for length; ~350 series matched "Climate and
Weather".

**Series-level fields** (`GET /series/KXHIGHNY`): `category: "Climate and Weather"`,
`frequency: "daily"`, `fee_type: "quadratic"`, `fee_multiplier: 1`, `contract_terms_url` →
`GLOBALTEMPERATURE.pdf` (shared across all `KXHIGH*` series — it's one generic contract-terms
family, not per-city).

**Event ticker format:** `{SERIES}-{YY}{MON}{DD}` e.g. `KXHIGHNY-26SEP16` (2-digit year, 3-letter
caps month, 2-digit day, no separators). `strike_date` in the event is the **UTC instant the local
day ends**, always at the station's *standard-time* offset regardless of current DST — e.g. for
Sep 16 2026 (EDT in effect), `KXHIGHNY-26SEP16.strike_date = 2026-09-17T05:00:00Z` = midnight
**EST**, not midnight EDT (which would be 04:00Z). Chicago/Austin same pattern: `06:00Z` = midnight
CST even in CDT season. This directly confirms the "local **standard** time" assumption baked into
`station_map.STATION_STANDARD_UTC_OFFSET_HOURS` and `weather.py`'s fixed-offset requirement.

**Brackets per event:** 6 markets per NYC/Chicago/Miami/Austin event on 2026-09-16: one open lower
tail (`strike_type:"less"`), four 2°F-wide "between" brackets, one open upper tail
(`strike_type:"greater"`). Example (`KXHIGHNY-26SEP16`):

| ticker | strike_type | floor_strike | cap_strike | sub_title |
| --- | --- | --- | --- | --- |
| `...-T75` | less | — | 75 | 74° or below |
| `...-B75.5` | between | 75 | 76 | 75° to 76° |
| `...-B77.5` | between | 77 | 78 | 77° to 78° |
| `...-B79.5` | between | 79 | 80 | 79° to 80° |
| `...-B81.5` | between | 81 | 82 | 81° to 82° |
| `...-T82` | greater | 82 | — | 83° or above |

Ticker suffix convention: `B{floor+0.5}` for a "between" bracket, `T{threshold}` for either open
tail (the number is the cap for "less", the floor for "greater"). Brackets are contiguous 2°F bins
with no gaps or overlaps (75-76, 77-78, … not 75-76, 76-77 — don't miscount).

**Continuous-bound mapping (confirms `weather.py`'s docstring example exactly):**
- `between floor=F cap=C` → continuous `[F-0.5, C+0.5)`. E.g. `B81.5` (81–82°) → `[80.5, 82.5)`.
- `less cap=C` (open lower tail) → continuous `(-inf, C-0.5)`. E.g. `T75` ("<75°" / "74° or below")
  → `(-inf, 74.5)`.
- `greater floor=F` (open upper tail) → continuous `[F+0.5, +inf)`. E.g. `T82` ("83° or above")
  → `[82.5, +inf)`.

**Open/close timing** (`KXHIGHNY-26SEP16-*`): `open_time: 2026-09-15T14:00:00Z` (≈10am ET the day
*before* the target date), `close_time: 2026-09-17T05:00:00Z` (midnight EST ending the target
local-standard day). So events open roughly 34h before the target day's local-standard midnight and
run through the full target day.

**Tick size / fees exposed by the API:** `price_ranges: [{"start":"0.0000","end":"1.0000",
"step":"0.0100"}]` on every market — standard 1¢ tick, matches `venue.py`'s
`MIN/MAX_PRICE_CENTS = 1/99`. `fee_type`/`fee_multiplier` are exposed per series (`quadratic`/`1`
for weather) but the actual per-contract cents formula is **not** returned by any endpoint hit
here. Third-party sources (Kalshi's published fee-schedule PDF, not independently re-verified by
this research) describe it as `fee = round(multiplier × P × (1-P), 2)` with a base multiplier
around 0.07 for most categories — unverified against the raw API, flagged as such.

## 2. Settlement rules — read this section carefully

**The settlement source is not static.** Two snapshots of the *same series*, different market age:

- **Active market opened 2026-09-15** (`KXHIGHNY-26SEP16-B81.5`): `settlement_sources: [{"name":
  "The Weather Company", "url": "https://weather.com/kalshi"}]`. `rules_primary`: *"If the maximum
  temperature recorded at New York City (CLINYC) for Sep 16, 2026, is between 81-82° fahrenheit
  **according to The Weather Company**, then the market resolves to Yes."* `rules_secondary`
  explicitly tells traders not to rely on AccuWeather/Google Weather and that "the official and
  final value ... is the maximum/minimum temperature as reported by the Weather Company," with a
  note that preliminary Weather Company data can have rounding/conversion issues and Kalshi has
  discretion to hold expiration for a data revision.
- **Settled market from 2026-07-16** (`KXHIGHNY-26JUL16-T89`, `status:"finalized"`, `result:"yes"`):
  `settlement_sources: [{"name": "NWS Climatological Report", "url":
  "https://forecast.weather.gov/product.php?site=OKX&product=CLI&issuedby=NYC"}]`. `rules_primary`:
  *"If the highest temperature recorded in Central Park, New York for July 16, 2026 **as reported
  by the National Weather Service's Climatological Report (Daily)**, is less than 89°, then the
  market resolves to Yes."* This is the literal NWS "CLI" product — the URL is the live
  forecast.weather.gov CLI product page for OKX/NYC. Every event ticker back through at least
  January 2026 that still has retrievable `settlement_sources` (via the event shell, see §3) shows
  `"NWS Climatological Report"`, same URL pattern (`product=CLI&issuedby={3-4 letter city code}`).

  So: **NWS Daily Climate Report ("CLI") is confirmed as the historical settlement source for
  KXHIGH* markets**, exactly as guessed — but Kalshi appears to have switched the live/current
  contract's settlement source to "The Weather Company" sometime between the Jul 16 and Sep 15
  2026 contract listings. This is a real product change to flag for anyone hard-coding "NWS CLI
  settles this," and it means paper-trading code that reads `rules_primary`/`rules_secondary` off a
  *live* market and compares it against an NWS forecast should not silently assume the settlement
  source matches the forecast source — they currently don't for new contracts.

- **Station identifiers.** `rules_primary` for Chicago/Miami/Austin/NYC uses "CLI" + station
  triplet: `CLINYC`, `CLIMDW`, `CLIMIA`, `CLIAUS` — these are literally the NWS AFOS/WMO product
  identifiers for the Daily Climate Report at each station, and they match `station_map.py`'s
  stations one-for-one (Central Park, Chicago **Midway**, Miami Intl, Austin Bergstrom). No mismatch
  found for the four mapped stations. (Whether "The Weather Company" sources its NYC number from
  the same Central Park instrument as the NWS CLI report is not verifiable from the API — the
  contract text no longer names a specific instrument, just "New York City.")

- **Local standard time confirmed.** See §1 — `strike_date`/`close_time` sit at midnight in the
  station's *standard*-time offset year-round, confirmed for NYC (EST, UTC-5) in September (DST
  month) and for Chicago/Austin (CST, UTC-6) likewise. This matches
  `STATION_STANDARD_UTC_OFFSET_HOURS` in `watchlist.py` exactly.

- **Integer-rounding → continuous bounds.** Confirmed exactly as `weather.py`'s docstring assumes:
  `floor_strike - 0.5` to `cap_strike + 0.5` for a "between" bracket; open tails get one bound at
  `threshold ± 0.5` and the other at infinity. See the worked table in §1.

- **Open tails / disputes.** `rules_secondary` (same on every market in an event) covers the "no
  data available" case: if the Exchange determines the initial non-preliminary publication has a
  material error, expiration is held "until the publication of a non-materially-erroneous data
  revision or until the Expiration Date, whichever comes first. If no data is available at the end
  of this period, **all markets will resolve to the last fair price determined by Kalshi**" — i.e.
  a discretionary cash-settlement fallback, not a NO/void outcome. `expiration_time` (typically
  `close_time + ~6 days`) is the hard backstop.

- **`status` values in practice (important code-level finding):** live/tradeable markets return
  `status: "active"`; a fully resolved market returns `status: "finalized"` with `result: "yes"` or
  `"no"` populated. The string `"settled"` **only appears as a query filter value** (`?status=
  settled`), never as a payload value — `/markets?status=finalized` returns 0 rows even though
  `/markets?status=settled` is what actually retrieves `finalized` markets. `observe.py`'s
  `if market["status"] == "settled":` therefore never matches live data; it should check for
  `"finalized"` (or whatever the real terminal value is) instead. No transient status was observed
  between close and finalization in this research (nothing closed recently enough to catch mid-
  transition), so it's unverified whether a literal `"settled"` (as opposed to `"finalized"`) value
  ever briefly appears — but it was never seen once.

## 3. Historical prices

- **`GET /markets/trades?ticker=...`** — works unauthenticated, no `series_ticker`-only filtering
  (passing `series_ticker` alone is silently ignored and returns an unfiltered firehose of all
  series' recent trades — `ticker` is the only effective per-market filter). Params seen accepted:
  `ticker`, `min_ts`, `max_ts`, `limit` (1–1000, default 100), `is_block_trade`; cursor-paginated,
  empty `cursor` means done.
- **`GET /series/{series}/markets/{ticker}/candlesticks?start_ts=&end_ts=&period_interval=`** —
  `period_interval` ∈ {1, 60, 1440} minutes. Returns `price`/`yes_bid`/`yes_ask` OHLC + `volume_fp`
  + `open_interest_fp` per bucket. Confirmed working for a market that closed 2 months ago.
- **A hard historical/live partition exists and is discoverable via `GET /historical/cutoff`**
  (unauthenticated, 200): returns `market_settled_ts`, `trades_created_ts`,
  `orders_updated_ts`, `market_positions_last_updated_ts` — at the time of this research, all four
  were `"2026-07-18T00:00:00Z"`. **Markets/candlesticks/trades/orders/positions dated before their
  cutoff are only available via the `/historical/...` mirror of each endpoint**
  (`GET /historical/markets`, `GET /historical/markets/{ticker}/candlesticks`,
  `GET /historical/trades`), same params, also unauthenticated. Events and series are **not**
  partitioned — `/events/{ticker}` and `/series/{ticker}` work for any date. Field names differ
  slightly on the historical side (e.g. `open_interest` not `open_interest_fp`, plain
  `"0.3300"`-style price fields without the `_dollars` suffix on historical candlesticks) — an
  adapter reusing `venue.py`'s parsing against `/historical/...` payloads needs to handle this.
- **Empirically confirmed retention window:** live `/markets?event_ticker=KXHIGHNY-26JUL10` (68
  days before "today", 2026-09-16) returns markets; `KXHIGHNY-26JUL09` (69 days) returns empty —
  consistent with the `2026-07-18` cutoff above. Kalshi's docs describe the live window as
  targeting "3 months," advancing forward continuously.
- **Retrieved samples:** ~2 months old, live endpoint: `KXHIGHNY-26JUL16-T89` (finalized,
  `result:"yes"`) — full `rules_primary`/`rules_secondary`, 100+ trades via `/markets/trades`,
  hourly candlesticks via `/series/.../candlesticks`, all with no auth. ~1 year old:
  `KXHIGHNY-25SEP16-B73.5` (finalized, `result:"yes"`, "73-74°") **not retrievable via `/markets`,
  `/markets/trades`, or `/series/.../candlesticks`** (all empty), but fully retrievable via
  `/historical/markets?event_ticker=KXHIGHNY-25SEP16`, `/historical/trades?ticker=...`, and
  `/historical/markets/{ticker}/candlesticks` — same NWS-CLI rules text, full trade tape, hourly
  candles.
- **Realistic depth per series:** effectively **unlimited** history is retrievable for `KXHIGH*`
  once you know to switch to `/historical/...` past ~2–3 months back — event shells (hence exact
  tickers, via `/historical/markets?event_ticker=`) go back at least a year with no sign of a hard
  stop in this sampling. The only practical friction is needing the exact market ticker, which for
  old dates you get from `/historical/markets?event_ticker=SERIES-YYMONDD`, not from the live
  `/markets` listing.

## 4. NBA

`KXNBAGAME` ("NBA Game", `category:"Sports"`, `exchange_index:3`, `fee_type:
"quadratic_with_maker_fees"`) is the moneyline-winner series; `KXNBATOTAL` covers game totals,
`KXNBASPREAD` covers spreads, plus dozens of prop/derivative series (`KXNBAPTS`, `KXNBA1H`, etc.).
Settlement source: `{"name": "the Governing League", "url": "https://www.nba.com"}` plus ESPN as a
secondary reference.

**Today (2026-09-16) is mid-way through the 2026 offseason** — the "2025-26 regular season" the
task asked about already finished (NBA Finals settled markets found: `KXNBAGAME-26JUN13NYKSAS-*`,
finalized, closed 2026-06-14). Currently-`open` `KXNBAGAME` events are **2026-27 season** games,
e.g. `KXNBAGAME-26OCT20BOSDET` (opened for trading 2026-08-20, ~2 months pre-game). Ticker format:
`KXNBAGAME-{YYMONDD}{AWAY}{HOME}-{TEAM}` (e.g. `KXNBAGAME-26OCT20BOSDET-DET` = "Detroit wins").

Since the 2025-26 season ended 2026-06-14 (Finals), well before the `2026-07-18` historical cutoff,
**every 2025-26 game is now in the historical partition**, and it is retrievable: fetched
`/historical/markets?series_ticker=KXNBAGAME&min_close_ts=...&max_close_ts=...` successfully (note:
the timestamp filters did not reliably narrow the result set in testing — treat them as
best-effort, and iterate by known event ticker/date instead if you need a specific game).
Retrieved a full Finals game (`KXNBAGAME-26JUN13NYKSAS-NYK`, opened 2026-06-09, occurrence
2026-06-14T03:30Z, i.e. **~5 days of pre-tip trading window**) and pulled 1-minute candlesticks via
`/historical/markets/{ticker}/candlesticks?period_interval=1` — dense, liquid data (volume in the
hundreds of thousands of contracts per minute near the buzzer in this sample).

**Estimate of usable pre-tip history:** the entire 2025-26 regular season (30 teams × 82 games ÷ 2
≈ 1,230 games) plus play-in (~4) and playoffs (~90) should have retrievable pre-tip price history
via `/historical/...`, based on this one confirmed sample and the fact that markets open days
ahead of tip-off with real volume. This is an extrapolation from a single verified game, not a
full-season crawl — flagged as an estimate, not a count.

## 5. Rate limits

`docs.kalshi.com`'s "Rate Limits and Tiers" page documents only **authenticated** tiers, starting
at **Basic (200 read tokens/s, 100 write tokens/s), which itself requires account signup** — there
is no published tier or numeric limit for fully anonymous/unauthenticated GETs. No
`X-RateLimit-*`/`Retry-After` headers were present on any unauthenticated response captured here
(checked via `curl -D -`). Practical implication: treat unauthenticated access as **undocumented
and unbounded-on-paper but presumably IP-throttled**; the small-request-volume / short-sleep
approach used in this research (≥1s between calls, well under even the lowest documented
authenticated tier) is the safe default until Kalshi publishes an anonymous-tier number.

## Sample files saved

`kalshi-samples/`: `kxhighny-26sep16-markets.json` (6-bracket live event, full rules text);
`kxhighny-26sep16-{b79.5,t82}-orderbook.json` (empty — no resting interest at sample time);
`kxnbagame-boston-orderbook.json` (populated two-sided book — confirms `orderbook_fp.{yes,no}_dollars`
matches `asks_from_orderbook()` in `src/paper/venue.py` exactly).
