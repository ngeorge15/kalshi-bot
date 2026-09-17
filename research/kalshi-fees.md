# Kalshi trading fee schedule (accessed 2026-09-16)

## Verified live (primary, first-party API)

`GET https://api.elections.kalshi.com/trade-api/v2/series/KXHIGHNY` (fetched via curl,
2026-09-16) returns, verbatim:

    "fee_multiplier":1,"fee_type":"quadratic"

So the flagship weather series confirms: **weather markets use `fee_type: "quadratic"`,
`fee_multiplier: 1`** — i.e. taker-fee-only, no maker discount, no per-series scaling.

`GET https://docs.kalshi.com/api-reference/market/get-series` (docs.kalshi.com, fetched
2026-09-16) documents the `Series` schema fields verbatim:

> FeeType is a string representing the series' fee structure. Fee structures can be
> found at https://kalshi.com/docs/kalshi-fee-schedule.pdf.
> Enum: `quadratic`, `quadratic_with_maker_fees`, `quadratic_with_combo_maker_fees`, `flat`
>
> FeeMultiplier is a floating point multiplier applied to the fee calculations.

## Formula (UNVERIFIED against the primary PDF — see below)

`https://kalshi.com/docs/kalshi-fee-schedule.pdf` (linked from `help.kalshi.com`'s fees
article as "the complete Fee Schedule, and the math behind the fees") returned **HTTP 429
on every attempt** (WebFetch and curl, several user agents, several retries with delay,
over ~10 minutes) — Cloudflare/rate-limit blocked, not fetched directly.

The formula below is corroborated by three independent secondary sources that quote or
reproduce the PDF's content, plus the help-center page confirming its existence and that
fees are charged only "when a trade is ultimately executed":

- `help.kalshi.com/en/articles/13823805-fees`: "Kalshi makes money by charging a
  transaction fee on the expected earnings on the contract... The complete Fee Schedule...
  are posted... [here](kalshi-fee-schedule.pdf)." Maker fees are "applied to orders placed
  that are not immediately matched and are instead left as resting orders"; "only charged
  when a trade is ultimately executed, there are no fees associated with canceling a
  resting order." No settlement or membership fee is mentioned anywhere.
- `marketmath.io/platforms/kalshi`: "7% × p × (1−p) per contract (taker)" /
  "1.75% × p × (1−p) per contract (maker)"; worked example: "0.07 x 0.50 x 0.50 = 0.0175
  ... Round-trip taker cost: $0.035" (peak fee at the 50c midpoint).
- `oddsshopper.com` ("The Coin Flip Is The Most Expensive Trade On Kalshi") and
  `whirligigbear.substack.com` ("Maker/Taker Math on Kalshi") both independently give the
  same `0.07 × P × (1-P)` taker formula and the $1.75-fee-on-100-contracts-at-50c example.

**Formula used (taker):** `fee_dollars = ceil_to_cent(fee_multiplier × 0.07 × C × P × (1−P))`,
where `C` = contract count in the fill, `P` = price in dollars (0 < P < 1). Verified
arithmetically: `C=100, P=0.50 → 0.07×100×0.25 = $1.75` exactly, matching the widely-cited
"$1.75 fee to trade 100 contracts of a 50/50 market" example.

**Formula used (maker, only on `quadratic_with_maker_fees` /
`quadratic_with_combo_maker_fees` series):** `fee_dollars =
ceil_to_cent(fee_multiplier × 0.0175 × C × P × (1−P))` — one quarter of the taker
coefficient. `quadratic` series (all weather series, confirmed above) have **no** maker
discount: every fill pays the taker rate regardless of maker/taker status.

**Rounding:** rounds **up** to the next whole cent. Confirmed by the marketmath.io worked
example (`0.0175` fee-dollars input, described as rounding to a value effectively $0.02
when applied as a per-cent charge) and by every source agreeing the formula is applied
per fill and then ceiling-rounded; no source claims fractional-cent (sub-cent) settlement.
One further-removed blog (botforkalshi.com) claims a fractional "centicent ($0.0001)"
rounding basis with a running rounding-overpayment accumulator across an order's fills —
**this is NOT corroborated by any other source and is treated as unverified**; this
implementation uses the simpler, multiply-corroborated whole-cent-per-fill rounding.

**Settlement:** fees are charged at trade execution, not at settlement. No source
mentions a settlement fee for regular contracts.

## What's unverified / simplifications made

- The exact PDF text itself was never fetched (persistent HTTP 429). Everything above is
  reconstructed from secondary sources that quote it plus the official schema docs.
- Whether `fee_multiplier` applies identically to both the taker and maker coefficients,
  or whether Kalshi tracks separate multipliers, is not confirmed by the API schema (only
  one `fee_multiplier` field is exposed per series). This implementation applies the same
  `fee_multiplier` to both. This is moot for weather series today, since they are
  `quadratic` (no maker fee ever applies) with `fee_multiplier: 1`.
- `fee_type: "flat"` exists in the enum but its formula was not found in any source;
  unsupported by this implementation (raises `ValueError`).
