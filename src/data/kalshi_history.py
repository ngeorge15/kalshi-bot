"""Historical Kalshi weather-market metadata and prices, public GET only.

Covers the KXHIGHNY / KXHIGHCHI / KXHIGHMIA / KXHIGHAUS daily-high-temperature
series (see `research/kalshi-public-data.md`). Everything here is an
unauthenticated ``GET`` against ``https://external-api.kalshi.com/trade-api/v2``
-- this module never imports `src.kalshi.auth`, never reads API credentials,
and never calls an order/portfolio endpoint. It exists purely to reconstruct
what a market looked like and traded at in the past, for backtesting.

**Live vs. historical partition.** Kalshi splits recent (~3 months) market
data across the regular endpoints (``/markets``, ``/series/.../candlesticks``)
and older data behind an ``/historical/...`` mirror with slightly different
field names. Critically, an out-of-window request to the *wrong* side does
not error -- it returns an empty result, indistinguishable at a glance from
"this event/candle genuinely has no data." This module never infers "no
data" from an empty response: which side to call is decided **before** the
request, from each market's own close time compared against
``GET /historical/cutoff`` (`get_historical_cutoff`), never from whether a
prior call came back empty. See `research/kalshi-public-data.md` §3.

**Bracket bounds.** `parse_market` turns each market's `strike_type` +
`floor_strike`/`cap_strike` into a continuous `[lower_bound_f, upper_bound_f)`
interval, confirmed against real payloads:
    - ``between`` floor=F cap=C  -> ``[F-0.5, C+0.5)``
    - ``less`` cap=C             -> ``(-inf, C-0.5)``
    - ``greater`` floor=F        -> ``[F+0.5, +inf)``

**Settlement source.** Kalshi switched weather settlement from the NWS CLI
report to "The Weather Company" underneath this series (see
`research/kalshi-public-data.md`'s orchestrator-verification note); each
market's `rules_primary` text names which one applied to it, so
`parse_market` records `settlement_source` per market rather than assuming
one project-wide truth source.

**Caching.** Anything from the historical (settled, immutable) side is
cached forever on disk via `src.data.cache`, under the gitignored `data/`
tree. Live-side responses (a market that may still be trading, or whose
result could still change) are never persisted to that long-lived cache.

Usage::

    from src.data.kalshi_history import fetch_event_at_decision
    from datetime import date

    rows = fetch_event_at_decision("KXHIGHNY", date(2025, 7, 16))
    for row in rows:
        print(row["ticker"], row["lower_bound_f"], row["upper_bound_f"], row["price"])
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from src.data.cache import cache_get, cache_set

logger = logging.getLogger(__name__)

# --- HTTP ---------------------------------------------------------------------

BASE_URL = "https://external-api.kalshi.com/trade-api/v2"
CONTACT_EMAIL = os.environ.get("NWS_CONTACT_EMAIL", "kalshi-bot@example.com")

REQUEST_TIMEOUT = (5, 30)  # (connect, read) seconds
SLEEP_SECONDS = 1.0  # politeness delay between real network calls

# Cutoff moves forward continuously but only needs to be re-checked
# occasionally during a single research session; not so long that a
# multi-day development session never notices it moving.
CUTOFF_CACHE_TTL_SECONDS = 6 * 3600

# Historical (settled) responses are immutable once fetched -- cache forever.
LONG_CACHE_TTL_SECONDS = 100 * 365 * 24 * 3600

_SESSION: requests.Session | None = None


def _get_session() -> requests.Session:
    """Return the module-level requests.Session (built once). GET-only, retries 429/5xx."""
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
        session.headers.update({"User-Agent": f"(kalshi-bot-research-history, {CONTACT_EMAIL})"})
        _SESSION = session
    return _SESSION


# --- Station / series mapping ---------------------------------------------------

# Confirmed against live `rules_primary` text in research/kalshi-public-data.md
# (station identifiers CLINYC/CLIMDW/CLIMIA/CLIAUS match one-for-one).
SERIES_STATIONS: dict[str, str] = {
    "KXHIGHNY": "KNYC",
    "KXHIGHCHI": "KMDW",
    "KXHIGHMIA": "KMIA",
    "KXHIGHAUS": "KAUS",
}

# Fixed local-standard-time UTC offsets (no DST). Duplicated (not imported)
# from src/paper/watchlist.py and src/data/weather/archive.py so this
# data-layer module keeps no dependency on src/paper.
STATION_STANDARD_UTC_OFFSET_HOURS: dict[str, int] = {
    "KNYC": -5,
    "KMDW": -6,
    "KMIA": -5,
    "KAUS": -6,
}

# A resolved Kalshi market reports status "finalized"; "settled" only ever
# appears as a query-filter value, never a payload value, but is accepted
# here too in case that changes. Duplicated (not imported) from
# src/paper/venue.py -- see that module's own comment for why.
KALSHI_SETTLED_STATUSES = frozenset({"finalized", "settled"})

_MONTH_ABBR = {
    1: "JAN", 2: "FEB", 3: "MAR", 4: "APR", 5: "MAY", 6: "JUN",
    7: "JUL", 8: "AUG", 9: "SEP", 10: "OCT", 11: "NOV", 12: "DEC",
}
_MONTH_NUM = {name: num for num, name in _MONTH_ABBR.items()}


def _as_date(value: str | date) -> date:
    return value if isinstance(value, date) else date.fromisoformat(value)


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _local_day_start(target: date, utc_offset_hours: int) -> datetime:
    """UTC instant at which `target`'s local-standard day (00:00 local) begins."""
    return datetime.combine(
        target, datetime.min.time(), timezone(timedelta(hours=utc_offset_hours))
    ).astimezone(timezone.utc)


def event_ticker_for_date(series: str, target_date: str | date) -> str:
    """Build the deterministic event ticker for `series` on `target_date`.

    Format confirmed live: ``{SERIES}-{YY}{MON}{DD}`` -- 2-digit year,
    3-letter caps month, 2-digit day, no separators, e.g. ``KXHIGHNY-26SEP16``.

    Raises:
        ValueError: If `series` is not one of `SERIES_STATIONS`.
    """
    if series not in SERIES_STATIONS:
        raise ValueError(f"Unknown series {series!r}; expected one of {sorted(SERIES_STATIONS)}")
    day = _as_date(target_date)
    return f"{series}-{day.strftime('%y')}{_MONTH_ABBR[day.month]}{day.day:02d}"


def _parse_event_ticker_date(event_ticker: str, series: str) -> date:
    """Recover the target local-standard date encoded in an event ticker."""
    prefix = f"{series}-"
    if not event_ticker.startswith(prefix):
        raise ValueError(f"Event ticker {event_ticker!r} does not start with {prefix!r}")
    suffix = event_ticker[len(prefix):]
    if len(suffix) != 7:
        raise ValueError(f"Event ticker {event_ticker!r} has an unrecognized date suffix {suffix!r}")
    yy, mon, dd = suffix[:2], suffix[2:5], suffix[5:]
    month = _MONTH_NUM.get(mon)
    if month is None:
        raise ValueError(f"Event ticker {event_ticker!r} has an unrecognized month abbreviation {mon!r}")
    return date(2000 + int(yy), month, int(dd))


def _station_for_series(series: str) -> str:
    station = SERIES_STATIONS.get(series)
    if station is None:
        raise ValueError(f"Unknown series {series!r}; expected one of {sorted(SERIES_STATIONS)}")
    return station


def decision_instant_utc(series: str, target_date: str | date) -> datetime:
    """T = target local-standard day start minus 1 minute, in UTC.

    This is the decision instant the backtest trades at: the last moment
    before the target day begins, so nothing about the target day itself
    (including its own settlement) can leak into the forecast or the quote
    used at T.
    """
    station = _station_for_series(series)
    offset = STATION_STANDARD_UTC_OFFSET_HOURS[station]
    return _local_day_start(_as_date(target_date), offset) - timedelta(minutes=1)


# --- Historical/live partition --------------------------------------------------

def get_historical_cutoff(session: requests.Session | None = None, use_cache: bool = True) -> dict[str, datetime]:
    """Fetch `GET /historical/cutoff`: the instant each dataset moved to the historical mirror.

    Returns:
        Dict with keys `market_settled_ts`, `trades_created_ts`,
        `orders_updated_ts`, `market_positions_last_updated_ts`, each an
        aware UTC `datetime`.
    """
    cache_params: dict[str, Any] = {}
    if use_cache:
        cached = cache_get("kalshi_history_cutoff", cache_params, ttl_seconds=CUTOFF_CACHE_TTL_SECONDS)
        if cached is not None:
            return {key: _parse_utc(value) for key, value in cached.items()}
    sess = session if session is not None else _get_session()
    resp = sess.get(f"{BASE_URL}/historical/cutoff", timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    payload = resp.json()
    cache_set("kalshi_history_cutoff", cache_params, payload)
    time.sleep(SLEEP_SECONDS)
    return {key: _parse_utc(value) for key, value in payload.items()}


def _use_historical(reference_time: datetime, cutoff: datetime) -> bool:
    """True if `reference_time` (e.g. a market's expected close) is in the historical partition.

    The decision is made from `reference_time` alone, never from whether a
    prior request came back empty -- an empty response from the wrong side
    of the partition looks identical to "no data exists" and must never be
    used to infer which side to call.
    """
    return reference_time <= cutoff


# --- 1. Market metadata -----------------------------------------------------

def fetch_event_markets(
    series: str,
    target_date: str | date,
    session: requests.Session | None = None,
    cutoff: dict[str, datetime] | None = None,
) -> list[dict]:
    """Fetch raw market payloads for one event (one series, one local-standard day).

    Chooses `/historical/markets` or `/markets` by comparing the event's
    *expected* close time (computed deterministically from the event ticker
    and the station's fixed UTC offset, not fetched) against
    `get_historical_cutoff()`'s `market_settled_ts` -- decided before the
    request, never inferred from an empty result.

    Args:
        series: One of `SERIES_STATIONS`.
        target_date: The local-standard target date.
        session: Optional session (tests).
        cutoff: Optional pre-fetched `get_historical_cutoff()` result, to
            avoid one extra call per event when fetching a whole range.

    Returns:
        Raw market dicts as returned by the API (unparsed); empty if the
        event has no markets (e.g. it never existed).
    """
    station = _station_for_series(series)
    offset = STATION_STANDARD_UTC_OFFSET_HOURS[station]
    day = _as_date(target_date)
    expected_close = _local_day_start(day + timedelta(days=1), offset)

    sess = session if session is not None else _get_session()
    cutoff_map = cutoff if cutoff is not None else get_historical_cutoff(sess)
    use_historical = _use_historical(expected_close, cutoff_map["market_settled_ts"])

    event_ticker = event_ticker_for_date(series, day)
    endpoint = f"{BASE_URL}/historical/markets" if use_historical else f"{BASE_URL}/markets"
    cache_params = {"event_ticker": event_ticker, "endpoint": "historical" if use_historical else "live"}

    if use_historical:
        cached = cache_get("kalshi_history_markets", cache_params, ttl_seconds=LONG_CACHE_TTL_SECONDS)
        if cached is not None:
            return cached

    resp = sess.get(endpoint, params={"event_ticker": event_ticker}, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    time.sleep(SLEEP_SECONDS)
    markets = resp.json().get("markets", [])

    if use_historical:
        cache_set("kalshi_history_markets", cache_params, markets)
    return markets


_STRIKE_TYPES = frozenset({"between", "less", "greater"})


def _bounds_from_strike(strike_type: str, floor_strike: object, cap_strike: object) -> tuple[float | None, float | None]:
    """Continuous `(lower_bound_f, upper_bound_f)` bounds for one bracket. See module docstring."""
    if strike_type not in _STRIKE_TYPES:
        raise ValueError(f"Unknown strike_type {strike_type!r}; expected one of {sorted(_STRIKE_TYPES)}")
    if strike_type == "between":
        if floor_strike is None or cap_strike is None:
            raise ValueError("'between' strike requires both floor_strike and cap_strike")
        return float(floor_strike) - 0.5, float(cap_strike) + 0.5
    if strike_type == "less":
        if cap_strike is None:
            raise ValueError("'less' strike requires cap_strike")
        return None, float(cap_strike) - 0.5
    # "greater"
    if floor_strike is None:
        raise ValueError("'greater' strike requires floor_strike")
    return float(floor_strike) + 0.5, None


def _settlement_source(rules_primary: object) -> str:
    """Classify a market's settlement source from its `rules_primary` text."""
    text = rules_primary if isinstance(rules_primary, str) else ""
    if "Weather Company" in text:
        return "weather_company"
    if "Climatological Report" in text or "National Weather Service" in text:
        return "nws_cli"
    return "unknown"


def parse_market(raw: dict, series: str) -> dict:
    """Normalize one raw market payload (from `fetch_event_markets`).

    Returns:
        Dict with `ticker`, `event_ticker`, `station`, `date_lst`,
        `lower_bound_f`, `upper_bound_f`, `result` (`'yes'`/`'no'`/`None`),
        `settlement_source` (`'nws_cli'`/`'weather_company'`/`'unknown'`),
        `close_time` (ISO 8601, verbatim from the API).

    Raises:
        ValueError: If `strike_type`/bound fields are missing or malformed.
    """
    event_ticker = raw["event_ticker"]
    station = _station_for_series(series)
    date_lst = _parse_event_ticker_date(event_ticker, series).isoformat()
    lower, upper = _bounds_from_strike(raw.get("strike_type"), raw.get("floor_strike"), raw.get("cap_strike"))
    result = raw.get("result")
    result = result if result in ("yes", "no") else None
    return {
        "ticker": raw["ticker"],
        "event_ticker": event_ticker,
        "station": station,
        "date_lst": date_lst,
        "lower_bound_f": lower,
        "upper_bound_f": upper,
        "result": result,
        "settlement_source": _settlement_source(raw.get("rules_primary")),
        "close_time": raw.get("close_time"),
        "status": raw.get("status"),
    }


# --- 2. Candlesticks / price at an instant --------------------------------------

def _dollars_to_cents(value: object) -> int | None:
    """Parse a Kalshi dollar-string ("0.0300") to integer cents, or None."""
    if value is None:
        return None
    try:
        return int((Decimal(str(value)) * 100).to_integral_value(rounding=ROUND_HALF_UP))
    except (ArithmeticError, ValueError):
        return None


def _to_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_candle(raw: dict, is_historical: bool) -> dict:
    """Normalize one raw candlestick (live or historical shape) to a common form.

    Live candlesticks suffix price sub-fields with `_dollars` and count
    fields with `_fp` (e.g. `yes_bid.close_dollars`, `volume_fp`);
    historical candlesticks drop both suffixes (`yes_bid.close`, `volume`).
    Confirmed against real payloads from both endpoints (see module tests).
    """
    price_suffix = "" if is_historical else "_dollars"
    count_suffix = "" if is_historical else "_fp"
    yes_bid = raw.get("yes_bid") or {}
    yes_ask = raw.get("yes_ask") or {}
    return {
        "end_period_ts": raw["end_period_ts"],
        "yes_bid_cents": _dollars_to_cents(yes_bid.get(f"close{price_suffix}")),
        "yes_ask_cents": _dollars_to_cents(yes_ask.get(f"close{price_suffix}")),
        "volume": _to_float(raw.get(f"volume{count_suffix}")),
        "open_interest": _to_float(raw.get(f"open_interest{count_suffix}")),
    }


def fetch_candlesticks(
    series: str,
    ticker: str,
    event_ticker: str,
    start_ts: int,
    end_ts: int,
    period_interval: int,
    session: requests.Session | None = None,
    cutoff: dict[str, datetime] | None = None,
) -> list[dict]:
    """Fetch and normalize candlesticks for one market over `[start_ts, end_ts]` (unix seconds).

    Chooses `/historical/markets/{ticker}/candlesticks` or
    `/series/{series}/markets/{ticker}/candlesticks` by the same
    close-time-vs-cutoff rule as `fetch_event_markets` (reusing
    `market_settled_ts` as a simplifying assumption -- candlesticks are
    keyed to the market itself, and in practice all four `/historical/cutoff`
    fields have been observed identical; this is a documented approximation,
    not a verified per-dataset cutoff for candlesticks specifically).

    Args:
        series: One of `SERIES_STATIONS`.
        ticker: The market ticker.
        event_ticker: That market's event ticker (used only to recover the
            target date for the endpoint-selection rule).
        start_ts, end_ts: Unix-second window, inclusive.
        period_interval: Minutes per candle; Kalshi accepts 1, 60, or 1440.
        session: Optional session (tests).
        cutoff: Optional pre-fetched `get_historical_cutoff()` result.

    Returns:
        List of `{end_period_ts, yes_bid_cents, yes_ask_cents, volume,
        open_interest}` dicts, ordered as returned by the API.
    """
    day = _parse_event_ticker_date(event_ticker, series)
    station = _station_for_series(series)
    offset = STATION_STANDARD_UTC_OFFSET_HOURS[station]
    expected_close = _local_day_start(day + timedelta(days=1), offset)

    sess = session if session is not None else _get_session()
    cutoff_map = cutoff if cutoff is not None else get_historical_cutoff(sess)
    use_historical = _use_historical(expected_close, cutoff_map["market_settled_ts"])

    url = (
        f"{BASE_URL}/historical/markets/{ticker}/candlesticks"
        if use_historical
        else f"{BASE_URL}/series/{series}/markets/{ticker}/candlesticks"
    )
    params = {"start_ts": start_ts, "end_ts": end_ts, "period_interval": period_interval}
    cache_params = {
        "ticker": ticker, "start_ts": start_ts, "end_ts": end_ts,
        "period_interval": period_interval, "endpoint": "historical" if use_historical else "live",
    }

    if use_historical:
        cached = cache_get("kalshi_history_candles", cache_params, ttl_seconds=LONG_CACHE_TTL_SECONDS)
        if cached is not None:
            return cached

    resp = sess.get(url, params=params, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    time.sleep(SLEEP_SECONDS)
    raw_candles = resp.json().get("candlesticks", [])
    normalized = [_normalize_candle(c, use_historical) for c in raw_candles]

    if use_historical:
        cache_set("kalshi_history_candles", cache_params, normalized)
    return normalized


# Beyond this many hours before T, a candle is too old to represent a live
# quote at T; treat the market as having no usable price rather than reusing
# a stale one.
DEFAULT_MAX_STALENESS_HOURS = 3.0


@dataclass(frozen=True)
class PriceAtInstant:
    """A market's quote as of some decision instant T, or why it is unusable.

    Attributes:
        status: `"ok"`, `"missing"` (no eligible candle, or bid/ask absent
            on the eligible one), or `"stale"` (the most recent eligible
            candle ended more than `max_staleness_hours` before T).
        yes_bid_cents, yes_ask_cents: The quote, only populated when
            `status == "ok"`.
        candle_end_utc: ISO 8601 end time of the candle used (or the most
            recent eligible one, for a `"stale"` result); `None` if no
            candle ended at or before T at all.
        age_hours: Hours between the candle's end and T; `None` if there was
            no eligible candle.
        volume, open_interest: Carried through from the candle, when used.
    """

    status: str
    yes_bid_cents: int | None = None
    yes_ask_cents: int | None = None
    candle_end_utc: str | None = None
    age_hours: float | None = None
    volume: float | None = None
    open_interest: float | None = None


def price_at_instant(
    candles: list[dict],
    target_time: datetime,
    max_staleness_hours: float = DEFAULT_MAX_STALENESS_HOURS,
) -> PriceAtInstant:
    """The most recent candle whose end is at or before `target_time` -- never a later one.

    Args:
        candles: Normalized candlesticks from `fetch_candlesticks`.
        target_time: The decision instant T (aware datetime).
        max_staleness_hours: A candle older than this before T is reported
            `"stale"` rather than silently reused.

    Returns:
        A `PriceAtInstant`. Never interpolates and never selects a candle
        whose `end_period_ts` is after `target_time`.
    """
    target_ts = target_time.timestamp()
    eligible = [c for c in candles if c["end_period_ts"] <= target_ts]
    if not eligible:
        return PriceAtInstant(status="missing")
    best = max(eligible, key=lambda c: c["end_period_ts"])
    age_hours = (target_ts - best["end_period_ts"]) / 3600.0
    candle_end_utc = _format_utc(datetime.fromtimestamp(best["end_period_ts"], tz=timezone.utc))
    if age_hours > max_staleness_hours:
        return PriceAtInstant(status="stale", candle_end_utc=candle_end_utc, age_hours=age_hours)
    if best["yes_bid_cents"] is None or best["yes_ask_cents"] is None:
        return PriceAtInstant(status="missing", candle_end_utc=candle_end_utc, age_hours=age_hours)
    return PriceAtInstant(
        status="ok",
        yes_bid_cents=best["yes_bid_cents"],
        yes_ask_cents=best["yes_ask_cents"],
        candle_end_utc=candle_end_utc,
        age_hours=age_hours,
        volume=best.get("volume"),
        open_interest=best.get("open_interest"),
    )


# --- 3. One event's markets, priced at the decision instant ---------------------

def fetch_event_at_decision(
    series: str,
    target_date: str | date,
    session: requests.Session | None = None,
    cutoff: dict[str, datetime] | None = None,
    period_interval: int = 60,
    lookback_hours: int = 24,
    max_staleness_hours: float = DEFAULT_MAX_STALENESS_HOURS,
) -> list[dict]:
    """Fetch one event's markets, each priced at T = target local-standard day start - 1 minute.

    Args:
        series: One of `SERIES_STATIONS`.
        target_date: The local-standard target date.
        session: Optional session (tests).
        cutoff: Optional pre-fetched `get_historical_cutoff()` result.
        period_interval: Candlestick granularity in minutes (1, 60, or 1440).
        lookback_hours: How far before T to request candles from, so a
            staleness check has data to work with.
        max_staleness_hours: Passed through to `price_at_instant`.

    Returns:
        List of dicts merging `parse_market`'s fields with `decision_time_utc`
        and `price` (a `PriceAtInstant`), one per market in the event. Empty
        if the event has no markets.
    """
    sess = session if session is not None else _get_session()
    cutoff_map = cutoff if cutoff is not None else get_historical_cutoff(sess)
    raw_markets = fetch_event_markets(series, target_date, sess, cutoff_map)
    decision_time = decision_instant_utc(series, target_date)
    start_ts = int((decision_time - timedelta(hours=lookback_hours)).timestamp())
    end_ts = int(decision_time.timestamp())

    rows = []
    for raw in raw_markets:
        market = parse_market(raw, series)
        candles = fetch_candlesticks(
            series, market["ticker"], market["event_ticker"], start_ts, end_ts,
            period_interval, sess, cutoff_map,
        )
        price = price_at_instant(candles, decision_time, max_staleness_hours)
        rows.append({**market, "decision_time_utc": _format_utc(decision_time), "price": price})
    return rows
