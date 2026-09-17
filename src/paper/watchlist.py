"""Author, validate, and inspect paper-research weather watchlists.

A watchlist is a JSON list of entries, each pairing a Kalshi market with the
weather forecast inputs `src/paper/observe.py` needs to fetch a quote and run
the strict baseline in `src/paper/weather.py`. Authoring one by hand is
fiddly and the failure modes are obscure -- an already-started target date,
for example, currently fails deep inside `predict_hourly_high` with a message
that never names the actual mistake. This module exists to make authoring
fast (`scaffold`) and to fail loudly and early instead (`validate`), plus two
review aids (`bracket_coverage` and `explain`).

None of these functions decide whether a market is *tradeable* or whether the
resulting probabilities mean anything. `scaffold` never guesses at ticker
text, contract-rules sources, or bracket bounds -- those are the reviewed,
human-owned parts of an entry, and it never marks one available. `validate`
checks structure and internal consistency, not permission to trade or the
truth of any prediction. Nothing here makes a network call.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import logging
import math
import re

from src.data.weather.station_map import STATIONS

logger = logging.getLogger(__name__)


# --- Structural constants ---------------------------------------------------

REQUIRED_ENTRY_KEYS = ("ticker", "market_type", "event_key", "latitude", "longitude",
                       "rules_source", "weather_spec", "eligibility")
REQUIRED_ELIGIBILITY_KEYS = ("available", "checked_at", "expires_at", "source")

# weather_spec only describes temperature brackets; observe_once only knows
# how to act on this one market_type.
SUPPORTED_MARKET_TYPES = ("temperature",)

# Mirrors the offset bound enforced deep inside predict_hourly_high, duplicated
# here so validate() catches the mistake before that function ever runs.
MIN_UTC_OFFSET_HOURS = -12
MAX_UTC_OFFSET_HOURS = 14

MIN_LATITUDE, MAX_LATITUDE = -90.0, 90.0
MIN_LONGITUDE, MAX_LONGITUDE = -180.0, 180.0

# STATIONS coordinates are recorded to 4 decimal places (~11 m). A hundredth
# of a degree (~1 km at the equator) comfortably separates "same station,
# rounded differently" from "this is actually a different place."
COORDINATE_AGREEMENT_TOLERANCE_DEGREES = 0.01

# Marks a value scaffold() could not responsibly fill in; validate() flags
# every occurrence of this marker regardless of which field it appears in.
PLACEHOLDER_MARKER = "REPLACE"
TICKER_PLACEHOLDER_PREFIX = f"{PLACEHOLDER_MARKER}-WITH-REVIEWED-TICKER"
RULES_SOURCE_PLACEHOLDER = f"{PLACEHOLDER_MARKER}-WITH-REVIEWED-CONTRACT-RULES-URL"
ELIGIBILITY_SOURCE_PLACEHOLDER = f"{PLACEHOLDER_MARKER}-WITH-CURRENT-AVAILABILITY-REVIEW"

# Standard-time (non-DST) UTC offsets for the four mapped stations' local
# civil time. predict_hourly_high requires a fixed "local-standard" offset,
# not a DST-adjusted one, so these do not change across the year. Scaffold
# uses these as a default; pass utc_offset_hours explicitly to override.
STATION_STANDARD_UTC_OFFSET_HOURS: dict[str, int] = {
    "KNYC": -5,  # America/New_York standard time (EST)
    "KMDW": -6,  # America/Chicago standard time (CST)
    "KMIA": -5,  # America/New_York standard time (EST)
    "KAUS": -6,  # America/Chicago standard time (CST)
}

_TRAILING_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})$")

ERROR = "error"
WARNING = "warning"


@dataclass(frozen=True)
class Finding:
    """One validation result.

    Attributes:
        index: Position of the offending entry in the watchlist, or ``None``
            for a whole-watchlist problem (e.g. the watchlist is not a list).
        field: Dotted path to the offending field (e.g. ``"weather_spec.date"``).
        severity: ``"error"`` (structurally wrong, or will crash or misbehave
            downstream) or ``"warning"`` (legitimate but needs a human look).
        message: Specific, actionable description of the problem.
    """
    index: int | None
    field: str
    severity: str
    message: str


@dataclass(frozen=True)
class ValidationResult:
    """The outcome of validating a watchlist.

    `ok` being True means the watchlist is structurally sound and internally
    consistent -- not that any market is tradeable or that its predictions
    will be meaningful.
    """
    findings: list[Finding]

    @property
    def errors(self) -> list[Finding]:
        """Findings severe enough to block use."""
        return [f for f in self.findings if f.severity == ERROR]

    @property
    def warnings(self) -> list[Finding]:
        """Findings that are legitimate but merit a human look."""
        return [f for f in self.findings if f.severity == WARNING]

    @property
    def ok(self) -> bool:
        """True if there are no errors (warnings do not block use)."""
        return not self.errors


# --- Shared parsing/checking helpers ----------------------------------------

def _bound_problems(lower: object, upper: object) -> list[str]:
    """Return human-readable problems with a bracket's bounds, if any."""
    problems = []
    for name, bound in (("lower_bound_f", lower), ("upper_bound_f", upper)):
        if bound is not None and (isinstance(bound, bool) or not isinstance(bound, (int, float))
                                   or not math.isfinite(bound)):
            problems.append(f"{name} must be a finite number or null, got {bound!r}")
    if problems:
        return problems
    if lower is None and upper is None:
        problems.append("both lower_bound_f and upper_bound_f are null; specify at least one bound")
    elif lower is not None and upper is not None and lower >= upper:
        problems.append(f"lower_bound_f ({lower}) must be strictly less than upper_bound_f ({upper})")
    return problems


def _require_aware(now: datetime, caller: str) -> None:
    """Raise ValueError if `now` is not a timezone-aware datetime."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError(f"{caller}() requires a timezone-aware `now` (e.g. datetime.now(timezone.utc)); "
                         f"got a naive datetime {now!r}")


def _offset_problem(offset: object) -> str | None:
    """Return a problem message if `offset` is not a valid UTC offset in hours."""
    if type(offset) is not int or not MIN_UTC_OFFSET_HOURS <= offset <= MAX_UTC_OFFSET_HOURS:
        return (f"utc_offset_hours must be an int between {MIN_UTC_OFFSET_HOURS} and "
                f"{MAX_UTC_OFFSET_HOURS}, got {offset!r}")
    return None


def _parse_date(value: object) -> date | None:
    """Parse an ISO 8601 date string, returning None (never raising) on failure."""
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _parse_utc_timestamp(value: object) -> datetime | None:
    """Parse a timezone-aware ISO 8601 timestamp, returning None on failure."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _format_utc(moment: datetime) -> str:
    """Format a timezone-aware datetime as ISO 8601 UTC with a 'Z' suffix."""
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def local_day_start(target: date, utc_offset_hours: int) -> datetime:
    """Return the UTC instant a local-standard calendar day begins.

    Args:
        target: The local calendar date.
        utc_offset_hours: Fixed local-standard UTC offset, in hours.

    Returns:
        The UTC datetime at which `target`'s local day (00:00 local) begins.
    """
    return datetime.combine(target, time(), timezone(timedelta(hours=utc_offset_hours))).astimezone(timezone.utc)


def _find_placeholders(value: object, path: str) -> list[str]:
    """Recursively collect dotted field paths whose string value contains PLACEHOLDER_MARKER."""
    found = []
    if isinstance(value, str):
        if PLACEHOLDER_MARKER in value:
            found.append(path)
    elif isinstance(value, dict):
        for key, sub in value.items():
            found.extend(_find_placeholders(sub, f"{path}.{key}" if path else str(key)))
    elif isinstance(value, list):
        for i, sub in enumerate(value):
            found.extend(_find_placeholders(sub, f"{path}[{i}]"))
    return found


# --- scaffold ----------------------------------------------------------------

def scaffold(station_code: str, target_date: str, brackets: Sequence[tuple[float | None, float | None]],
             now: datetime, utc_offset_hours: int | None = None) -> list[dict]:
    """Generate placeholder watchlist entries for one station and target date.

    Pre-fills everything derivable from the station map and the caller's
    inputs: coordinates, a consistent `event_key`, the `weather_spec`
    skeleton, and an `eligibility` block. It never invents a ticker, a
    contract-rules source, or bracket bounds, and it never marks an entry
    available -- those require a human to read the actual settlement rules
    and check the market is currently tradeable. Run `validate()` on the
    result before editing further; it reports exactly what still needs
    human input.

    Args:
        station_code: Key into `src.data.weather.station_map.STATIONS`
            (e.g. ``"KNYC"``). Case-insensitive.
        target_date: Local calendar date the brackets describe, as an ISO
            8601 date string (e.g. ``"2026-09-05"``).
        brackets: One `(lower_bound_f, upper_bound_f)` pair per bracket, in
            Fahrenheit. Either bound may be `None` for an open tail, but not
            both. These must come from reviewed contract rules -- this
            function does not infer them from ticker text or titles.
        now: Current UTC time, used only to timestamp the placeholder
            eligibility window. Must be timezone-aware. Never read from the
            system clock internally.
        utc_offset_hours: Fixed local-standard UTC offset for the station.
            Defaults to `STATION_STANDARD_UTC_OFFSET_HOURS[station_code]`
            when omitted; pass explicitly to override.

    Returns:
        One placeholder entry per bracket, in the order given. Every entry
        has ``eligibility.available is False``.

    Raises:
        ValueError: If `station_code` is unrecognized, `target_date` is not
            a valid ISO date, `brackets` is empty or contains a bracket with
            non-finite, disordered, or fully-null bounds, the resolved
            `utc_offset_hours` is out of range, or `now` is naive (lacks
            timezone info).
    """
    _require_aware(now, "scaffold")
    code = station_code.upper()
    station = STATIONS.get(code)
    if station is None:
        raise ValueError(f"Unknown station_code {station_code!r}; expected one of {sorted(STATIONS)}")
    target = _parse_date(target_date)
    if target is None:
        raise ValueError(f"target_date must be an ISO 8601 date (YYYY-MM-DD), got {target_date!r}")
    if not brackets:
        raise ValueError("brackets must contain at least one (lower_bound_f, upper_bound_f) pair")
    offset = STATION_STANDARD_UTC_OFFSET_HOURS[code] if utc_offset_hours is None else utc_offset_hours
    offset_problem = _offset_problem(offset)
    if offset_problem:
        raise ValueError(offset_problem)

    event_key = f"{station['city']}-{target.isoformat()}"
    checked_at = _format_utc(now)
    entries = []
    for index, pair in enumerate(brackets):
        lower, upper = pair
        problems = _bound_problems(lower, upper)
        if problems:
            raise ValueError(f"bracket {index} ({lower!r}, {upper!r}): {'; '.join(problems)}")
        entries.append({
            "ticker": f"{TICKER_PLACEHOLDER_PREFIX}-{code}-{target.isoformat()}-{index}",
            "market_type": "temperature",
            "event_key": event_key,
            "latitude": station["lat"],
            "longitude": station["lon"],
            "rules_source": RULES_SOURCE_PLACEHOLDER,
            "weather_spec": {
                "date": target.isoformat(),
                "utc_offset_hours": offset,
                "lower_bound_f": lower,
                "upper_bound_f": upper,
            },
            "eligibility": {
                # Scaffolding cannot know whether a market is tradeable;
                # that is a human judgement. Never set True here.
                "available": False,
                "checked_at": checked_at,
                # Zero-width window: reads as already expired at any `now`
                # at or after scaffold time, forcing an explicit human
                # review before the entry is treated as eligible.
                "expires_at": checked_at,
                "source": ELIGIBILITY_SOURCE_PLACEHOLDER,
            },
        })
    logger.info("Scaffolded %d bracket(s) for %s on %s; all require human review before use.",
                len(entries), event_key, target.isoformat())
    return entries


# --- validate ----------------------------------------------------------------

def validate(watchlist: object, now: datetime) -> ValidationResult:
    """Structurally and semantically validate a watchlist.

    Checks structure and internal consistency only -- not whether any market
    is tradeable, whether `eligibility.available` should be true, or whether
    a prediction built from an entry would be any good.

    This is an authoring-time check: it errors on a target day that has
    already started because that is the classic authoring mistake, not
    because such an entry is unusable. An entry whose target day has since
    started legitimately remains in a *live* watchlist so `observe` can keep
    collecting its settlement -- do not call `validate()` to decide whether
    `observe` should still process an entry.

    Args:
        watchlist: The parsed watchlist (expected to be a JSON list of entry
            dicts).
        now: Current UTC time, used to check that target dates have not
            already started and that eligibility windows have not expired.
            Must be timezone-aware. Never read from the system clock
            internally.

    Returns:
        A `ValidationResult` with one `Finding` per problem found.

    Raises:
        ValueError: If `now` is naive (lacks timezone info).
    """
    _require_aware(now, "validate")
    if not isinstance(watchlist, list):
        return ValidationResult([Finding(None, "<watchlist>", ERROR,
            f"Watchlist must be a JSON list of entries, got {type(watchlist).__name__}")])

    findings: list[Finding] = []
    tickers: dict[str, list[int]] = defaultdict(list)
    for index, entry in enumerate(watchlist):
        if not isinstance(entry, dict):
            findings.append(Finding(index, "<entry>", ERROR,
                f"Entry must be a JSON object, got {type(entry).__name__}"))
            continue
        findings.extend(_validate_entry(index, entry, now))
        ticker = entry.get("ticker")
        if isinstance(ticker, str) and ticker.strip():
            tickers[ticker].append(index)

    for ticker, indices in tickers.items():
        if len(indices) > 1:
            for index in indices:
                others = [i for i in indices if i != index]
                findings.append(Finding(index, "ticker", ERROR,
                    f"Duplicate ticker {ticker!r} also used at index(es) {others}"))
    return ValidationResult(findings)


def _validate_entry(index: int, entry: dict, now: datetime) -> list[Finding]:
    """Validate one entry, returning its findings."""
    findings: list[Finding] = []

    def err(field_name: str, message: str) -> None:
        findings.append(Finding(index, field_name, ERROR, message))

    def warn(field_name: str, message: str) -> None:
        findings.append(Finding(index, field_name, WARNING, message))

    for key in REQUIRED_ENTRY_KEYS:
        if key not in entry:
            err(key, f"missing required key {key!r}")
    for path in _find_placeholders(entry, ""):
        err(path, f"placeholder value not replaced with a reviewed value (contains {PLACEHOLDER_MARKER!r})")

    if "ticker" in entry:
        ticker = entry.get("ticker")
        if not isinstance(ticker, str) or not ticker.strip():
            err("ticker", "ticker must be a nonempty string")

    if "market_type" in entry:
        market_type = entry.get("market_type")
        if not isinstance(market_type, str) or not market_type.strip():
            err("market_type", "market_type must be a nonempty string")
        elif market_type not in SUPPORTED_MARKET_TYPES:
            err("market_type", f"market_type {market_type!r} is not supported; expected one of "
                               f"{SUPPORTED_MARKET_TYPES} (weather_spec only describes temperature brackets)")

    event_key = entry.get("event_key")
    if "event_key" in entry and (not isinstance(event_key, str) or not event_key.strip()):
        err("event_key", "event_key must be a nonempty string")

    lat, lon = entry.get("latitude"), entry.get("longitude")
    lat_valid = ("latitude" in entry and not isinstance(lat, bool) and isinstance(lat, (int, float))
                 and math.isfinite(lat) and MIN_LATITUDE <= lat <= MAX_LATITUDE)
    lon_valid = ("longitude" in entry and not isinstance(lon, bool) and isinstance(lon, (int, float))
                 and math.isfinite(lon) and MIN_LONGITUDE <= lon <= MAX_LONGITUDE)
    if "latitude" in entry and not lat_valid:
        err("latitude", f"latitude must be a finite number in [{MIN_LATITUDE}, {MAX_LATITUDE}], got {lat!r}")
    if "longitude" in entry and not lon_valid:
        err("longitude", f"longitude must be a finite number in [{MIN_LONGITUDE}, {MAX_LONGITUDE}], got {lon!r}")

    if lat_valid and lon_valid and isinstance(event_key, str) and event_key.strip():
        city = event_key.split("-", 1)[0].strip().upper()
        known = next((s for s in STATIONS.values() if s["city"].upper() == city), None)
        if known is not None and (abs(lat - known["lat"]) > COORDINATE_AGREEMENT_TOLERANCE_DEGREES
                                   or abs(lon - known["lon"]) > COORDINATE_AGREEMENT_TOLERANCE_DEGREES):
            warn("latitude", f"({lat}, {lon}) disagrees with the known {city} station "
                             f"({known['lat']}, {known['lon']}) by more than "
                             f"{COORDINATE_AGREEMENT_TOLERANCE_DEGREES} deg; fine for a deliberately custom "
                             "location, otherwise double-check the coordinates")

    if "rules_source" in entry:
        rules_source = entry.get("rules_source")
        if not isinstance(rules_source, str) or not rules_source.strip():
            err("rules_source", "rules_source must be a nonempty string")

    if "weather_spec" in entry:
        spec = entry.get("weather_spec")
        if not isinstance(spec, dict):
            err("weather_spec", "weather_spec must be a JSON object")
        else:
            findings.extend(_validate_weather_spec(index, spec, event_key, now))

    if "eligibility" in entry:
        eligibility = entry.get("eligibility")
        if not isinstance(eligibility, dict):
            err("eligibility", "eligibility must be a JSON object")
        else:
            findings.extend(_validate_eligibility(index, eligibility, now))

    return findings


def _validate_weather_spec(index: int, spec: dict, event_key: object, now: datetime) -> list[Finding]:
    """Validate the `weather_spec` block of one entry, returning its findings."""
    findings: list[Finding] = []

    def err(field_name: str, message: str) -> None:
        findings.append(Finding(index, f"weather_spec.{field_name}", ERROR, message))

    date_value = spec.get("date")
    target = _parse_date(date_value) if "date" in spec else None
    if "date" not in spec:
        err("date", "missing required key 'date'")
    elif target is None:
        err("date", f"date must be an ISO 8601 date (YYYY-MM-DD), got {date_value!r}")

    offset = spec.get("utc_offset_hours")
    if "utc_offset_hours" not in spec:
        offset_problem = "missing required key 'utc_offset_hours'"
        err("utc_offset_hours", offset_problem)
    else:
        offset_problem = _offset_problem(offset)
        if offset_problem:
            err("utc_offset_hours", offset_problem)

    lower, upper = spec.get("lower_bound_f"), spec.get("upper_bound_f")
    for problem in _bound_problems(lower, upper):
        err("lower_bound_f/upper_bound_f", problem)

    # This is the single most common authoring mistake: a target date whose
    # local day has already started. Left uncaught, it fails deep inside
    # predict_hourly_high with a message that never names the real problem.
    if target is not None and not offset_problem:
        start = local_day_start(target, offset)
        if now >= start:
            err("date", f"target local day {target.isoformat()} (starts {_format_utc(start)} UTC) has "
                        f"already started as of {_format_utc(now)}; predict_hourly_high requires a forecast "
                        "made before the target day starts -- pick a future date")

    if target is not None and isinstance(event_key, str) and event_key.strip():
        match = _TRAILING_DATE.search(event_key)
        if match and match.group(1) != target.isoformat():
            err("date", f"event_key {event_key!r} encodes date {match.group(1)} but weather_spec.date is "
                        f"{target.isoformat()}; these must agree")

    return findings


def _validate_eligibility(index: int, eligibility: dict, now: datetime) -> list[Finding]:
    """Validate the `eligibility` block of one entry, returning its findings."""
    findings: list[Finding] = []

    def err(field_name: str, message: str) -> None:
        findings.append(Finding(index, f"eligibility.{field_name}", ERROR, message))

    def warn(field_name: str, message: str) -> None:
        findings.append(Finding(index, f"eligibility.{field_name}", WARNING, message))

    for key in REQUIRED_ELIGIBILITY_KEYS:
        if key not in eligibility:
            err(key, f"missing required key {key!r}")

    if "available" in eligibility and not isinstance(eligibility.get("available"), bool):
        err("available", f"available must be true or false, got {eligibility.get('available')!r}")

    if "source" in eligibility:
        source = eligibility.get("source")
        if not isinstance(source, str) or not source.strip():
            err("source", "source must be a nonempty string")

    checked_at = None
    if "checked_at" in eligibility:
        checked_at = _parse_utc_timestamp(eligibility.get("checked_at"))
        if checked_at is None:
            err("checked_at", f"checked_at must be a timezone-aware ISO 8601 timestamp, "
                              f"got {eligibility.get('checked_at')!r}")

    expires_at = None
    if "expires_at" in eligibility:
        expires_at = _parse_utc_timestamp(eligibility.get("expires_at"))
        if expires_at is None:
            err("expires_at", f"expires_at must be a timezone-aware ISO 8601 timestamp, "
                              f"got {eligibility.get('expires_at')!r}")

    if checked_at is not None and expires_at is not None:
        if checked_at > expires_at:
            err("checked_at", f"checked_at ({_format_utc(checked_at)}) is after "
                              f"expires_at ({_format_utc(expires_at)})")
        elif expires_at <= now:
            warn("expires_at", f"eligibility window expired at {_format_utc(expires_at)}, at or before now "
                               f"({_format_utc(now)}); re-verify availability before treating this as eligible")

    return findings


# --- bracket_coverage ---------------------------------------------------------

@dataclass(frozen=True)
class _Bracket:
    index: int
    ticker: object
    lower: float | None
    upper: float | None


def _effective_bounds(bracket: _Bracket) -> tuple[float, float]:
    """Return (lower, upper) with a null lower/upper mapped to -inf/+inf."""
    lower = -math.inf if bracket.lower is None else bracket.lower
    upper = math.inf if bracket.upper is None else bracket.upper
    return lower, upper


def _json_bound(value: float) -> float | None:
    """Map +/-inf to None so gap/overlap bounds stay JSON-safe (allow_nan=False)."""
    return None if math.isinf(value) else value


def _sort_key(bracket: _Bracket) -> tuple[float, float]:
    return _effective_bounds(bracket)


def bracket_coverage(entries: list[dict]) -> dict[str, dict]:
    """Report, per `event_key`, whether the entries' brackets partition the real line.

    Sweeps each event's brackets in order of `lower_bound_f` (a null lower
    bound sorts first as -inf; a null upper bound is treated as +inf and does
    not necessarily sort last), tracking the furthest point any bracket seen
    so far reaches. A gap is a stretch the next bracket starts past that
    point; an overlap is a stretch it starts before that point. Because
    brackets are half-open [lower, upper), bounds that merely touch are
    neither. This is a running-coverage sweep rather than a pairwise check of
    sorted neighbours, so it also catches a bracket overlapping or covering
    one that is not adjacent to it once sorted. If the brackets do not
    partition the line, the per-event probabilities they imply cannot be made
    to sum to 1, and any paired comparison against market prices is measuring
    something undefined.

    Entries that are not dicts, or are missing a usable `event_key` or
    `weather_spec` bounds, are silently skipped -- call `validate()` first to
    have those reported as findings.

    Args:
        entries: Watchlist entries (or any dicts shaped like them).

    Returns:
        A dict keyed by `event_key`, each value a dict with `n_brackets`,
        `gaps` (list of `{"from_f", "to_f"}` dicts describing an uncovered
        interval; an infinite bound is reported as `None`), `overlaps` (list
        of `{"tickers", "from_f", "to_f"}` dicts describing a double-covered
        interval, `tickers` a list of the (at least two) tickers responsible),
        `has_open_lower_tail` (true if any bracket in the event has a null
        lower bound), `has_open_upper_tail` (true if any has a null upper
        bound), and `partitions` (True only when there are no gaps or
        overlaps and both tails are open).
    """
    by_event: dict[str, list[_Bracket]] = defaultdict(list)
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        event_key = entry.get("event_key")
        spec = entry.get("weather_spec")
        if not isinstance(event_key, str) or not event_key.strip() or not isinstance(spec, dict):
            continue
        lower, upper = spec.get("lower_bound_f"), spec.get("upper_bound_f")
        if _bound_problems(lower, upper):
            continue
        by_event[event_key].append(_Bracket(index, entry.get("ticker"), lower, upper))

    report = {}
    for event_key, brackets in by_event.items():
        ordered = sorted(brackets, key=_sort_key)
        gaps, overlaps = [], []
        covered_upper = None
        covered_ticker = None
        for bracket in ordered:
            lo, hi = _effective_bounds(bracket)
            if covered_upper is None:
                covered_upper, covered_ticker = hi, bracket.ticker
                continue
            if lo > covered_upper:
                gaps.append({"from_f": _json_bound(covered_upper), "to_f": _json_bound(lo)})
                covered_upper, covered_ticker = hi, bracket.ticker
            elif lo < covered_upper:
                overlaps.append({"tickers": [covered_ticker, bracket.ticker],
                                  "from_f": _json_bound(lo), "to_f": _json_bound(min(covered_upper, hi))})
                if hi > covered_upper:
                    covered_upper, covered_ticker = hi, bracket.ticker
            elif hi > covered_upper:
                covered_upper, covered_ticker = hi, bracket.ticker
        has_open_lower_tail = any(b.lower is None for b in ordered)
        has_open_upper_tail = any(b.upper is None for b in ordered)
        report[event_key] = {
            "n_brackets": len(ordered),
            "gaps": gaps,
            "overlaps": overlaps,
            "has_open_lower_tail": has_open_lower_tail,
            "has_open_upper_tail": has_open_upper_tail,
            "partitions": not gaps and not overlaps and has_open_lower_tail and has_open_upper_tail,
        }
    return report


# --- explain -------------------------------------------------------------

def _sort_key_of(entry: object) -> float:
    spec = entry.get("weather_spec") if isinstance(entry, dict) else None
    lower = spec.get("lower_bound_f") if isinstance(spec, dict) else None
    return lower if isinstance(lower, (int, float)) and not isinstance(lower, bool) else -math.inf


def _fmt_bound(bound: object) -> str:
    return "-inf" if bound is None else str(bound)


def explain(watchlist: list[dict]) -> str:
    """Render a short human-readable summary for a reviewer to eyeball.

    Lists, per event, the brackets in sorted order, the coverage verdict, and
    which fields still need human input (placeholders, or an entry not yet
    marked available). This does not check target dates against a clock and
    makes no claim about tradeability -- pair it with `validate()` before
    actually running the watchlist.

    Args:
        watchlist: The parsed watchlist.

    Returns:
        A multi-line human-readable report string.
    """
    if not isinstance(watchlist, list) or not watchlist:
        return "Watchlist is empty or not a list; nothing to explain."

    coverage = bracket_coverage(watchlist)
    by_event: dict[str, list[int]] = defaultdict(list)
    for index, entry in enumerate(watchlist):
        key = entry.get("event_key") if isinstance(entry, dict) else None
        by_event[key if isinstance(key, str) and key.strip() else f"<entry {index}: no event_key>"].append(index)

    entry_word = "entry" if len(watchlist) == 1 else "entries"
    lines = [f"{len(watchlist)} {entry_word} across {len(by_event)} event(s):", ""]
    for event_key in sorted(by_event):
        indices = by_event[event_key]
        lines.append(f"{event_key} ({len(indices)} bracket(s)):")
        for i in sorted(indices, key=lambda i: _sort_key_of(watchlist[i])):
            entry = watchlist[i]
            if not isinstance(entry, dict):
                lines.append(f"  [{i}] not a JSON object")
                continue
            spec = entry.get("weather_spec")
            lower = spec.get("lower_bound_f") if isinstance(spec, dict) else None
            upper = spec.get("upper_bound_f") if isinstance(spec, dict) else None
            ticker = entry.get("ticker", "<missing ticker>")
            lines.append(f"  [{i}] [{_fmt_bound(lower)}, {_fmt_bound(upper)})  ticker={ticker}")
            needs = sorted(_find_placeholders(entry, ""))
            eligibility = entry.get("eligibility")
            if isinstance(eligibility, dict) and eligibility.get("available") is not True:
                needs.append("eligibility (not marked available)")
            if needs:
                lines.append(f"      needs human input: {', '.join(needs)}")
        summary = coverage.get(event_key)
        if summary is None:
            lines.append("  coverage: could not be determined (missing or invalid bracket bounds)")
        elif summary["partitions"]:
            lines.append("  coverage: brackets partition the real line cleanly")
        else:
            problems = []
            if summary["gaps"]:
                problems.append(f"{len(summary['gaps'])} gap(s)")
            if summary["overlaps"]:
                problems.append(f"{len(summary['overlaps'])} overlap(s)")
            if not summary["has_open_lower_tail"]:
                problems.append("lower tail not open")
            if not summary["has_open_upper_tail"]:
                problems.append("upper tail not open")
            lines.append(f"  coverage: DOES NOT partition the real line ({'; '.join(problems)})")
        lines.append("")
    return "\n".join(lines).rstrip()
