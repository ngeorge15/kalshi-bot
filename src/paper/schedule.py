"""Scheduling and coverage tracking for repeated read-only observations.

`observe_once` collects one pass.  A prospective experiment needs many passes
over weeks, and the thing that makes such a series trustworthy is not the
individual pass but the *record of attempts*: which tickers were due, which were
actually observed, and where collection stopped.

A series with silent holes is not the experiment that was declared.  If the
collector dies on a Friday and resumes on a Monday, the three missing city-days
are not missing at random -- they may be exactly the days a forecast was hard --
and nothing in the settled outcomes themselves would reveal the gap.  So every
attempt is journalled to `paper_observation_runs`, including skips and errors,
and :func:`coverage_report` reads that journal back.

This module never decides that results are good enough to stop.  The stopping
rule belongs to the predeclared protocol (see :mod:`src.paper.protocol`), and
letting a scheduler halt on results would be the exact optional-stopping bias
predeclaration exists to prevent.

Usage:
    from src.paper.schedule import run_scheduled_pass, coverage_report

    outcome = run_scheduled_pass(broker, watchlist, now)
    coverage = coverage_report(broker.db_path, watchlist, now)
"""

import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

# Default gap between observations of the same ticker.  Hourly is frequent
# enough to catch quote movement through a trading day without hammering public
# endpoints; the NWS hourly forecast itself updates about this often.
DEFAULT_INTERVAL_SECONDS = 3600

# A collection gap longer than this is reported as a hole in the series rather
# than normal spacing.  Set to three intervals so one transient failure and a
# retry do not register as a gap.
GAP_MULTIPLE = 3

# Statuses that mean the market was genuinely looked at, as opposed to skipped
# for eligibility or failed outright.
OBSERVED_STATUSES = frozenset({"observed", "filled", "resting", "settled"})


def _connect(conn_or_db):
    """Return ``(connection, owns_it)`` accepting a live connection or a path."""
    if isinstance(conn_or_db, sqlite3.Connection):
        conn_or_db.row_factory = sqlite3.Row
        return conn_or_db, False
    connection = sqlite3.connect(str(conn_or_db))
    connection.row_factory = sqlite3.Row
    return connection, True


def _utc(value: str) -> datetime:
    """Parse an ISO 8601 UTC timestamp, tolerating a trailing Z."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def last_observed_at(conn_or_db, ticker: str) -> str | None:
    """Return the most recent attempt time for *ticker*, or None if never tried.

    Any attempt counts, including a skip or an error.  Retrying a failing ticker
    every few seconds would be a denial-of-service pattern against a public API,
    so backoff is driven by attempts rather than successes.
    """
    conn, owns = _connect(conn_or_db)
    try:
        row = conn.execute(
            "SELECT MAX(run_at) AS last FROM paper_observation_runs WHERE ticker = ?",
            (ticker,),
        ).fetchone()
        return row["last"] if row and row["last"] else None
    finally:
        if owns:
            conn.close()


def due_entries(conn_or_db, watchlist: list[dict], now: datetime,
                interval_seconds: int = DEFAULT_INTERVAL_SECONDS) -> list[dict]:
    """Select the watchlist entries due for observation at *now*.

    Args:
        conn_or_db: Open connection or path to the paper database.
        watchlist: Reviewed watchlist entries.
        now: Current time; passed in so scheduling is deterministic in tests.
        interval_seconds: Minimum gap between attempts on the same ticker.

    Returns:
        The subset of *watchlist* whose ticker has never been attempted or was
        last attempted at least *interval_seconds* ago.  Entries without a
        usable ticker are returned so the collector can report them rather than
        dropping them silently.
    """
    if not isinstance(watchlist, list):
        raise ValueError("Watchlist must be a JSON list")
    if not isinstance(interval_seconds, int) or interval_seconds <= 0:
        raise ValueError("interval_seconds must be a positive integer")

    conn, owns = _connect(conn_or_db)
    try:
        due = []
        for entry in watchlist:
            ticker = entry.get("ticker") if isinstance(entry, dict) else None
            if not isinstance(ticker, str) or not ticker.strip():
                due.append(entry)
                continue
            last = last_observed_at(conn, ticker)
            if last is None or (now - _utc(last)).total_seconds() >= interval_seconds:
                due.append(entry)
        return due
    finally:
        if owns:
            conn.close()


def record_attempts(conn_or_db, results: list[dict], now: datetime) -> int:
    """Journal one pass's per-ticker outcomes.

    Args:
        conn_or_db: Open connection or path to the paper database.
        results: The list returned by ``observe_once``.
        now: Attempt timestamp.

    Returns:
        Number of rows written.
    """
    conn, owns = _connect(conn_or_db)
    try:
        written = 0
        for item in results:
            conn.execute(
                "INSERT INTO paper_observation_runs (run_at, ticker, status, reason) "
                "VALUES (?, ?, ?, ?)",
                (now.isoformat(), str(item.get("ticker") or "unknown"),
                 str(item.get("status") or "unknown"), item.get("reason")),
            )
            written += 1
        conn.commit()
        return written
    finally:
        if owns:
            conn.close()


def run_scheduled_pass(broker, watchlist: list[dict], now: datetime, reader=None,
                       interval_seconds: int = DEFAULT_INTERVAL_SECONDS) -> dict:
    """Observe the entries that are due and journal what happened.

    Performs no work when nothing is due, so this is safe to invoke on a short
    external timer.

    Args:
        broker: A forward-mode :class:`~src.paper.broker.PaperBroker`.
        watchlist: Reviewed watchlist entries.
        now: Current time, supplied by the caller for determinism.
        reader: Optional injected data reader, for offline tests.
        interval_seconds: Minimum gap between attempts on the same ticker.

    Returns:
        Dict with ``ran`` (bool), ``due_count``, ``results`` (from
        ``observe_once``), ``recorded`` and ``next_due_at``.
    """
    from src.paper.observe import observe_once

    due = due_entries(broker.db_path, watchlist, now, interval_seconds)
    if not due:
        logger.debug("No watchlist entries due at %s", now.isoformat())
        return {"ran": False, "due_count": 0, "results": [], "recorded": 0,
                "next_due_at": next_due_at(broker.db_path, watchlist, interval_seconds)}

    results = observe_once(broker, due, reader=reader, clock=lambda: now)
    recorded = record_attempts(broker.db_path, results, now)
    logger.info("Observed %d due ticker(s) at %s", recorded, now.isoformat())
    return {"ran": True, "due_count": len(due), "results": results, "recorded": recorded,
            "next_due_at": next_due_at(broker.db_path, watchlist, interval_seconds)}


def next_due_at(conn_or_db, watchlist: list[dict],
                interval_seconds: int = DEFAULT_INTERVAL_SECONDS) -> str | None:
    """Earliest time any watchlist ticker becomes due again.

    Returns:
        An ISO 8601 timestamp, or ``None`` when the watchlist is empty or holds
        a ticker never yet attempted (which is due immediately).
    """
    conn, owns = _connect(conn_or_db)
    try:
        soonest = None
        for entry in watchlist:
            ticker = entry.get("ticker") if isinstance(entry, dict) else None
            if not isinstance(ticker, str) or not ticker.strip():
                continue
            last = last_observed_at(conn, ticker)
            if last is None:
                return None
            candidate = _utc(last) + timedelta(seconds=interval_seconds)
            soonest = candidate if soonest is None or candidate < soonest else soonest
        return soonest.isoformat() if soonest else None
    finally:
        if owns:
            conn.close()


def collection_gaps(conn_or_db, ticker: str,
                    interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
                    gap_multiple: int = GAP_MULTIPLE) -> list[dict]:
    """Find breaks in one ticker's observation history.

    A gap is a pause longer than ``interval_seconds * gap_multiple`` between
    consecutive attempts.

    Returns:
        One dict per gap with ``after``, ``before`` and ``seconds``, oldest
        first.  Empty when the series is continuous or too short to judge.
    """
    conn, owns = _connect(conn_or_db)
    try:
        rows = conn.execute(
            "SELECT run_at FROM paper_observation_runs WHERE ticker = ? ORDER BY run_at ASC",
            (ticker,),
        ).fetchall()
        threshold = interval_seconds * gap_multiple
        gaps = []
        for earlier, later in zip(rows, rows[1:]):
            seconds = (_utc(later["run_at"]) - _utc(earlier["run_at"])).total_seconds()
            if seconds > threshold:
                gaps.append({"after": earlier["run_at"], "before": later["run_at"],
                             "seconds": seconds})
        return gaps
    finally:
        if owns:
            conn.close()


def coverage_report(conn_or_db, watchlist: list[dict], now: datetime,
                    interval_seconds: int = DEFAULT_INTERVAL_SECONDS) -> dict:
    """Summarise collection coverage across the watchlist.

    This is the honesty check on a prospective run: it reports what was actually
    collected and where the holes are, rather than assuming the scheduler ran as
    intended.

    Returns:
        Dict with ``tickers`` (per-ticker attempt counts, observed counts, first
        and last attempt, and gaps), ``total_attempts``, ``total_observed``,
        ``tickers_never_observed`` and ``total_gaps``.
    """
    conn, owns = _connect(conn_or_db)
    try:
        per_ticker = []
        never = []
        total_attempts = total_observed = total_gaps = 0
        for entry in watchlist:
            ticker = entry.get("ticker") if isinstance(entry, dict) else None
            if not isinstance(ticker, str) or not ticker.strip():
                continue
            rows = conn.execute(
                "SELECT run_at, status FROM paper_observation_runs WHERE ticker = ? "
                "ORDER BY run_at ASC", (ticker,)).fetchall()
            observed = sum(1 for r in rows if r["status"] in OBSERVED_STATUSES)
            gaps = collection_gaps(conn, ticker, interval_seconds)
            if not rows:
                never.append(ticker)
            per_ticker.append({
                "ticker": ticker, "attempts": len(rows), "observed": observed,
                "first_attempt_at": rows[0]["run_at"] if rows else None,
                "last_attempt_at": rows[-1]["run_at"] if rows else None,
                "gaps": gaps,
            })
            total_attempts += len(rows)
            total_observed += observed
            total_gaps += len(gaps)
        return {"as_of": now.isoformat(), "tickers": per_ticker,
                "total_attempts": total_attempts, "total_observed": total_observed,
                "tickers_never_observed": never, "total_gaps": total_gaps,
                "note": ("Attempts include skips and errors. A gap means collection paused "
                         "for longer than the configured interval; missing days are not "
                         "necessarily missing at random and should be reported alongside "
                         "any result computed from this series.")}
    finally:
        if owns:
            conn.close()
