"""Generate paper-research watchlist entries directly from Kalshi's live market data.

Authoring a watchlist by hand (`src.paper.watchlist.scaffold`) requires copying a
ticker, a settlement-rules reference, and bracket bounds out of the Kalshi UI or
API response by eye -- slow, and every one of those three is exactly the kind of
field a transcription slip corrupts silently. `build_watchlist` instead reads
that data straight from `GET /markets?event_ticker=...` (via
`src.data.kalshi_history.fetch_event_markets`) for a real target date and
mechanically produces one entry per bracket: the market's actual ticker, its
bounds as parsed by `src.data.kalshi_history.parse_market` (never re-derived
from ticker text), and its settlement-source URL exactly as the payload states
it (falling back to the payload's `rules_primary` prose only when no URL is
present -- this module never invents a rules URL of its own).

**Eligibility stays a human judgement, on purpose.** Everything this module
reads -- a ticker, a bracket's bounds, a settlement-source URL -- is a fact
about the market that the API states outright. Whether that market is
*currently tradeable by this operator* is not: it depends on account access,
region restrictions, whether the book has any depth, and whether the exact
rules text has actually been read and accepted as fit for the strict weather
baseline -- none of which `fetch_event_markets` reports, and a market being
listed does not mean any of them hold. So `available` defaults to `False` and
`build_watchlist` refuses `available=True` unless the caller also supplies a
non-empty `eligibility_source` naming who checked and vouching for when. That
mirrors `scaffold`'s stance (see `src/paper/watchlist.py`) and this module
reuses its `validate()` pipeline as regression coverage: run against real
data, an `available=False` result carries only the same eligibility/placeholder
findings `scaffold` output would, and an `available=True` one validates clean.

Every network call here goes through `src.data.kalshi_history`, which is
public, unauthenticated, GET-only. This module makes real network calls when
invoked directly (see `watchlist-generate` in `src/paper/cli.py`); nothing in
`src/paper` does.
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta, timezone
import logging
import math

from src.data.kalshi_history import SERIES_STATIONS, fetch_event_markets, parse_market
from src.data.weather.station_map import STATIONS
from src.paper.watchlist import STATION_STANDARD_UTC_OFFSET_HOURS, bracket_coverage

logger = logging.getLogger(__name__)

# Reverse of SERIES_STATIONS: which series to query for a given station code.
STATION_SERIES: dict[str, str] = {station: series for series, station in SERIES_STATIONS.items()}

# How long a freshly-generated eligibility window lasts when the caller marks
# entries available. Arbitrary but conservative next to max_forecast_age_seconds
# (6h) in src/paper/config.py -- long enough to cover same-day review and use
# without silently outliving the human check it is meant to represent.
DEFAULT_ELIGIBILITY_HOURS = 24.0


def _format_utc(moment: datetime) -> str:
    """Format a timezone-aware datetime as ISO 8601 UTC with a 'Z' suffix."""
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _as_date_str(target_date: str | date) -> str:
    return target_date.isoformat() if isinstance(target_date, date) else target_date


def _sort_key(lower: float | None, upper: float | None) -> tuple[float, float]:
    """Sort key placing an open lower tail first and an open upper tail last."""
    return (-math.inf if lower is None else lower, math.inf if upper is None else upper)


def _rules_source(raw: dict) -> str:
    """The market's own settlement-source URL, or its rules prose if no URL is given.

    Never invents a URL: if the payload has neither a usable `settlement_sources`
    entry nor `rules_primary` text, this raises rather than fabricating one.

    Raises:
        ValueError: If the payload states no settlement-source URL or rules text.
    """
    sources = raw.get("settlement_sources")
    if isinstance(sources, list):
        for source in sources:
            url = source.get("url") if isinstance(source, dict) else None
            if isinstance(url, str) and url.strip():
                return url
    rules_primary = raw.get("rules_primary")
    if isinstance(rules_primary, str) and rules_primary.strip():
        return rules_primary
    raise ValueError(
        f"Market {raw.get('ticker')!r} payload has no settlement_sources URL or rules_primary text; "
        "refusing to invent a rules_source")


def build_watchlist(
    stations: Sequence[str],
    target_date: str | date,
    now: datetime,
    *,
    available: bool = False,
    eligibility_source: str | None = None,
    eligibility_hours: float = DEFAULT_ELIGIBILITY_HOURS,
    session=None,
    cutoff=None,
) -> list[dict]:
    """Fetch live Kalshi markets for `target_date` and build one entry per bracket.

    For each station, fetches that date's event via
    `src.data.kalshi_history.fetch_event_markets` and emits one watchlist entry
    per bracket returned, using the market's real ticker, its bounds and
    settlement source exactly as `parse_market`/the raw payload state them, and
    the station's coordinates from `src.data.weather.station_map.STATIONS`.
    Entries within an event are ordered by bracket lower bound (open lower tail
    first, open upper tail last).

    Args:
        stations: Station codes (e.g. `["KNYC", "KMDW"]`), case-insensitive.
            Must be keys of `src.data.kalshi_history.SERIES_STATIONS`.
        target_date: Local calendar date the brackets describe.
        now: Current UTC time, used only to timestamp the eligibility window.
            Must be timezone-aware. Never read from the system clock internally.
        available: If `True`, mark every generated entry's eligibility
            `available: true` with a window from `now` to
            `now + eligibility_hours`. Requires `eligibility_source`. If
            `False` (the default), entries start unavailable with an
            already-expired (zero-width) window, exactly like `scaffold` --
            a human must review and re-mark them before `observe` will act on
            them outside `research_only` mode.
        eligibility_source: Required, and only meaningful, when
            `available=True`: a human-readable description of who verified
            current tradeability and when (e.g. `"nikhi, manually confirmed
            tradeable in the Kalshi app, 2026-09-17 14:00 ET"`). This function
            has no way to check tradeability itself -- a market being listed
            by the API does not mean it is tradeable by this operator -- so it
            refuses to accept `available=True` without one.
        eligibility_hours: Length of the eligibility window in hours, only
            used when `available=True`.
        session, cutoff: Passed through to `fetch_event_markets` (tests only;
            production callers should omit both).

    Returns:
        One entry per bracket, across all requested stations, each shaped for
        `src.paper.watchlist.validate`.

    Raises:
        ValueError: If `now` is naive, `available=True` is passed without a
            non-empty `eligibility_source`, any station code is unrecognized,
            a station's event has no markets yet (the event may not be listed
            for that date; try the nearest listed future date), or a market's
            payload lacks bounds or a settlement-source reference entirely.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError(f"build_watchlist() requires a timezone-aware `now`; got a naive datetime {now!r}")
    if available and not (isinstance(eligibility_source, str) and eligibility_source.strip()):
        raise ValueError(
            "available=True requires a non-empty eligibility_source naming who verified current "
            "tradeability and when -- eligibility is a human judgement this function cannot make "
            "from market data alone")

    codes = [code.upper() for code in stations]
    if not codes:
        raise ValueError("stations must name at least one station code")
    unknown = sorted(set(codes) - set(STATION_SERIES))
    if unknown:
        raise ValueError(f"Unknown station code(s) {unknown}; expected one of {sorted(STATION_SERIES)}")

    date_str = _as_date_str(target_date)
    checked_at = _format_utc(now)
    expires_at = _format_utc(now + timedelta(hours=eligibility_hours)) if available else checked_at
    eligibility_note = eligibility_source if available else (
        "not yet reviewed by a human; generated by src.research.watchlist_gen.build_watchlist")

    entries: list[dict] = []
    for code in codes:
        series = STATION_SERIES[code]
        station = STATIONS[code]
        offset = STATION_STANDARD_UTC_OFFSET_HOURS[code]
        raw_markets = fetch_event_markets(series, date_str, session=session, cutoff=cutoff)
        if not raw_markets:
            raise ValueError(
                f"No live markets found for {series} on {date_str}; the event may not be listed yet -- "
                "try the nearest listed future date")

        event_key = f"{station['city']}-{date_str}"
        parsed = []
        for raw in raw_markets:
            market = parse_market(raw, series)
            rules_source = _rules_source(raw)
            parsed.append((market, rules_source))
        parsed.sort(key=lambda pair: _sort_key(pair[0]["lower_bound_f"], pair[0]["upper_bound_f"]))

        for market, rules_source in parsed:
            entries.append({
                "ticker": market["ticker"],
                "market_type": "temperature",
                "event_key": event_key,
                "latitude": station["lat"],
                "longitude": station["lon"],
                "rules_source": rules_source,
                "weather_spec": {
                    "date": market["date_lst"],
                    "utc_offset_hours": offset,
                    "lower_bound_f": market["lower_bound_f"],
                    "upper_bound_f": market["upper_bound_f"],
                },
                "eligibility": {
                    "available": bool(available),
                    "checked_at": checked_at,
                    "expires_at": expires_at,
                    "source": eligibility_note,
                },
            })
        logger.info("Generated %d bracket(s) for %s on %s from live Kalshi data.",
                    len(raw_markets), event_key, date_str)
    return entries


def coverage_summary(entries: list[dict]) -> dict[str, dict]:
    """Per-event bracket coverage for a generated watchlist; see `bracket_coverage`.

    Kalshi's own bracket ladder is contiguous with open tails on both ends, so
    a healthy generated watchlist should always report `partitions: True` for
    every event -- a `False` here on freshly generated (not hand-edited) data
    would mean either a station's event returned an incomplete bracket set or
    `build_watchlist` mis-sorted/mis-parsed something, either way worth
    investigating before trusting the file.
    """
    return bracket_coverage(entries)
