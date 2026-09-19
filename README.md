# Kalshi paper research bot

Test whether forecasts beat market probabilities under explicit simulated execution
costs. **The current workflow is local paper trading.** Finding a credible paper
edge is a useful result even if this project never trades real money.

The paper package has no exchange order or cancellation methods and does not load
Kalshi credentials. It stores experiments in a separate database. Existing legacy
exchange/trading modules remain in `src/`; the paper CLI does not invoke them.

## Findings

Seven routes to a tradeable edge were tested against Kalshi weather and NBA
markets; none survives fees. The evidence, the controls that make the negatives
credible, and the mistakes corrected along the way are in
[research/README.md](research/README.md).

## Try the complete offline example

From this directory, using Python 3.10+ (the paper simulator uses the standard
library; the optional public-data collector also needs `requests`):

```sh
python3 -m src.paper --db data/paper/example.db init --config examples/paper_config.json
python3 -m src.paper --db data/paper/example.db replay examples/paper_events.jsonl
python3 -m src.paper --db data/paper/example.db report --output data/paper/example-report.json
```

This synthetic fixture submits 25 contracts, fills 8 across two later ask levels,
cancels the remainder, and settles. Expected simulated fees: 16 cents; simulated
profit: 419 cents. **These numbers test accounting, not an actual trading edge.**
Replaying the file again returns the same effects without duplicate fills or cash.

Each experiment persists its configuration. Changing capital, costs, model
uncertainty, or run kind requires a new database rather than silently rewriting
an experiment's assumptions. `init` rejects existing non-paper databases.

## Forward paper observations

The optional observer reads public market/orderbook data and NWS hourly forecasts
using GET requests. It submits only local simulated orders. It makes one pass and
exits; run it repeatedly to observe later fills and settlements. No background
process or scheduler is started automatically.

```sh
python3 -m src.paper --db data/paper/forward.db init --config examples/paper_forward_config.json
python3 -m src.paper --db data/paper/forward.db observe examples/paper_watchlist.json
python3 -m src.paper --db data/paper/forward.db report
```

**The supplied watchlist is intentionally unavailable and makes no data requests.**
Before using an actual market, replace its placeholder fields with the reviewed
ticker, settlement bounds, forecast coordinates, local standard UTC offset, and
an explicit availability check with its source and expiry. Public visibility
alone does not establish account eligibility. Unavailable or expired entries are
skipped; existing paper reservations for those entries are canceled. An access
error is reported, with no attempt to switch hosts or bypass it.

Automatic forward observations currently support temperature markets only. Manual
forward events also support precipitation. NBA models remain available for offline
research; a separate replay experiment can explicitly enable `games`, `props`,
`totals`, etc. Default experiments select temperature and precipitation only.
Washington availability research and sources are recorded in [AUDIT.md](AUDIT.md).

The collector stores raw market/book and forecast snapshots with events. It makes
one baseline prediction per market/model/version, continues collecting later books,
and settles previously observed markets only when the source reports `settled` and
a YES/NO outcome. A changed close time or reviewed contract specification stops that
market for investigation. Observation error results return a nonzero exit code.

The weather baseline is **uncalibrated**: a Normal distribution centered on the
maximum hourly temperature, with a fixed experiment-level sigma (default 5 F).
It requires all 24 hours of a future local-standard calendar day, correct units,
and a fresh issuance timestamp. No missing-temperature defaults are used. Explicit
continuous Fahrenheit bounds must match the contract's settlement/rounding rules;
the code does not infer those rules from a title. Same-day forecasts and observed
highs-so-far are not implemented.

### Authoring a watchlist

`scaffold`/`validate`/`explain` are offline authoring tools: they never make a
network call, never touch the experiment database, and work without `init`.

```sh
python3 -m src.paper watchlist-scaffold KNYC 2026-10-15 \
    --bracket=:69.5 --bracket=69.5:72.5 --bracket=72.5: \
    --output data/paper/nyc-watchlist.json
# edit data/paper/nyc-watchlist.json by hand, then:
python3 -m src.paper watchlist-validate data/paper/nyc-watchlist.json
python3 -m src.paper watchlist-explain data/paper/nyc-watchlist.json
python3 -m src.paper --db data/paper/forward.db observe data/paper/nyc-watchlist.json
```

`watchlist-scaffold` fills in everything it can derive (coordinates, an
`event_key`, the `weather_spec` skeleton) but never invents a ticker,
contract-rules URL, or availability review -- a human must replace those
placeholder fields before the entry is usable, and `watchlist-validate`
reports every one still outstanding. `--bracket` takes `LOW:HIGH` in
Fahrenheit; either side may be empty for an open tail, and a negative bound
needs the `--bracket=-5:0` form so it is not read as another flag.
`watchlist-validate` exits `0` when there are no errors (warnings are
allowed) and `2` otherwise; `watchlist-explain` prints a short plain-text
summary for a reviewer to eyeball, including bracket coverage gaps/overlaps.

Hand-authoring with `watchlist-scaffold` still works, but for the four mapped
weather stations (KNYC/KMDW/KMIA/KAUS) `watchlist-generate`
(`src/research/watchlist_gen.py`) mechanically builds a full watchlist from
Kalshi's own live market data instead: real tickers, bounds parsed from the
market payload (never guessed from ticker text), and each bracket's own
settlement-source reference, for every open bracket of one event:

```sh
python3 -m src.paper watchlist-generate --stations KNYC,KMDW,KMIA,KAUS --date 2026-10-15 \
    --output data/paper/nyc-watchlist.json
python3 -m src.paper watchlist-validate data/paper/nyc-watchlist.json
python3 -m src.paper watchlist-explain data/paper/nyc-watchlist.json
```

`watchlist-generate` is the one watchlist command that reaches the network
(a handful of unauthenticated `GET`s against Kalshi's public market API); the
other three stay fully offline. It never marks an entry available -- pass
`--available --eligibility-source "..."` only once a human has actually just
confirmed the market is tradeable, otherwise it refuses with exit code `2`.
Without `--available`, the generated file validates with zero errors and only
the same eligibility-window warning `scaffold` output would carry (the window
is already expired, forcing a human review before `observe` acts on it
outside `research_only` mode).

Public endpoints and response contracts checked during implementation:
[Kalshi market data](https://docs.kalshi.com/getting_started/quick_start_market_data),
[orderbook](https://docs.kalshi.com/api-reference/market/get-market-orderbook),
[market](https://docs.kalshi.com/api-reference/market/get-market),
[NWS API](https://www.weather.gov/documentation/services-web-api).
The automated tests mock these contracts; a real reviewed-watchlist smoke run is
still outstanding. If public book access requires authentication, this collector
reports the error rather than loading trading credentials.

## Input contract

`replay` reads one JSON object per line in chronological order. `event` reads one
JSON object from stdin, useful for connecting another forecast/data process:

```sh
python3 -m src.paper --db data/paper/forward.db event < path/to/event.json
```

Every event requires a globally unique `event_id`, `type`, and timezone-aware `at`.
Retries must send identical content with the same ID. Accepted/rejected/skipped
results are journaled; malformed events fail and roll back. File replay stops at
the failing line and reports its line number, preserving earlier committed events.
A corrected file can resume using the original IDs.

| Type | Additional required fields | Behavior |
| --- | --- | --- |
| `quote` | `ticker`, `market_type`, `event_key`, `close_at`, `observed_at`, boolean `available`; optional `yes_asks`/`no_asks` arrays of `[integer cents, whole contracts]` | Observe market; fill older orders against available ask depth. |
| `forecast` | `ticker`, `yes_probability`, `model_name`, string `model_version` | Record prediction, compare both sides after costs, Kelly-size a viable paper order or record why skipped. |
| `weather_forecast` | `ticker`, `snapshot` containing `source`, `issued_at`, hourly `periods` | Calculate and journal strict weather baseline; quote must include reviewed `weather_spec`. |
| `order` | `ticker`, `side` (`yes`/`no`), `limit_cents`, `quantity` | Reserve cash/exposure after risk checks. No immediate fill. |
| `cancel` | `order_id` | Release unfilled reservation; keep previously filled inventory. |
| `settlement` | `ticker`, `result` (`yes`/`no`) | After close, pay winning filled contracts, realize costs/P&L, expire remaining orders. |
| `halt` | None | Persist halt, cancel every outstanding reservation; continue accounting for owned contracts. |
| `unavailable` | `ticker` | Suspend market and cancel its outstanding reservations. |

`event_key` is explicit correlation metadata, e.g. `NYC-2026-09-05`; related
contracts share it. A quote requires exact integer cents and whole contracts.
The public-data adapter complements opposite bids into asks, rounds ask prices up,
and fractional quantities down. No event sends money to an exchange.

## Execution and evidence assumptions

- Buy-and-hold binary positions only. No selling/shorting or fractional contracts.
- Fills require a strictly later observed quote, executable asks within the limit,
  and remaining visible depth. Pending orders share depth in FIFO order.
- Consumed depth is persisted. An unchanged snapshot does not replenish it; visible
  quantity increases and newly observed levels can. This conservative approximation
  may underfill and cannot reconstruct exchange queue position or hidden liquidity.
- Default simulated cost is 2 cents per contract plus 1 cent adverse slippage
  (`fee_model: "flat"`). These are adjustable experiment assumptions, **not**
  Kalshi's current fee tariff. Set `fee_model: "kalshi"` (with `fee_type` and
  `fee_multiplier` from the series' public API record, e.g. `quadratic`/`1`
  for weather series) to charge Kalshi's real price-dependent quadratic fee
  instead; see `src/paper/fees.py` and `research/kalshi-fees.md`. Every paper
  fill consumes observed ask depth, which is a taker action, so the kalshi
  model always charges the taker rate.
- Cash and exposure include pending reservations and filled cost. Daily loss limits
  use realized settlement P&L; reported equity is at cost, not mark-to-market or
  liquidation value. There is no automatic halt reset.
- Synthetic, replay, and forward modes are permanently labeled. Forward checks
  arrival time; it cannot independently prove forecast provenance or lack of leakage.
- Paired Brier scores use the first eligible forecast per model/version/market,
  including forecasts that did not trade. A positive difference means lower model
  error on that sample, not statistical proof of an edge. Related markets are
  correlated. The report deliberately makes no profitability/significance claim.

## Tests and existing components

For the full project dependencies, use a Python environment compatible with
`requirements.txt` (Python 3.12 is a practical starting point):

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pytest tests -q -m 'not integration'
.venv/bin/python -m pytest tests/paper -q
```

Development validation used the existing `/opt/anaconda3/bin/python` environment;
no dependencies were changed. Tests mock external data. Live integration tests are
excluded from the commands above.

Existing modules cover NBA/weather ingestion and models, temporal validation,
legacy trading, and analytics. The new paper schema/report is intentionally
separate from the old `trades` schema, whose statuses and accounting do not fully
represent paper execution. See [AUDIT.md](AUDIT.md) and [ROADMAP.md](ROADMAP.md) for
remaining model, evaluation, and infrastructure work.
