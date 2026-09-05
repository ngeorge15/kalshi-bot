"""Edge decay analysis: do identified edges persist or close before settlement (R10.6)?

Reads ``price_observations`` (D5-03) rather than ``predictions``.  The distinction
matters: a prediction row is written when the bot evaluates a market it might
trade, whereas an observation is written every time a market is *sampled*.  Most
of the decay signal lives in markets that were never traded -- if an edge closes
before we would have entered, that is exactly what we need to know, and no trade
record would ever show it.

Sign convention throughout: ``edge_cents`` is ``model_prob * 100 - mid_price``.
Positive means the market is cheap relative to the model.  Decay is measured on
the *magnitude* of the edge, so a mispricing in either direction counts.

Usage:
    from src.analytics.edge_decay import record_observation, decay_by_bucket

    record_observation(db, ticker="KXNBAGAME-...", market_type="games",
                       mid_price_cents=52, model_prob=0.61, hours_to_close=6.0)
    buckets = decay_by_bucket(db, market_type="games")
"""

import logging
from datetime import datetime, timezone

import numpy as np

logger = logging.getLogger(__name__)

# Time-to-close buckets, in hours: (lower_inclusive, upper_exclusive).
# None as the upper bound means unbounded.
DEFAULT_BUCKETS: tuple[tuple[float, float | None], ...] = (
    (0.0, 3.0),
    (3.0, 12.0),
    (12.0, 24.0),
    (24.0, 48.0),
    (48.0, None),
)

# An edge counts as "persisted" if it retains at least this share of its
# original magnitude by the final observation before close.
PERSISTENCE_RATIO = 0.5

# Tickers whose initial edge is smaller than this are excluded from persistence
# statistics -- proportional decay of a 1-cent edge is noise, not signal.
MIN_INITIAL_EDGE_CENTS = 3.0


def record_observation(
    db,
    ticker: str,
    market_type: str,
    mid_price_cents: int,
    model_prob: float | None = None,
    yes_bid_cents: int | None = None,
    yes_ask_cents: int | None = None,
    hours_to_close: float | None = None,
    observed_at: str | None = None,
) -> int:
    """Record one market price sample (R10.6).

    Intended to be called on every market scan, including markets no signal was
    generated for.

    Args:
        db: :class:`~src.db.database.Database` instance.
        ticker: Kalshi market ticker.
        market_type: Free-form market type tag (D5-07: not validated).
        mid_price_cents: Mid price in integer cents, per CONVENTIONS.
        model_prob: Model probability at observation time, if a model ran.
        yes_bid_cents: Best YES bid, if known.
        yes_ask_cents: Best YES ask, if known.
        hours_to_close: Hours until the market closes. ``None`` when unknown.
        observed_at: ISO 8601 UTC timestamp. Defaults to now.

    Returns:
        The inserted row ID.
    """
    if observed_at is None:
        observed_at = datetime.now(timezone.utc).isoformat()

    edge_cents = (
        int(round(model_prob * 100 - mid_price_cents)) if model_prob is not None else None
    )

    cursor = db.execute(
        """INSERT INTO price_observations
        (ticker, market_type, observed_at, yes_bid_cents, yes_ask_cents,
         mid_price_cents, model_prob, edge_cents, hours_to_close)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            ticker,
            market_type,
            observed_at,
            yes_bid_cents,
            yes_ask_cents,
            mid_price_cents,
            model_prob,
            edge_cents,
            hours_to_close,
        ),
    )
    return cursor.lastrowid


def decay_curve(db, ticker: str) -> list[dict]:
    """Return one ticker's observation series, furthest from close first.

    Returns:
        Rows with ``observed_at``, ``hours_to_close``, ``mid_price_cents``,
        ``model_prob``, ``edge_cents`` and ``abs_edge_cents``.  Empty if the
        ticker has never been observed.
    """
    rows = db.fetchall(
        """SELECT observed_at, hours_to_close, mid_price_cents, model_prob, edge_cents
           FROM price_observations
           WHERE ticker = ?
           ORDER BY observed_at ASC""",
        (ticker,),
    )
    return [
        {
            **r,
            "abs_edge_cents": abs(r["edge_cents"]) if r["edge_cents"] is not None else None,
        }
        for r in rows
    ]


def decay_by_bucket(
    db,
    market_type: str | None = None,
    buckets: tuple[tuple[float, float | None], ...] = DEFAULT_BUCKETS,
) -> list[dict]:
    """Mean absolute edge per time-to-close bucket (R10.6).

    A downward trend as ``hours_to_close`` shrinks means edges are closing before
    settlement -- the market converges toward the model, and entering late
    captures less than the scan suggested.

    Observations with a NULL ``hours_to_close`` or NULL ``edge_cents`` are
    excluded; they cannot be placed on the time axis.

    Args:
        db: Database instance.
        market_type: Optional filter.
        buckets: ``(lower_inclusive, upper_exclusive)`` hour ranges. ``None`` as
            the upper bound means unbounded.

    Returns:
        One dict per bucket, ordered nearest-to-close first, with ``label``,
        ``hours_lower``, ``hours_upper``, ``n``, ``mean_abs_edge_cents`` and
        ``mean_edge_cents``. Empty buckets report ``n`` 0 and ``None`` means.
    """
    conditions = ["hours_to_close IS NOT NULL", "edge_cents IS NOT NULL"]
    params: list = []
    if market_type is not None:
        conditions.append("market_type = ?")
        params.append(market_type)

    rows = db.fetchall(
        "SELECT hours_to_close, edge_cents FROM price_observations WHERE "
        + " AND ".join(conditions),
        tuple(params),
    )

    out: list[dict] = []
    for lower, upper in buckets:
        if upper is None:
            selected = [r for r in rows if r["hours_to_close"] >= lower]
            label = f"{lower:g}h+"
        else:
            selected = [
                r for r in rows if lower <= r["hours_to_close"] < upper
            ]
            label = f"{lower:g}-{upper:g}h"

        edges = [r["edge_cents"] for r in selected]
        out.append(
            {
                "label": label,
                "hours_lower": lower,
                "hours_upper": upper,
                "n": len(edges),
                "mean_abs_edge_cents": float(np.mean(np.abs(edges))) if edges else None,
                "mean_edge_cents": float(np.mean(edges)) if edges else None,
            }
        )
    return out


def edge_persistence(
    db,
    market_type: str | None = None,
    min_initial_edge_cents: float = MIN_INITIAL_EDGE_CENTS,
    persistence_ratio: float = PERSISTENCE_RATIO,
) -> dict:
    """What fraction of identified edges survive to settlement (R10.6)?

    For each ticker, compares the earliest observation (furthest from close) with
    the latest.  An edge persisted if its magnitude at the final observation is
    at least ``persistence_ratio`` of its initial magnitude.

    Tickers with fewer than two observations, or an initial edge below
    ``min_initial_edge_cents``, are excluded -- proportional decay of a 1-cent
    edge is noise.

    Args:
        db: Database instance.
        market_type: Optional filter.
        min_initial_edge_cents: Minimum initial magnitude to be counted.
        persistence_ratio: Share of the initial magnitude that must remain.

    Returns:
        Dict with ``n_tickers`` (those evaluated), ``n_persisted``,
        ``persistence_rate`` (``None`` when nothing qualified),
        ``mean_initial_edge_cents``, ``mean_final_edge_cents`` and
        ``mean_retained_fraction``.
    """
    conditions = ["edge_cents IS NOT NULL"]
    params: list = []
    if market_type is not None:
        conditions.append("market_type = ?")
        params.append(market_type)

    rows = db.fetchall(
        "SELECT ticker, observed_at, edge_cents FROM price_observations WHERE "
        + " AND ".join(conditions)
        + " ORDER BY ticker ASC, observed_at ASC",
        tuple(params),
    )

    series: dict[str, list[int]] = {}
    for r in rows:
        series.setdefault(r["ticker"], []).append(r["edge_cents"])

    initials: list[float] = []
    finals: list[float] = []
    retained: list[float] = []
    n_persisted = 0

    for edges in series.values():
        if len(edges) < 2:
            continue
        initial = abs(edges[0])
        if initial < min_initial_edge_cents:
            continue
        final = abs(edges[-1])
        fraction = final / initial
        initials.append(initial)
        finals.append(final)
        retained.append(fraction)
        if fraction >= persistence_ratio:
            n_persisted += 1

    n = len(retained)
    return {
        "n_tickers": n,
        "n_persisted": n_persisted,
        "persistence_rate": (n_persisted / n) if n else None,
        "mean_initial_edge_cents": float(np.mean(initials)) if initials else None,
        "mean_final_edge_cents": float(np.mean(finals)) if finals else None,
        "mean_retained_fraction": float(np.mean(retained)) if retained else None,
    }


def decay_by_market_type(db) -> dict[str, dict]:
    """Edge persistence per market type, keyed by market type (D5-07).

    Market types are discovered, never enumerated.
    """
    rows = db.fetchall(
        "SELECT DISTINCT market_type FROM price_observations ORDER BY market_type"
    )
    return {r["market_type"]: edge_persistence(db, market_type=r["market_type"]) for r in rows}


def summarize_decay(db, market_type: str | None = None) -> dict:
    """Compact decay summary for the performance snapshot (R10.7).

    Returns:
        Dict with ``buckets`` (from :func:`decay_by_bucket`), ``persistence``
        (from :func:`edge_persistence`) and ``by_market_type``.
    """
    return {
        "buckets": decay_by_bucket(db, market_type=market_type),
        "persistence": edge_persistence(db, market_type=market_type),
        "by_market_type": decay_by_market_type(db),
    }
