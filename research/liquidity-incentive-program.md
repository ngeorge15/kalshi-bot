# Kalshi's Liquidity Incentive Program — mechanics, and why the "untraded cities" premise was wrong

*Researched 2026-09-18. Programme end date stated by Kalshi as 2027-01-01, subject
to modification — re-verify before relying on anything here.*

## Why this was investigated

Every strategy tested in this repo so far tries to **predict better than the
market**, and every one has failed on measured evidence (see
`weather-backtest-validation.md`, `maker-simulation.md`, `nba-market-comparison.md`).
A liquidity subsidy is structurally different: the exchange pays you for
*being present with a two-sided quote*, whether or not your quote is right.
That makes it the only candidate revenue source found so far that does not
require an informational edge.

It was investigated on the back of an observation that turned out to be a
measurement error. The correction is recorded below, because the error is
more instructive than the programme.

## CORRECTION — the observation that motivated this was wrong

An earlier scan of Kalshi's climate series concluded that several cities
(Denver, Los Angeles, Philadelphia) were "listed but completely untraded — no
volume, no two-sided quotes", and therefore free for the taking under a
liquidity subsidy.

That conclusion was drawn from markets fetched **between their `created_time`
and their `open_time`**. A Kalshi daily-weather market is created several
hours before it opens (e.g. `KXHIGHDEN-26SEP19` — created 09:34 UTC, opened
14:00 UTC). Before the open, the book is legitimately empty and volume is
legitimately zero. Reading that state as "nobody trades this city" is a
straightforward look-at-the-wrong-moment error, the same class of mistake as
a look-ahead leak, just pointing the other way.

Re-measured after open, across all 50 `KXHIGH*` series (2026-09-18):

| | |
|---|---|
| Series with open markets | 24 of 50 (the other 26, mostly international airports, list no open markets at all) |
| Markets per open series | 12 |
| Median bid/ask spread | **1¢** in 18 of 24 series |
| Most liquid | LAX — 325,508 contracts traded, median top-of-book size 195 |
| Denver | 52,220 volume, 1¢ spread, median size 6 |
| Philadelphia | 71,985 volume, 1¢ spread, median size 10 |

Denver, LAX and Philadelphia are not thin. LAX is the **most** liquid weather
series on the exchange. The premise was backwards.

## The programme's actual mechanics

From Kalshi's help centre (see Sources):

- **Snapshots** are taken once per second, at a random moment within the second.
- **Time Period Score** = your total snapshot scores ÷ all participants' total
  snapshot scores.
- **Reward** = Time Period Score × Time Period Reward × (non-excluded snapshots
  ÷ total snapshots), rounded down to the cent.
- A snapshot is **excluded** if the market is not open, **or if resting orders
  do not meet the Target Size on both the yes and the no side**.
- **Target Size**: more than 100 and fewer than 20,000 contracts resting on each
  side.
- Per-snapshot scoring finds a **Reference Price** (the first price level at
  which cumulative resting size reaches one-fifth of Target Size), then scores
  qualifying orders by size and by distance from it — orders at or better than
  the Reference Price score 1.0×, worse ones are discounted geometrically by
  tick distance.
- **Payout**: $1–$1,000 per market per day; $1.00 minimum; time periods of up to
  31 days, which may overlap.
- **Eligibility**: "most regular U.S. Kalshi members". Excluded: employees,
  affiliates, Introducing Brokers, FCMs and their customers, and non-U.S. users.
- Eligible markets: "all Kalshi markets are potentially eligible. Active reward
  periods are clearly marked on market pages."

## Assessment: not currently testable, and probably not attractive

Three findings, in order of how badly they hurt.

**1. Which markets actually pay is not in the public API.** The `/markets`
payload for a weather market carries no reward, incentive or programme field of
any kind (verified against `KXHIGHDEN-26SEP19-T80`). Kalshi says active reward
periods are "clearly marked on market pages" — i.e. in the web UI, not the data
feed. Without knowing which markets have a live reward period and what that
period's pool is, a simulation would be assumption stacked on assumption. There
is no number to compute.

**2. Target Size is far above the depth these markets carry.** Qualifying
requires **>100 contracts resting on each side**. Median top-of-book size is
5–30 contracts in most weather series. So the subsidy is not a matter of
adding a token quote to an empty book: it requires posting more than 100
contracts on each side, which is several times the depth anyone else is
showing, into a market already quoted 1¢ wide.

**3. That is exactly the exposure already measured as losing.** `maker-simulation.md`
measured what happens to resting offers in these markets: of 1,580 posted
offers, the 26.8% that filled included **100% of the brackets that eventually
settled YES**. Providing liquidity here loses 1.56¢ per contract to adverse
selection. The subsidy would have to out-earn that on more than 100 contracts
a side, and the subsidy is capped at $1,000 per market per day *shared across
all participants*, with the score share diluted by whoever else shows up.

**Conclusion.** The subsidy is real and the mechanics are knowable, but the
opportunity that appeared to exist was an artefact of reading pre-open books.
On the markets that actually exist, the programme asks you to take the exact
risk already measured as unprofitable, for a payment that cannot be sized from
public data. Shelved, not refuted: if Kalshi ever exposes reward periods in the
API, the cost side of this trade is already measured and the calculation would
take an afternoon.

## What this rules in

The one weather idea left untested does not need a subsidy, a forecast, or a
counterparty's mistake — see `intraday-floor.md`.

## Sources

- [Liquidity Incentive Program — Kalshi Help Center](https://help.kalshi.com/en/articles/13823851-liquidity-incentive-program)
- [Liquidity and Volume Incentive Programs: Where to Find Them](https://help.kalshi.com/en/articles/16076644-liquidity-and-volume-incentive-programs-where-to-find-them)
- [Incentive Programs — Kalshi](https://kalshi.com/incentives)
- [Liquidity Provider Program — Kalshi Help Center](https://help.kalshi.com/en/articles/15410219-liquidity-provider-program)
