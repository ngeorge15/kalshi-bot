"""Kalshi's price-dependent trading fee formula, as a pure function.

Kalshi does not charge a flat per-contract fee: the fee scales with the trade
price. Per the public series schema (``fee_type``, ``fee_multiplier`` on
``GET /trade-api/v2/series/{ticker}``, documented at
https://docs.kalshi.com/api-reference/market/get-series, accessed 2026-09-16)
and the fee schedule it references
(https://kalshi.com/docs/kalshi-fee-schedule.pdf), the taker fee is:

    fee_dollars = ceil_to_cent(fee_multiplier * 0.07 * C * P * (1 - P))

where ``C`` is the contract count in the fill and ``P`` is the price in
dollars (0 < P < 1); the result rounds UP to the next whole cent and is
charged at trade execution, never at settlement. Series that also carry a
maker fee (``fee_type`` ``quadratic_with_maker_fees`` /
``quadratic_with_combo_maker_fees``) discount the maker side to one quarter
of the taker rate (coefficient 0.0175 instead of 0.07). Plain ``quadratic``
series -- every weather series, confirmed live via
``GET https://api.elections.kalshi.com/trade-api/v2/series/KXHIGHNY`` on
2026-09-16 (``fee_type: "quadratic", fee_multiplier: 1``) -- have no maker
discount at all: every fill, resting or not, pays the taker rate.

Kalshi's own fee-schedule PDF returned HTTP 429 on every fetch attempt made
while writing this module (WebFetch and curl, several user agents, several
retries). The formula above is corroborated by three independent secondary
sources reproducing it (including the widely-cited worked example of a
$1.75 fee on 100 contracts at 50c, which this module reproduces exactly) plus
the official field descriptions from docs.kalshi.com. See
research/kalshi-fees.md for full source quotes, access dates, and what
remains unverified.
"""
from decimal import Decimal, ROUND_CEILING

TAKER_COEFFICIENT = Decimal("7")     # 0.07, expressed as numerator over the /10000 below
MAKER_COEFFICIENT = Decimal("1.75")  # 0.0175 -- one quarter of the taker rate

_MAKER_FEE_TYPES = frozenset({"quadratic_with_maker_fees", "quadratic_with_combo_maker_fees"})
KNOWN_FEE_TYPES = frozenset({"quadratic"}) | _MAKER_FEE_TYPES


def trading_fee_cents(price_cents: int, count: int, *, fee_type: str, fee_multiplier: float,
                       is_maker: bool) -> int:
    """Kalshi's quadratic trading fee, in integer cents, for one fill.

    Args:
        price_cents: Fill price, an integer in [1, 99].
        count: Number of contracts in the fill, a positive integer.
        fee_type: The series' ``fee_type`` as returned by the Kalshi API
            (``quadratic``, ``quadratic_with_maker_fees``, or
            ``quadratic_with_combo_maker_fees``; ``flat`` exists in the
            public enum but its schedule was not found in any source and is
            unsupported here).
        fee_multiplier: The series' ``fee_multiplier`` (a positive float).
        is_maker: Whether this fill is the maker side. Ignored for plain
            ``quadratic`` series, which have no maker discount.

    Returns:
        The fee in whole cents, rounded up.

    Raises:
        ValueError: For an out-of-range price/count, an unknown fee_type, or
            a non-finite/non-positive fee_multiplier.
    """
    if type(count) is not int or count < 1:
        raise ValueError("count must be a positive integer")
    if type(price_cents) is not int or not 1 <= price_cents <= 99:
        raise ValueError("price_cents must be an integer in [1, 99]")
    if fee_type not in KNOWN_FEE_TYPES:
        raise ValueError(f"Unsupported fee_type: {fee_type!r}")
    if isinstance(fee_multiplier, bool) or not isinstance(fee_multiplier, (int, float)):
        raise ValueError("fee_multiplier must be a real number")
    multiplier = Decimal(str(fee_multiplier))
    if not multiplier.is_finite() or multiplier <= 0:
        raise ValueError("fee_multiplier must be finite and positive")
    coefficient = MAKER_COEFFICIENT if (is_maker and fee_type in _MAKER_FEE_TYPES) else TAKER_COEFFICIENT
    raw_cents = (coefficient * multiplier * count * price_cents * (100 - price_cents)) / Decimal(10000)
    return int(raw_cents.to_integral_value(rounding=ROUND_CEILING))
