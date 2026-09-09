"""Mark-to-market valuation for the paper broker's long-only positions.

Open positions are derived by replaying ``paper_fills`` (joined through
``paper_orders`` for side) against markets that have not yet settled
(``paper_markets.result IS NULL``). The broker is long-only and never records
a closing sale, so summing fill quantity and cost per (ticker, side) is a
complete position reconstruction; no separate "position" table exists or is
needed.

Valuation deliberately uses the conservative side of the book. This project's
synthetic order book (see ``src/paper/broker.py:_quote``) only ever records
*ask* depth for each side: the price at which a contract can be bought. There
is no native sell/bid book. Closing a long YES position means *selling* YES,
which this system approximates as buying the complementary NO contract at its
best ask -- at settlement the pair pays out ``MARKET_PAYOUT_CENTS`` regardless
of outcome, so the implied liquidation value of one YES contract is
``MARKET_PAYOUT_CENTS - best_no_ask_cents`` (and the mirror image for NO).
Using a position's own-side ask (the price to buy *more* of the same side, not
to close it) would overstate equity -- inflated valuation is exactly the kind
of flattery this project's ``report()`` evidence notes warn against, and
mark-to-market must not introduce it through the back door.

Mark-to-market is an estimate, not a settlement outcome. Thin depth, wide
complementary spreads, and stale quotes all make it unreliable. A position
whose quote is stale, missing, malformed, or lacks complementary-side depth to
price against is reported as unvaluable -- separately, with a reason -- and is
excluded from the portfolio total rather than silently valued at cost (which
would hide unrealized losses) or at its own ask (which would flatter it).
"""
from contextlib import contextmanager
from datetime import datetime
import json
import logging
from pathlib import Path
import sqlite3
from typing import Iterator

from src.paper.broker import utc

logger = logging.getLogger(__name__)

# A settled binary market pays this many cents, total, to whichever side of a
# yes/no pair wins. It is the constant used to derive an implied bid for one
# side from the best ask on the complementary side.
MARKET_PAYOUT_CENTS = 100

# The two contract sides this broker ever holds. Order matters only for
# readability; positions are grouped by (ticker, side) regardless.
SIDES = ("yes", "no")


@contextmanager
def _open_connection(conn_or_db: "sqlite3.Connection | str | Path") -> Iterator[sqlite3.Connection]:
    """Yield a row-returning connection, reusing one already open.

    Args:
        conn_or_db: An open ``sqlite3.Connection`` (reused as-is, row_factory
            forced to ``sqlite3.Row``, left open for the caller to manage) or
            a filesystem path to a paper database (opened, and closed on
            exit).

    Yields:
        A ``sqlite3.Connection`` with ``row_factory`` set to ``sqlite3.Row``.
    """
    if isinstance(conn_or_db, sqlite3.Connection):
        conn_or_db.row_factory = sqlite3.Row
        yield conn_or_db
        return
    conn = sqlite3.connect(str(conn_or_db))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _open_position_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Reconstruct open (ticker, side) positions from fills net of settlement.

    Args:
        conn: An open, row-returning connection.

    Returns:
        One row per (ticker, side) with nonzero filled quantity on a market
        that has not settled, carrying aggregate quantity and cost plus the
        market's current quote for valuation.
    """
    return conn.execute("""
        SELECT o.ticker AS ticker, o.side AS side, m.quote_json AS quote_json,
               m.quote_at AS quote_at, SUM(f.quantity) AS quantity,
               SUM(f.price_cents * f.quantity + f.fee_cents) AS cost_cents
        FROM paper_fills f
        JOIN paper_orders o USING(order_id)
        JOIN paper_markets m USING(ticker)
        WHERE m.result IS NULL
        GROUP BY o.ticker, o.side
        HAVING SUM(f.quantity) > 0
        ORDER BY o.ticker, o.side
    """).fetchall()


def _best_ask_cents(levels: list) -> int | None:
    """Return the lowest price with remaining depth, or None if none exists.

    Args:
        levels: A ``[[price_cents, count], ...]`` list as stored in
            ``paper_markets.quote_json["remaining_depth"][side]``.

    Returns:
        The lowest ask price with positive remaining count, or None if the
        side has no priceable depth left.
    """
    priced = [price for price, count in levels if count > 0]
    return min(priced) if priced else None


def _liquidation_price_cents(quote_json: str, quote_at: str, side: str, now: datetime,
                              max_quote_age_seconds: int) -> tuple[int | None, str | None]:
    """Derive the conservative per-contract liquidation price for one side.

    A long ``side`` position is priced off the complementary side's best ask,
    per this module's docstring, never off its own side's ask.

    Args:
        quote_json: The market's ``paper_markets.quote_json`` value.
        quote_at: The market's ``paper_markets.quote_at`` ISO 8601 UTC value.
        side: ``"yes"`` or ``"no"`` -- the side of the held position.
        now: The evaluation timestamp (timezone-aware UTC).
        max_quote_age_seconds: Quotes older than this are refused.

    Returns:
        A ``(price_cents, None)`` pair on success, or ``(None, reason)`` where
        ``reason`` names why the position could not be valued.
    """
    try:
        quote_time = utc(quote_at)
    except ValueError:
        return None, "invalid_quote_timestamp"
    age_seconds = (now - quote_time).total_seconds()
    if age_seconds < 0:
        return None, "quote_timestamp_in_future"
    if age_seconds > max_quote_age_seconds:
        return None, "stale_quote"
    try:
        depth = json.loads(quote_json)["remaining_depth"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return None, "malformed_quote"
    complementary_side = "no" if side == "yes" else "yes"
    complementary_ask = _best_ask_cents(depth.get(complementary_side, []))
    if complementary_ask is None:
        return None, "no_complementary_depth"
    return MARKET_PAYOUT_CENTS - complementary_ask, None


def mark_to_market(conn_or_db: "sqlite3.Connection | str | Path", now: datetime,
                    max_quote_age_seconds: int) -> dict:
    """Value every open position at its current, conservative quote price.

    Long YES is priced off the best NO ask and long NO off the best YES ask
    (see module docstring); this never values a position at the ask on its
    own side, which would overstate equity. This is an estimate of what
    closing the book right now might realize -- not proof of profit or loss,
    and unreliable when depth is thin or the quote is old.

    Args:
        conn_or_db: An open ``sqlite3.Connection`` to a paper database, or a
            path to one.
        now: The evaluation timestamp (timezone-aware UTC). Never sourced
            from the wall clock inside this function, so callers can test
            deterministically.
        max_quote_age_seconds: Quotes older than this cannot be trusted for
            valuation and make the position unvaluable rather than priced.

    Returns:
        A dict with:
            positions: list of valued positions, each with ticker, side,
                quantity, cost_cents, market_value_cents, and
                unrealized_pnl_cents.
            cost_cents, market_value_cents, unrealized_pnl_cents: portfolio
                totals over valued positions only.
            valued_count: number of positions included in the totals.
            unvaluable_positions: list of positions excluded from the totals,
                each with ticker, side, quantity, cost_cents, and reason.
            unvaluable_count: len(unvaluable_positions).

        On an empty book this returns zeros and empty lists; it never raises
        for an empty or fully-unvaluable portfolio.
    """
    with _open_connection(conn_or_db) as conn:
        rows = _open_position_rows(conn)
    positions = []
    unvaluable_positions = []
    total_cost_cents = 0
    total_market_value_cents = 0
    total_unrealized_pnl_cents = 0
    for row in rows:
        price_cents, reason = _liquidation_price_cents(
            row["quote_json"], row["quote_at"], row["side"], now, max_quote_age_seconds)
        cost_cents = row["cost_cents"]
        if price_cents is None:
            logger.warning("Position %s/%s unvaluable: %s", row["ticker"], row["side"], reason)
            unvaluable_positions.append({"ticker": row["ticker"], "side": row["side"],
                                          "quantity": row["quantity"], "cost_cents": cost_cents,
                                          "reason": reason})
            continue
        market_value_cents = price_cents * row["quantity"]
        unrealized_pnl_cents = market_value_cents - cost_cents
        positions.append({"ticker": row["ticker"], "side": row["side"], "quantity": row["quantity"],
                           "cost_cents": cost_cents, "market_value_cents": market_value_cents,
                           "unrealized_pnl_cents": unrealized_pnl_cents})
        total_cost_cents += cost_cents
        total_market_value_cents += market_value_cents
        total_unrealized_pnl_cents += unrealized_pnl_cents
    return {"positions": positions, "cost_cents": total_cost_cents,
            "market_value_cents": total_market_value_cents,
            "unrealized_pnl_cents": total_unrealized_pnl_cents, "valued_count": len(positions),
            "unvaluable_positions": unvaluable_positions, "unvaluable_count": len(unvaluable_positions)}


def equity_at_market(conn_or_db: "sqlite3.Connection | str | Path", now: datetime,
                      max_quote_age_seconds: int) -> dict:
    """Return cash plus the market value of open positions.

    This is the mark-to-market counterpart to ``PaperBroker.report()``'s
    ``equity_at_cost_cents``. It is still an estimate: positions that cannot
    be safely valued (stale, missing, or malformed quotes; no complementary
    depth) are excluded from ``equity_at_market_cents`` rather than assumed
    worthless or valued at cost, so this number is a lower bound on total
    equity whenever ``unvaluable_count`` is nonzero, not a complete picture.

    Args:
        conn_or_db: An open ``sqlite3.Connection`` to a paper database, or a
            path to one.
        now: The evaluation timestamp (timezone-aware UTC).
        max_quote_age_seconds: Passed through to ``mark_to_market``.

    Returns:
        A dict with cash_cents, market_value_cents, equity_at_market_cents
        (cash_cents + market_value_cents), unvaluable_count,
        unvaluable_positions, and a sober evidence_note.
    """
    with _open_connection(conn_or_db) as conn:
        cash_row = conn.execute("SELECT cash_cents FROM paper_account WHERE id=1").fetchone()
        cash_cents = cash_row["cash_cents"] if cash_row else 0
        marks = mark_to_market(conn, now, max_quote_age_seconds)
    return {"cash_cents": cash_cents, "market_value_cents": marks["market_value_cents"],
            "equity_at_market_cents": cash_cents + marks["market_value_cents"],
            "unvaluable_count": marks["unvaluable_count"],
            "unvaluable_positions": marks["unvaluable_positions"],
            "evidence_note": "Equity at market prices long YES off the best NO ask and long NO off "
                              "the best YES ask, never off a position's own-side ask. It is an "
                              "estimate, not a liquidation guarantee: thin depth and stale quotes "
                              "make it unreliable, and positions that cannot be safely valued are "
                              "excluded rather than assumed worthless or priced at cost -- see "
                              "unvaluable_positions."}
