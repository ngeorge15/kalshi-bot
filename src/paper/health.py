"""Input health checks for paper-trading data: quotes, forecasts, watchlist
eligibility, and settlement completeness.

A research run is only as good as its inputs. None of these checks make a
trading decision or touch the network; each reads the local paper database
(and, for the watchlist check, an in-memory watchlist already loaded by the
caller) and reports what it finds. Every check returns a structured result
with a severity, a human-readable reason, and the offending items -- never a
bare bool -- so a caller can both gate on severity and show the details.

Severity convention used throughout this module:
    ok: nothing offending found.
    warn: offending items exist, but the condition is a normal, recoverable
        part of operating this system (a quote due for its next refresh, a
        watchlist entry not yet reviewed as eligible). The shipped example
        watchlist (examples/paper_watchlist.json) ships with
        ``"available": false`` and placeholder tickers -- ineligibility is
        the expected default state, not an anomaly, and does not warrant
        ``fail``.
    fail: offending items indicate missing or structurally broken data that
        can silently corrupt results if not addressed (a market past close
        with no settlement recorded; a watchlist entry whose eligibility
        block is missing or malformed rather than merely not-yet-eligible).
"""
from contextlib import contextmanager
from datetime import datetime
import logging
from pathlib import Path
import sqlite3
from typing import Iterator

from src.paper.broker import utc

logger = logging.getLogger(__name__)

# Ranks severities so health_report can take the worst of several checks.
# Higher is worse.
SEVERITY_RANK = {"ok": 0, "warn": 1, "fail": 2}


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


def _result(check: str, severity: str, reason: str, items: list) -> dict:
    """Build one structured check result.

    Args:
        check: Short machine-readable name of the check.
        severity: One of "ok", "warn", "fail".
        reason: Human-readable summary of the verdict.
        items: The offending items backing the verdict (empty when ok).

    Returns:
        A dict with keys check, severity, reason, items, count.
    """
    if severity not in SEVERITY_RANK:
        raise ValueError(f"Unknown severity: {severity}")
    return {"check": check, "severity": severity, "reason": reason, "items": items, "count": len(items)}


def quote_staleness(conn_or_db: "sqlite3.Connection | str | Path", now: datetime,
                     max_quote_age_seconds: int) -> dict:
    """Flag open markets whose most recent quote is older than allowed.

    Only markets that have not yet settled (``result IS NULL``) are in scope;
    a stale quote on a settled market cannot corrupt a live decision.

    Args:
        conn_or_db: An open ``sqlite3.Connection`` to a paper database, or a
            path to one.
        now: The evaluation timestamp (timezone-aware UTC). Callers must pass
            this explicitly; it is never read from the wall clock here.
        max_quote_age_seconds: Quotes older than this are considered stale.

    Returns:
        A structured result (see module docstring). Severity is "warn" when
        stale quotes exist -- staleness is an expected, recoverable state
        between polls, not evidence of corruption by itself.
    """
    with _open_connection(conn_or_db) as conn:
        rows = conn.execute(
            "SELECT ticker, quote_at FROM paper_markets WHERE result IS NULL ORDER BY ticker").fetchall()
    items = []
    for row in rows:
        try:
            age_seconds = (now - utc(row["quote_at"])).total_seconds()
        except ValueError:
            items.append({"ticker": row["ticker"], "quote_at": row["quote_at"],
                          "reason": "invalid_quote_timestamp"})
            continue
        if age_seconds > max_quote_age_seconds or age_seconds < 0:
            items.append({"ticker": row["ticker"], "quote_at": row["quote_at"],
                          "age_seconds": age_seconds,
                          "reason": "stale_quote" if age_seconds > max_quote_age_seconds
                          else "quote_timestamp_in_future"})
    severity = "warn" if items else "ok"
    reason = f"{len(items)} open market(s) with a quote older than {max_quote_age_seconds}s or timestamped in the future" \
        if items else "All open markets have a recent quote"
    return _result("quote_staleness", severity, reason, items)


def forecast_staleness(conn_or_db: "sqlite3.Connection | str | Path", now: datetime,
                        max_forecast_age_seconds: int) -> dict:
    """Flag open markets whose latest forecast per model/version is too old.

    Only the most recent prediction per (ticker, model_name, model_version)
    is considered per group -- an old prediction that has already been
    superseded by a fresh one is not itself a problem. Only markets that have
    not yet settled are in scope, for the same reason as quote_staleness.

    Args:
        conn_or_db: An open ``sqlite3.Connection`` to a paper database, or a
            path to one.
        now: The evaluation timestamp (timezone-aware UTC).
        max_forecast_age_seconds: Forecasts older than this are stale.

    Returns:
        A structured result (see module docstring). Severity is "warn" when
        stale forecasts exist.
    """
    with _open_connection(conn_or_db) as conn:
        rows = conn.execute("""
            SELECT p.ticker AS ticker, p.model_name AS model_name, p.model_version AS model_version,
                   MAX(p.created_at) AS created_at
            FROM paper_predictions p
            JOIN paper_markets m USING(ticker)
            WHERE m.result IS NULL
            GROUP BY p.ticker, p.model_name, p.model_version
            ORDER BY p.ticker, p.model_name, p.model_version
        """).fetchall()
    items = []
    for row in rows:
        try:
            age_seconds = (now - utc(row["created_at"])).total_seconds()
        except ValueError:
            items.append({"ticker": row["ticker"], "model_name": row["model_name"],
                          "model_version": row["model_version"], "created_at": row["created_at"],
                          "reason": "invalid_forecast_timestamp"})
            continue
        if age_seconds > max_forecast_age_seconds or age_seconds < 0:
            items.append({"ticker": row["ticker"], "model_name": row["model_name"],
                          "model_version": row["model_version"], "created_at": row["created_at"],
                          "age_seconds": age_seconds,
                          "reason": "stale_forecast" if age_seconds > max_forecast_age_seconds
                          else "forecast_timestamp_in_future"})
    severity = "warn" if items else "ok"
    reason = (f"{len(items)} open market forecast(s) older than {max_forecast_age_seconds}s or "
              "timestamped in the future") if items else "All open market forecasts are recent"
    return _result("forecast_staleness", severity, reason, items)


def watchlist_eligibility(watchlist: list[dict], now: datetime) -> dict:
    """Flag watchlist entries that are not currently eligible to observe.

    An entry is eligible only when its ``eligibility`` block is present and
    well-formed, ``available`` is exactly ``True``, and ``now`` falls within
    ``[checked_at, expires_at)`` -- the same gate ``observe.observe_once``
    applies before fetching or trading a ticker. The shipped example
    watchlist deliberately ships with ``"available": false`` and placeholder
    tickers, so most entries being ineligible is the normal state of an
    unreviewed watchlist, not a bug; this is reported as "warn". A
    structurally broken eligibility block (missing entirely, not a dict, or
    with missing/unparseable timestamps) is a data problem rather than a
    normal not-yet-eligible state, and is reported as "fail".

    Args:
        watchlist: The in-memory watchlist (a JSON list of dicts, matching
            examples/paper_watchlist.json).
        now: The evaluation timestamp (timezone-aware UTC).

    Returns:
        A structured result (see module docstring).
    """
    ineligible = []
    malformed = []
    for index, entry in enumerate(watchlist):
        ticker = entry.get("ticker") if isinstance(entry, dict) else None
        eligibility = entry.get("eligibility") if isinstance(entry, dict) else None
        if not isinstance(entry, dict) or not isinstance(eligibility, dict):
            malformed.append({"index": index, "ticker": ticker, "reason": "eligibility_missing"})
            continue
        if eligibility.get("available") is not True:
            ineligible.append({"index": index, "ticker": ticker, "reason": "not_available"})
            continue
        try:
            checked_at = utc(eligibility["checked_at"])
            expires_at = utc(eligibility["expires_at"])
        except (KeyError, ValueError, TypeError):
            malformed.append({"index": index, "ticker": ticker, "reason": "malformed_eligibility_timestamps"})
            continue
        if now < checked_at:
            ineligible.append({"index": index, "ticker": ticker, "reason": "checked_at_in_future"})
        elif now >= expires_at:
            ineligible.append({"index": index, "ticker": ticker, "reason": "expired"})
    if malformed:
        severity = "fail"
    elif ineligible:
        severity = "warn"
    else:
        severity = "ok"
    items = malformed + ineligible
    total_flagged = len(items)
    reason = (f"{len(malformed)} entr(y/ies) with a missing/malformed eligibility block, "
              f"{len(ineligible)} not currently eligible") if total_flagged else \
        "All watchlist entries are currently eligible"
    return _result("watchlist_eligibility", severity, reason, items)


def unresolved_closed_markets(conn_or_db: "sqlite3.Connection | str | Path", now: datetime) -> dict:
    """Flag markets past their close time with no recorded settlement result.

    A market with ``close_at <= now`` and ``result IS NULL`` means settlement
    data is missing -- fills against it cannot be marked realized, and any
    equity/Brier calculation that depends on a result is silently incomplete
    until this is fixed.

    Args:
        conn_or_db: An open ``sqlite3.Connection`` to a paper database, or a
            path to one.
        now: The evaluation timestamp (timezone-aware UTC).

    Returns:
        A structured result (see module docstring). Severity is "fail" when
        any such market exists.
    """
    with _open_connection(conn_or_db) as conn:
        rows = conn.execute(
            "SELECT ticker, close_at FROM paper_markets WHERE result IS NULL ORDER BY ticker").fetchall()
    items = [{"ticker": row["ticker"], "close_at": row["close_at"]}
             for row in rows if utc(row["close_at"]) <= now]
    severity = "fail" if items else "ok"
    reason = f"{len(items)} market(s) past close with no settlement result recorded" if items \
        else "No closed markets are missing a settlement result"
    return _result("unresolved_closed_markets", severity, reason, items)


def health_report(conn_or_db: "sqlite3.Connection | str | Path", watchlist: list[dict], now: datetime,
                   max_quote_age_seconds: int, max_forecast_age_seconds: int) -> dict:
    """Run every health check and roll them up into one verdict.

    Args:
        conn_or_db: An open ``sqlite3.Connection`` to a paper database, or a
            path to one.
        watchlist: The in-memory watchlist to check eligibility against.
        now: The evaluation timestamp (timezone-aware UTC).
        max_quote_age_seconds: Threshold for quote_staleness.
        max_forecast_age_seconds: Threshold for forecast_staleness.

    Returns:
        A dict with ``checks`` (the four structured results, keyed by check
        name) and ``severity`` (the worst severity across all checks -- "ok"
        only when every individual check is "ok"). A "fail" verdict here
        means a downstream report should be treated with suspicion, not that
        the run itself is invalid; humans still decide what to do about it.
    """
    with _open_connection(conn_or_db) as conn:
        checks = {
            "quote_staleness": quote_staleness(conn, now, max_quote_age_seconds),
            "forecast_staleness": forecast_staleness(conn, now, max_forecast_age_seconds),
            "watchlist_eligibility": watchlist_eligibility(watchlist, now),
            "unresolved_closed_markets": unresolved_closed_markets(conn, now),
        }
    worst = max(checks.values(), key=lambda result: SEVERITY_RANK[result["severity"]])
    if SEVERITY_RANK[worst["severity"]] > SEVERITY_RANK["ok"]:
        logger.info("Paper data health verdict: %s (%s)", worst["severity"], worst["reason"])
    return {"severity": worst["severity"], "checks": checks}
