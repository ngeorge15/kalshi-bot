"""Venue adapters: the only part of the research pipeline that is exchange-specific.

Everything downstream of collection is already venue-neutral -- `events`,
`uncertainty`, `protocol`, `health`, `marks` and `schedule` contain no exchange
references in code.  The coupling lives entirely in how a market is addressed,
how its status and settlement are read, and how its order book is expressed.
This module isolates those five things behind :class:`Venue` so a second venue
is an adapter rather than a rewrite.

**Price convention.**  Adapters must return integer-cent *asks* for both sides,
sorted ascending by price, in the shape the paper broker already expects:
``{"yes_asks": [[price_cents, quantity], ...], "no_asks": [...]}``.  Venues that
publish bids rather than asks must convert, as :class:`KalshiVenue` does -- a
YES ask is the complement of a NO bid.  Getting this backwards silently inverts
every edge calculation, so adapters carry the conversion rather than callers.

**No adapter is provided for a venue whose API has not been verified.**  Writing
one from memory would produce something that looks finished and fails on first
contact.  :class:`Venue` documents the contract; implement it against real
responses.
"""

import logging
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from typing import Protocol, runtime_checkable
from urllib.parse import quote

logger = logging.getLogger(__name__)

# Kalshi's public read-only host. Order entry lives on a different host that this
# research path deliberately never contacts.
KALSHI_MARKET_BASE = "https://external-api.kalshi.com/trade-api/v2"

# Contract prices are whole cents in [1, 99]; 0 and 100 are settled states, not
# quotable prices.
MIN_PRICE_CENTS = 1
MAX_PRICE_CENTS = 99

# A resolved Kalshi market reports status "finalized"; "settled" is accepted only
# as a query-filter value and from older payloads. "determined" is deliberately
# excluded: its result can still change before finalization.
KALSHI_SETTLED_STATUSES = frozenset({"finalized", "settled"})


@runtime_checkable
class Venue(Protocol):
    """The contract a prediction-market venue must satisfy to be collected from.

    Implementations must be pure translators: no trading, no authentication, no
    state.  Every method takes or returns plain data so adapters stay testable
    without network access.
    """

    name: str
    hosts: frozenset[str]

    def market_url(self, ticker: str) -> str:
        """Absolute URL for a single market's metadata."""
        ...

    def orderbook_url(self, ticker: str) -> str:
        """Absolute URL for a single market's order book."""
        ...

    def parse_market(self, payload: dict) -> dict:
        """Normalise a market payload.

        Returns:
            Dict with ``ticker``, ``status``, ``result`` (``'yes'``/``'no'``/
            ``None``), ``close_time`` (ISO 8601) and ``is_open`` (bool).
        """
        ...

    def parse_orderbook(self, payload: dict) -> dict:
        """Normalise an order book into integer-cent asks for both sides.

        Returns:
            ``{"yes_asks": [[price_cents, quantity], ...], "no_asks": [...]}``,
            each sorted ascending by price.
        """
        ...


class KalshiVenue:
    """Kalshi adapter over the public read-only market API.

    Kalshi publishes an ``orderbook_fp`` of fixed-point *bids* per side.  A YES
    ask is derived from the complementary NO bid, which is why
    :meth:`parse_orderbook` reads ``no_dollars`` to build ``yes_asks``.
    """

    name = "kalshi"
    hosts = frozenset({"external-api.kalshi.com"})

    def __init__(self, base: str = KALSHI_MARKET_BASE) -> None:
        self.base = base.rstrip("/")

    def market_url(self, ticker: str) -> str:
        """Absolute URL for one market."""
        return f"{self.base}/markets/{quote(ticker, safe='')}"

    def orderbook_url(self, ticker: str) -> str:
        """Absolute URL for one market's order book."""
        return self.market_url(ticker) + "/orderbook"

    def parse_market(self, payload: dict) -> dict:
        """Normalise Kalshi's ``{"market": {...}}`` envelope.

        Raises:
            KeyError: If the envelope or required fields are absent.
        """
        market = payload["market"]
        status = market["status"]
        result = market.get("result") or None
        return {
            "ticker": market["ticker"],
            "status": "settled" if status in KALSHI_SETTLED_STATUSES and result else status,
            "result": result,
            "close_time": market["close_time"],
            "is_open": status in {"active", "open"},
            "raw": market,
        }

    def parse_orderbook(self, payload: dict) -> dict:
        """Convert fixed-point complementary bids into integer-cent asks.

        Prices round up and quantities round down, so the simulated fill is never
        flattered by rounding.

        Raises:
            ValueError: If any level is malformed or out of range.
        """
        return asks_from_orderbook(payload)


def asks_from_orderbook(payload: dict) -> dict:
    """Complement opposite bids, rounding asks up and quantities down.

    Kept as a module-level function because it is the piece most likely to be
    reused or compared against when writing a second adapter.

    Args:
        payload: A Kalshi order book response containing ``orderbook_fp``.

    Returns:
        ``{"yes_asks": [...], "no_asks": [...]}`` with integer cents.

    Raises:
        ValueError: If a level is non-numeric, non-finite, or out of range.
    """
    book = payload["orderbook_fp"]
    result = {}
    for side, opposite in (("yes", "no"), ("no", "yes")):
        levels: dict[int, int] = {}
        for price, quantity in book[f"{opposite}_dollars"] or []:
            try:
                price, quantity = Decimal(str(price)), Decimal(str(quantity))
            except InvalidOperation as exc:
                raise ValueError("Invalid fixed-point book level") from exc
            if (not price.is_finite() or not quantity.is_finite()
                    or not 0 <= price <= 1 or quantity < 0):
                raise ValueError("Invalid fixed-point book level")
            cents = int(((1 - price) * 100).to_integral_value(rounding=ROUND_CEILING))
            count = int(quantity.to_integral_value(rounding=ROUND_FLOOR))
            if MIN_PRICE_CENTS <= cents <= MAX_PRICE_CENTS and count:
                levels[cents] = levels.get(cents, 0) + count
        result[f"{side}_asks"] = [list(pair) for pair in sorted(levels.items())]
    return result


# The venue used unless a caller supplies another.
DEFAULT_VENUE = KalshiVenue()
