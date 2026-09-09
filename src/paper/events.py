"""Event-level clustering for paired Brier score evaluation.

On Kalshi, one real-world event (one city, one date) is typically offered as
many bracket markets — "high temp 69-71", "71-73", and so on. Treating each
bracket as an independent observation overstates how much evidence a paired
Brier comparison actually carries: twenty-two brackets for one city-day are
close to ONE independent observation, not twenty-two. `paper_markets` already
carries `event_key` for this grouping; this module turns it into clustering
statistics. `src/paper/uncertainty.py` builds the actual interval estimates
on top of it.

Row format: each row is a dict with at least an `event_key: str` key. Other
keys (e.g. `model_brier`, `market_brier`) are passed through unchanged and
are irrelevant to this module — it only groups and counts.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Below this many total markets, "every market is its own event" and "every
# market shares one event" are the same degenerate case (a single market
# trivially satisfies both n_events == n_markets and n_events == 1), so there
# is no meaningful distinction to report.
MIN_MARKETS_FOR_DEGENERACY_CHECK = 2


def _validate_event_key(row: dict[str, Any]) -> str:
    """Return the validated event_key from one row.

    Args:
        row: A prediction/outcome row expected to carry `event_key`.

    Returns:
        The row's event_key, unchanged.

    Raises:
        ValueError: If event_key is missing, None, empty, or whitespace-only.
            Refusing here prevents silently forming a junk cluster.
    """
    key = row.get("event_key")
    if key is None or not isinstance(key, str) or not key.strip():
        raise ValueError(f"Row has invalid event_key: {key!r}; refusing to form a junk cluster")
    return key


def cluster_by_event(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group rows by event_key into a stable, sorted list of clusters.

    Args:
        rows: Prediction/outcome rows, each a dict with an `event_key`
            string key. Extra keys are preserved and passed through
            unchanged.

    Returns:
        A list of `{"event_key": str, "n_markets": int, "rows": list[dict]}`
        dicts sorted ascending by event_key, so output is deterministic
        regardless of input order. Rows within a cluster keep their original
        relative order. Empty input returns an empty list.

    Raises:
        ValueError: If any row has a missing, None, empty, or
            whitespace-only event_key.
    """
    for row in rows:
        _validate_event_key(row)
    clusters: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        clusters.setdefault(row["event_key"], []).append(row)
    return [{"event_key": key, "n_markets": len(clusters[key]), "rows": clusters[key]}
            for key in sorted(clusters)]


def cluster_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize how markets distribute across events.

    Args:
        rows: Prediction/outcome rows as in `cluster_by_event`.

    Returns:
        A dict with `n_markets`, `n_events`, `mean_markets_per_event`,
        `max_markets_per_event`, and `largest_event_share` (the fraction of
        all markets that belong to the single largest event). Statistics
        that are undefined on empty input are `None`, never NaN or a
        fabricated number.

    Raises:
        ValueError: Propagated from `cluster_by_event` for invalid
            event_key values.
    """
    clusters = cluster_by_event(rows)
    n_markets = len(rows)
    n_events = len(clusters)
    if n_events == 0:
        return {"n_markets": 0, "n_events": 0, "mean_markets_per_event": None,
                "max_markets_per_event": None, "largest_event_share": None}
    sizes = [cluster["n_markets"] for cluster in clusters]
    return {"n_markets": n_markets, "n_events": n_events,
            "mean_markets_per_event": n_markets / n_events,
            "max_markets_per_event": max(sizes),
            "largest_event_share": max(sizes) / n_markets}


def degenerate_clustering(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Detect the two failure modes that silently defeat event clustering.

    Case "no_clustering": every market is its own event
    (n_events == n_markets), so the event_key grouping carries no
    information beyond the naive per-market count — a clustered estimate
    would just equal the naive one.

    Case "single_event": every market belongs to one event, so the data
    supports exactly one independent observation no matter how many markets
    it contains.

    Args:
        rows: Prediction/outcome rows as in `cluster_by_event`.

    Returns:
        `{"degenerate": bool, "case": str | None, "reason": str | None,
        "n_markets": int, "n_events": int}`. Not degenerate (case/reason
        `None`) for empty input, a single market, or a genuine mix of event
        sizes — those are not silent failures of clustering.

    Raises:
        ValueError: Propagated from `cluster_by_event` for invalid
            event_key values.
    """
    summary = cluster_summary(rows)
    n_markets, n_events = summary["n_markets"], summary["n_events"]
    if n_markets == 0:
        return {"degenerate": False, "case": None, "reason": None, "n_markets": 0, "n_events": 0}
    if n_markets >= MIN_MARKETS_FOR_DEGENERACY_CHECK and n_events == 1:
        return {"degenerate": True, "case": "single_event",
                "reason": (f"all {n_markets} markets share one event_key; the data supports "
                           "exactly one independent observation, not " + str(n_markets)),
                "n_markets": n_markets, "n_events": n_events}
    if n_markets >= MIN_MARKETS_FOR_DEGENERACY_CHECK and n_events == n_markets:
        return {"degenerate": True, "case": "no_clustering",
                "reason": (f"each of the {n_markets} markets has a unique event_key; clustering "
                           "carries no information beyond the naive per-market count"),
                "n_markets": n_markets, "n_events": n_events}
    return {"degenerate": False, "case": None, "reason": None, "n_markets": n_markets, "n_events": n_events}
