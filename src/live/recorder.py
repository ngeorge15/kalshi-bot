"""Read-only recorder for live Kalshi weather order books.

Captures order-book snapshots for the daily-high-temperature series
(KXHIGHNY / KXHIGHCHI / KXHIGHMIA / KXHIGHAUS -- see
`src.data.kalshi_history`) to newline-delimited JSON on disk, so intraday
book behaviour can be studied and a live arbitrage check (`src.live.arb`)
can run against something more current than the historical candlesticks.

**Read-only by construction.** Every HTTP request this module makes goes
through `_get`, which checks the request path against `_ALLOWED_PATH_RES`
before sending it. That allowlist only contains market/orderbook/event GET
paths -- an order or portfolio path raises `ValueError` before any request
is sent (see `tests/live/test_recorder.py::test_url_allowlist_rejects_order_path`).
This module never imports `src.kalshi.auth` or `src.config`, and never reads
`KALSHI_API_KEY_ID` / `KALSHI_PRIVATE_KEY_PATH` -- it has no way to place an
order even if the allowlist were bypassed, because it never signs anything.

**Polling now, websocket later.** Kalshi's public REST endpoints
(`https://external-api.kalshi.com/trade-api/v2`) need no credentials and
serve real production data -- confirmed by every other module under
`src/data` and `src/paper`. Kalshi's WebSocket feed is richer (real-time
deltas instead of a polled snapshot) but requires an authenticated
RSA-PSS-signed handshake even to subscribe to public market-data channels,
and the user currently holds only DEMO credentials, whose sandbox markets
are not the real weather markets this project studies. So this module
implements only a polling backend, behind the `BookSource` seam below, and
leaves the websocket backend unimplemented on purpose.

A websocket backend, when production credentials exist, would need to:

1. Sign the WS upgrade handshake the same way `src.kalshi.auth.KalshiAuth`
   signs REST requests (RSA-PSS over `{timestamp}{method}{path}`), which
   means it *would* need to import credentials -- unlike this module, it
   could not stay credential-free. It should live in its own module (e.g.
   `src.live.ws_source`), not be folded into this one, so `recorder.py`
   keeps its "never reads credentials" guarantee.
2. Subscribe to the `orderbook_delta` (or equivalent) channel per market
   and maintain a local full-depth book per ticker from the initial
   snapshot message plus subsequent deltas -- WS updates are incremental,
   so the backend owns reconstructing a consistent ladder, not just
   forwarding messages.
3. On each update (or on a throttled timer), emit a snapshot dict in
   *exactly* the shape documented below, with `"source": "websocket"`, and
   hand it to the same `record`-style ndjson writer -- `record` and
   `poll_once` never need to change, because both backends satisfy the same
   `BookSource` protocol.
4. Implement its own reconnect/backoff loop (WS disconnects are normal),
   whereas `record`'s poll-sleep loop is specific to the REST backend.

**Snapshot shape** (exact -- also consumed by `src.live.arb`)::

    {
      "captured_utc": "2026-09-19T04:05:06Z",   # one value per poll cycle (groups a ladder)
      "received_utc": "2026-09-19T04:05:07Z",   # when THIS market's orderbook response arrived
      "source": "rest_poll",                     # or "websocket" later
      "event_ticker": "KXHIGHNY-26SEP19",
      "ticker": "KXHIGHNY-26SEP19-B80.5",
      "station": "KNYC",
      "date_lst": "2026-09-19",
      "lower_bound_f": 79.5, "upper_bound_f": 81.5,   # may be None for open tails
      "yes_bids": [[price_cents, size], ...],   # price descending, ints
      "yes_asks": [[price_cents, size], ...],   # price ascending, ints
      "status": "active",
      "close_time": "2026-09-20T03:59:00Z",
    }

A failed poll (the whole request cycle raised, not just one market) is
recorded as a `"gap"` line instead of silently producing a hole in the
series -- see `record`'s docstring and `src.paper.schedule`'s coverage
journal, which treats a silent gap the same way: as a correctness issue,
not a cosmetic one.

Usage::

    python -m src.live.recorder --stations KNYC,KMDW,KMIA,KAUS \\
        --date 2026-09-19 --out data/live/books-2026-09-19.jsonl \\
        --interval 15 --minutes 60
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from src.data.kalshi_history import SERIES_STATIONS, event_ticker_for_date, parse_market

logger = logging.getLogger(__name__)

# --- HTTP -----------------------------------------------------------------

BASE_URL = "https://external-api.kalshi.com/trade-api/v2"
REQUEST_TIMEOUT = (5, 30)  # (connect, read) seconds

# Read-only allowlist: only these path shapes may ever be requested from this
# module, checked in `_get` before any request is sent. No order/portfolio
# path can ever match.
_ALLOWED_PATH_RES = (
    re.compile(r"^/trade-api/v2/markets$"),
    re.compile(r"^/trade-api/v2/markets/[^/]+$"),
    re.compile(r"^/trade-api/v2/markets/[^/]+/orderbook$"),
    re.compile(r"^/trade-api/v2/events$"),
    re.compile(r"^/trade-api/v2/events/[^/]+$"),
)

# Contract prices are whole cents in [1, 99]; 0 and 100 are settled states,
# not quotable prices. Duplicated (not imported) from `src/paper/venue.py`
# so this module keeps no dependency on `src.paper` -- see that module's own
# comment on the same pattern for `KALSHI_SETTLED_STATUSES`.
MIN_PRICE_CENTS = 1
MAX_PRICE_CENTS = 99

_SESSION: requests.Session | None = None


def _get_session() -> requests.Session:
    """Build the module-level `requests.Session` (GET-only retries, no auth)."""
    global _SESSION
    if _SESSION is None:
        session = requests.Session()
        retry = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        session.headers.update({"User-Agent": "kalshi-bot-live-recorder/1.0"})
        _SESSION = session
    return _SESSION


def _get(session: requests.Session, url: str, params: dict | None = None) -> requests.Response:
    """The only way this module makes a request: checks `url` against the allowlist first.

    Raises:
        ValueError: If `url`'s path does not match `_ALLOWED_PATH_RES`. No
            request is sent in that case.
    """
    path = urlparse(url).path
    if not any(pattern.match(path) for pattern in _ALLOWED_PATH_RES):
        raise ValueError(f"Refusing non-allowlisted URL for a read-only recorder: {url!r}")
    response = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response


# --- Order-book conversion --------------------------------------------------

def _levels_from_dollars(raw_levels: Any, complement: bool) -> dict[int, int]:
    """Convert `[[price_str, size_str], ...]` fixed-point levels to `{cents: size}`.

    `complement=False` reads the level directly (used for YES bids): price
    rounds DOWN and size rounds DOWN, so a simulated fill against this bid is
    never flattered by rounding. `complement=True` takes `100 - price`
    (used to turn NO bids into YES asks, mirroring
    `src.paper.venue.asks_from_orderbook`): price rounds UP (a higher ask is
    worse for a simulated buyer) and size still rounds DOWN.

    Unlike `asks_from_orderbook`, a malformed individual level (non-numeric,
    non-finite, out of `[0, 1]`, negative size) is skipped rather than
    raising -- one bad row in a live feed must not lose the rest of a book
    that is otherwise fine to record.
    """
    levels: dict[int, int] = {}
    for entry in raw_levels or []:
        try:
            price_raw, size_raw = entry
            price, size = Decimal(str(price_raw)), Decimal(str(size_raw))
            if not price.is_finite() or not size.is_finite():
                continue
            if not (0 <= price <= 1) or size < 0:
                continue
            if complement:
                cents = int(((1 - price) * 100).to_integral_value(rounding=ROUND_CEILING))
            else:
                cents = int((price * 100).to_integral_value(rounding=ROUND_FLOOR))
            count = int(size.to_integral_value(rounding=ROUND_FLOOR))
        except (TypeError, ValueError, InvalidOperation):
            continue
        if MIN_PRICE_CENTS <= cents <= MAX_PRICE_CENTS and count:
            levels[cents] = levels.get(cents, 0) + count
    return levels


def orderbook_to_ladders(payload: dict) -> tuple[list[list[int]], list[list[int]]]:
    """Build `(yes_bids, yes_asks)` full ladders from a raw Kalshi orderbook payload.

    `yes_bids` reads `orderbook_fp.yes_dollars` directly, sorted price
    descending. `yes_asks` is the complement of `orderbook_fp.no_dollars`
    (a YES ask is `100 - no_bid_price`), sorted price ascending -- the same
    conversion `src.paper.venue.asks_from_orderbook` applies to a single
    level, applied here to the full ladder.

    Returns:
        `([price_cents, size], ...)` for each side. Either list may be
        empty (a one-sided or fully empty book) -- both keys are always
        present in the caller's snapshot regardless.
    """
    book = payload.get("orderbook_fp") if isinstance(payload, dict) else None
    book = book if isinstance(book, dict) else {}
    yes_bid_levels = _levels_from_dollars(book.get("yes_dollars"), complement=False)
    yes_ask_levels = _levels_from_dollars(book.get("no_dollars"), complement=True)
    yes_bids = [list(item) for item in sorted(yes_bid_levels.items(), reverse=True)]
    yes_asks = [list(item) for item in sorted(yes_ask_levels.items())]
    return yes_bids, yes_asks


# --- Polling backend ---------------------------------------------------------

_STATION_TO_SERIES: dict[str, str] = {station: series for series, station in SERIES_STATIONS.items()}


def _utc_now_iso(clock: Callable[[], datetime] | None = None) -> str:
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    return now.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def poll_once(
    stations: list[str],
    target_date: str | date,
    session: requests.Session | None = None,
    stats: dict[str, int] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> list[dict]:
    """One polling pass: resolve each station's event, fetch each market's book.

    Args:
        stations: Station identifiers (e.g. `"KNYC"`), matched against
            `src.data.kalshi_history.SERIES_STATIONS`.
        target_date: The local-standard event date to poll.
        session: Optional session (tests; production uses `_get_session()`).
        stats: Optional `{"captured": int, "skipped": int}` accumulator,
            mutated in place so a caller (e.g. `record`) can log counts
            without changing this function's return type.
        clock: Optional `() -> aware datetime`, for deterministic
            `captured_utc` values in tests.

    Returns:
        One snapshot dict (see module docstring) per market whose book was
        fetched and parsed successfully. A market whose event fetch,
        parsing, or orderbook fetch fails is skipped and counted in `stats`
        rather than raising -- a bad station or a single bad market must
        never abort the rest of the poll.
    """
    sess = session if session is not None else _get_session()
    stats = stats if stats is not None else {}
    stats.setdefault("captured", 0)
    stats.setdefault("skipped", 0)

    # Stamped once, before any request: every snapshot from this pass shares it,
    # so an event's legs group together even when the pass spans a second boundary.
    cycle_at = _utc_now_iso(clock)
    snapshots: list[dict] = []
    for station in stations:
        series = _STATION_TO_SERIES.get(station)
        if series is None:
            logger.warning("Unknown station %r; no series maps to it, skipping", station)
            stats["skipped"] += 1
            continue
        try:
            event_ticker = event_ticker_for_date(series, target_date)
            resp = _get(sess, f"{BASE_URL}/markets", params={"event_ticker": event_ticker})
            raw_markets = resp.json().get("markets", [])
        except (requests.RequestException, ValueError, KeyError) as exc:
            logger.warning("Failed to fetch markets for station=%s date=%s: %s", station, target_date, exc)
            stats["skipped"] += 1
            continue

        for raw in raw_markets:
            try:
                market = parse_market(raw, series)
            except (ValueError, KeyError) as exc:
                logger.warning("Skipping malformed market payload %r: %s", raw.get("ticker"), exc)
                stats["skipped"] += 1
                continue
            ticker = market["ticker"]
            try:
                ob_resp = _get(sess, f"{BASE_URL}/markets/{ticker}/orderbook")
                book_payload = ob_resp.json()
            except (requests.RequestException, ValueError) as exc:
                logger.warning("Skipping %s: orderbook fetch failed: %s", ticker, exc)
                stats["skipped"] += 1
                continue
            received_at = _utc_now_iso(clock)
            try:
                yes_bids, yes_asks = orderbook_to_ladders(book_payload)
            except (TypeError, KeyError, AttributeError) as exc:
                logger.warning("Skipping %s: malformed orderbook payload: %s", ticker, exc)
                stats["skipped"] += 1
                continue

            snapshots.append({
                # One value per poll cycle: consumers group an event's legs by
                # (event_ticker, captured_utc), and per-market stamps split a
                # single ladder across adjacent timestamps.
                "captured_utc": cycle_at,
                "received_utc": received_at,
                "source": "rest_poll",
                "event_ticker": market["event_ticker"],
                "ticker": ticker,
                "station": market["station"],
                "date_lst": market["date_lst"],
                "lower_bound_f": market["lower_bound_f"],
                "upper_bound_f": market["upper_bound_f"],
                "yes_bids": yes_bids,
                "yes_asks": yes_asks,
                "status": market["status"],
                "close_time": market["close_time"],
            })
            stats["captured"] += 1
    return snapshots


# --- Source seam --------------------------------------------------------------

@runtime_checkable
class BookSource(Protocol):
    """The interface `record` polls against.

    `poll_once` (above) is the only implementation today. A future
    websocket backend implements this same call shape -- same arguments,
    same snapshot-list return, `source` set to `"websocket"` in each
    snapshot -- so `record` and the ndjson writer never need to change. See
    the module docstring for exactly what a websocket backend would
    additionally need (credentials, reconnect/backoff, delta-to-ladder book
    reconstruction).
    """

    def __call__(
        self,
        stations: list[str],
        target_date: str | date,
        session: requests.Session | None = None,
        stats: dict[str, int] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> list[dict]:
        ...


# --- Recording loop -----------------------------------------------------------

MIN_INTERVAL_SECONDS = 5


@dataclass(frozen=True)
class RecordSummary:
    """Totals for one `record()` run."""

    polls: int
    captured: int
    skipped: int
    gaps: int


def record(
    stations: list[str],
    target_date: str | date,
    out_path: str | Path,
    interval_seconds: int,
    until: datetime,
    session: requests.Session | None = None,
    poll_fn: BookSource = poll_once,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    sleep_fn: Callable[[float], None] = time.sleep,
) -> RecordSummary:
    """Poll on a fixed interval until `until`, writing one JSON line per snapshot.

    Each poll's snapshots are appended and flushed immediately, so an
    interrupted run keeps every snapshot it managed to write. If an entire
    poll fails (as opposed to `poll_fn` skipping individual markets, which
    it already handles), a `{"source": "gap", ...}` line is written instead
    of leaving a silent hole in the series -- this project treats a silent
    gap as a correctness issue, not a cosmetic one (see
    `src.paper.schedule`'s coverage journal for the same principle applied
    to the paper-trading collector).

    Args:
        stations: Passed through to `poll_fn`.
        target_date: Passed through to `poll_fn`.
        out_path: Newline-delimited JSON output file; appended to, parent
            directories created if needed.
        interval_seconds: Minimum gap between poll starts. Must be `>= 5`
            (a polite floor against a public API); lower raises
            `ValueError` before anything runs.
        until: Aware `datetime`; the loop stops once `clock()` reaches it.
        session: Optional session, passed through to `poll_fn`.
        poll_fn: A `BookSource`-shaped callable; `poll_once` by default.
        clock: `() -> aware datetime`, injectable for tests.
        sleep_fn: `(seconds) -> None`, injectable for tests -- sleeps the
            *remainder* of each interval after accounting for how long the
            poll itself took, never a fixed amount on top of it.

    Returns:
        `RecordSummary` totals across the whole run.

    Raises:
        ValueError: If `interval_seconds < MIN_INTERVAL_SECONDS`.
    """
    if interval_seconds < MIN_INTERVAL_SECONDS:
        raise ValueError(
            f"interval_seconds must be >= {MIN_INTERVAL_SECONDS}s (polite minimum against a "
            f"public API); got {interval_seconds}"
        )
    if until.tzinfo is None:
        raise ValueError("until must be an aware datetime")

    sess = session if session is not None else _get_session()
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    polls = captured = skipped = gaps = 0
    with path.open("a") as fh:
        while clock() < until:
            loop_start = clock()
            polls += 1
            try:
                stats: dict[str, int] = {"captured": 0, "skipped": 0}
                snapshots = poll_fn(stations, target_date, session=sess, stats=stats, clock=clock)
            except Exception as exc:  # noqa: BLE001 -- a failed poll must never crash the run
                logger.warning("Poll failed, recording a gap marker: %s", exc)
                gap_line = {
                    "captured_utc": _utc_now_iso(clock),
                    "source": "gap",
                    "error": str(exc),
                }
                fh.write(json.dumps(gap_line) + "\n")
                fh.flush()
                gaps += 1
            else:
                for snapshot in snapshots:
                    fh.write(json.dumps(snapshot) + "\n")
                fh.flush()
                captured += stats["captured"]
                skipped += stats["skipped"]
                elapsed = (clock() - loop_start).total_seconds()
                logger.info(
                    "poll #%d: captured=%d skipped=%d elapsed=%.2fs",
                    polls, stats["captured"], stats["skipped"], elapsed,
                )

            now = clock()
            if now >= until:
                break
            elapsed = (now - loop_start).total_seconds()
            remaining = interval_seconds - elapsed
            if remaining > 0:
                sleep_fn(remaining)

    return RecordSummary(polls=polls, captured=captured, skipped=skipped, gaps=gaps)


# --- CLI ------------------------------------------------------------------

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--stations", required=True, help="Comma-separated station ids, e.g. KNYC,KMDW,KMIA,KAUS")
    parser.add_argument("--date", required=True, help="Local-standard event date, YYYY-MM-DD")
    parser.add_argument("--out", required=True, help="Output newline-delimited JSON path")
    parser.add_argument("--interval", type=int, default=15, help="Seconds between polls (>= 5)")
    parser.add_argument("--minutes", type=float, required=True, help="How many minutes to record for")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args(argv)
    stations = [s.strip() for s in args.stations.split(",") if s.strip()]
    target_date = date.fromisoformat(args.date)
    now = datetime.now(timezone.utc)
    until = now + timedelta(minutes=args.minutes)
    summary = record(stations, target_date, args.out, args.interval, until)
    logger.info(
        "done: polls=%d captured=%d skipped=%d gaps=%d -> %s",
        summary.polls, summary.captured, summary.skipped, summary.gaps, args.out,
    )


if __name__ == "__main__":
    main()
