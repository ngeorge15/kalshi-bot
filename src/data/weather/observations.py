"""Intraday **observed** temperature from the Iowa Environmental Mesonet (IEM) ASOS network.

This is the running-high "ratchet" feed: within a local-standard day, the
maximum temperature observed *so far* is a hard lower bound on that day's
eventual CLI daily high (see ``src/data/weather/archive.py`` for the
finalized CLI truth this feed is a same-day preview of). It is intended for
same-day decision code that needs "how hot has it gotten yet", not for
backtesting against the finalized day (use ``archive.fetch_cli_daily_highs``
for that).

**Endpoint**: ``https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py``,
``data=tmpf``, ``report_type=3`` (routine hourly METAR only -- excludes
SPECI/other ad hoc reports). Returns CSV ``station,valid,tmpf`` with
``valid`` as ``YYYY-MM-DD HH:MM`` in the requested ``tz`` (UTC here).

**Verified empirically (2026-09-18) -- the request window is half-open**
``[day1, day2)`` **in UTC**, confirmed via ``format=html``'s
``#DEBUG: Time Period`` line (e.g. ``day1=10, day2=11`` returns only
2026-09-10's hours; ``day1=10, day2=12`` returns both the 10th and 11th, not
the 12th). ``day1 == day2`` behaves the same as ``day2 == day1 + 1`` (both
return exactly day1's 24 hours) -- the API appears to floor `day2` down when
it isn't strictly after `day1`. To fetch an **inclusive** ``[start, end]``
range of UTC calendar days, this module always requests
``day2 = end + 1 day`` internally; callers of :func:`fetch_asos_observations`
pass an ordinary inclusive ``[start, end]``.

**Station ids**: unlike NOAA GHCND, IEM's ASOS network accepts the
Kalshi-style K-prefixed 4-letter ids (``KNYC``, ``KMDW``, ``KMIA``, ``KAUS``)
directly as the ``station`` query parameter -- all four were verified live
to return data. The CSV's own ``station`` column echoes IEM's bare
(non-K-prefixed) internal id (``NYC``, ``MDW``, ``MIA``, ``AUS``); this
module ignores that column for identity and always stamps rows with the
canonical K-prefixed `station_code` the caller asked for.

**Errors**: a real HTTP error (422 on a malformed request, 429 on rate
limiting) is raised by ``resp.raise_for_status()`` and retried by the
session's ``Retry`` adapter like every other fetcher in this package. But an
*unrecognized* station, or a valid request with zero matching rows, comes
back as **HTTP 200 with just the CSV header** (``station,valid,tmpf`` and no
data rows) -- this is not an error and is returned as an empty list. A
genuinely malformed 200 body (HTML, or any other unexpected first line) is
distinguished from that case by checking the header line, and raises
``ValueError`` -- see :func:`_parse_asos_csv`.

**Leak hazard**: METAR routine obs are timestamped on the observation's
own minute (typically but not always ``:53``) but are not actually
transmitted/available until a few minutes after that. Decision-time code
must go through :func:`observed_max_at`, which enforces
``OBSERVATION_LATENCY_MINUTES`` and refuses to look at an observation that
would not actually have been available yet -- never read `running_max_f`
directly off the latest row of :func:`running_max_by_instant`'s output.

**Local standard time**: as in ``archive.py``, "local day" means a *fixed*
UTC offset (``STATION_STANDARD_UTC_OFFSET_HOURS``, no DST), matching the
CLI/Kalshi settlement convention.

**Caching**: a completed UTC calendar day's raw response never changes and
is cached forever once fetched; a day within
``ASOS_RECENT_DAYS_ALWAYS_REFETCH`` days of "today" is always re-fetched,
since routine obs can be corrected/backfilled shortly after issuance.
Caching is done per UTC calendar day (mirrors ``archive.fetch_cli_daily_highs``'s
per-day IEM cache granularity), via ``src.data.cache``.

**Sparse data**: gaps and missing hours are normal for ASOS (station
maintenance, comms dropouts). This module does not interpolate -- it
reports exactly the hours it received, and surfaces `n_obs` so callers can
refuse a day with too few observations to trust its running max.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from src.data.cache import cache_get, cache_set

logger = logging.getLogger(__name__)

# --- HTTP -------------------------------------------------------------------

IEM_ASOS_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"

OBSERVATIONS_CONTACT_EMAIL = os.environ.get("NWS_CONTACT_EMAIL", "kalshi-bot@example.com")

REQUEST_TIMEOUT = (5, 30)  # (connect, read) seconds
SLEEP_SECONDS = 1.0  # politeness delay between real network calls

# METAR routine obs are timestamped on the observation's own minute but
# transmitted/available a few minutes later. A decision may only use an
# observation whose valid time is at least this far in the past.
OBSERVATION_LATENCY_MINUTES = 10

# A UTC calendar day within this many days of "today" can still be corrected
# (backfilled/QC'd) and is always re-fetched; anything older is final and is
# cached forever.
ASOS_RECENT_DAYS_ALWAYS_REFETCH = 2

# Effectively "never expire" -- a completed day's raw ASOS response does not
# change, so once cached it should never be re-requested.
LONG_CACHE_TTL_SECONDS = 100 * 365 * 24 * 3600

_SESSION: requests.Session | None = None


def _get_session() -> requests.Session:
    """Return the module-level requests.Session (built once).

    GET-only. Retries 429/5xx with backoff via an ``HTTPAdapter``, matching
    the pattern in ``src/data/weather/archive.py``, ``nws.py``, ``noaa.py``.
    """
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
        session.headers.update(
            {"User-Agent": f"(kalshi-bot-research-observations, {OBSERVATIONS_CONTACT_EMAIL})"}
        )
        _SESSION = session
    return _SESSION


def _today() -> date:
    """Return today's date. Indirection point so tests can monkeypatch "today"."""
    return date.today()


# --- Shared helpers -----------------------------------------------------------

# Fixed local-standard-time UTC offsets (no DST) for the four mapped
# stations. Mirrors ``archive.STATION_STANDARD_UTC_OFFSET_HOURS`` and
# ``src.paper.watchlist.STATION_STANDARD_UTC_OFFSET_HOURS``; duplicated (not
# imported) so this data-layer module has no dependency on `src/paper`, and
# stays self-contained like `archive.py` (its own copy carries the same
# rationale).
STATION_STANDARD_UTC_OFFSET_HOURS: dict[str, int] = {
    "KNYC": -5,
    "KMDW": -6,
    "KMIA": -5,
    "KAUS": -6,
}

# IEM ASOS station id for each mapped station. All four were verified live
# against the endpoint (2026-09-18): the K-prefixed Kalshi/ICAO id works
# directly as the `station` query parameter and returns data, even though
# the response CSV's own `station` column echoes IEM's bare 3-letter id
# (NYC/MDW/MIA/AUS) instead. No mapped station failed to return data.
ASOS_STATION_ID: dict[str, str] = {
    "KNYC": "KNYC",
    "KMDW": "KMDW",
    "KMIA": "KMIA",
    "KAUS": "KAUS",
}


def _as_date(value: str | date) -> date:
    """Coerce an ISO date string or ``date`` to a ``date``."""
    return value if isinstance(value, date) else date.fromisoformat(value)


def _parse_utc(value: str) -> datetime:
    """Parse an ISO 8601 timestamp (with or without an explicit offset) to aware UTC."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_utc(moment: datetime) -> str:
    """Format an aware UTC datetime as ISO 8601 with a trailing 'Z'."""
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _local_day_start(target: date, utc_offset_hours: int) -> datetime:
    """Return the UTC instant at which `target`'s local-standard day (00:00 local) begins.

    Uses a fixed offset (never a DST-aware zone), matching
    ``archive._local_day_start``.
    """
    return datetime.combine(
        target, datetime.min.time(), timezone(timedelta(hours=utc_offset_hours))
    ).astimezone(timezone.utc)


def _require_aware(now: datetime, caller: str) -> None:
    """Raise ValueError if `now` is not a timezone-aware datetime.

    Mirrors ``src.paper.watchlist._require_aware`` (duplicated, not
    imported, for the same layering reason as
    ``STATION_STANDARD_UTC_OFFSET_HOURS`` above).
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError(
            f"{caller}() requires a timezone-aware datetime (e.g. datetime.now(timezone.utc)); "
            f"got a naive datetime {now!r}"
        )


# --- 1. IEM ASOS observation fetch ---------------------------------------------

_EXPECTED_CSV_HEADER = "station,valid,tmpf"


def _parse_asos_csv(text: str) -> list[dict]:
    """Parse one IEM ASOS CSV body into `{valid_utc, temp_f}` rows (no station_code yet).

    Rows with a missing/unparseable `tmpf` or `valid` value are dropped
    silently (sparse data is normal). A response whose first line is not the
    expected CSV header is treated as an error payload (e.g. an HTML error
    page, or the endpoint's own plain-text "Invalid ..." messages) and
    raises `ValueError` rather than being silently parsed as zero rows.

    A *recognized-but-empty* result (an unknown station, or a valid request
    with no matching data) still comes back with the correct header and
    simply no data rows -- that is not an error and returns `[]`.
    """
    stripped = text.strip()
    if not stripped:
        raise ValueError("Empty response body from IEM ASOS endpoint (expected a CSV header)")
    first_line = stripped.splitlines()[0].strip()
    if first_line != _EXPECTED_CSV_HEADER:
        raise ValueError(
            f"Unexpected IEM ASOS response body (expected CSV header {_EXPECTED_CSV_HEADER!r}, "
            f"got {first_line[:200]!r})"
        )

    rows: list[dict] = []
    for rec in csv.DictReader(io.StringIO(text)):
        raw_temp = (rec.get("tmpf") or "").strip()
        if not raw_temp or raw_temp.upper() == "M":
            continue
        try:
            temp_f = float(raw_temp)
        except ValueError:
            continue

        raw_valid = (rec.get("valid") or "").strip()
        try:
            valid = datetime.strptime(raw_valid, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        except ValueError:
            continue

        rows.append({"valid_utc": _format_utc(valid), "temp_f": temp_f})
    return rows


def _fetch_asos_chunk(session: requests.Session, asos_id: str, start: date, end_exclusive: date) -> str:
    """GET one IEM ASOS request over the half-open UTC window [start, end_exclusive) and return raw text."""
    params = {
        "station": asos_id,
        "data": "tmpf",
        "year1": start.year,
        "month1": start.month,
        "day1": start.day,
        "year2": end_exclusive.year,
        "month2": end_exclusive.month,
        "day2": end_exclusive.day,
        "tz": "UTC",
        "format": "onlycomma",
        "latlon": "no",
        "missing": "empty",
        "trace": "empty",
        "direct": "no",
        "report_type": 3,
    }
    resp = session.get(IEM_ASOS_URL, params=params, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.text


def fetch_asos_observations(
    station_code: str,
    start: str | date,
    end: str | date,
    *,
    session: requests.Session | None = None,
    use_cache: bool = True,
) -> list[dict]:
    """Fetch hourly (routine METAR) observed temperature for `station_code` over [start, end].

    Args:
        station_code: Key into `ASOS_STATION_ID` (KNYC, KMDW, KMIA, KAUS).
        start: ISO date string or `date`, inclusive (UTC calendar date).
        end: ISO date string or `date`, inclusive.
        session: Optional `requests.Session` (for tests); defaults to the
            module-level session.
        use_cache: If `False`, always hit the network (still writes the
            cache). If `True` (default), a UTC calendar day older than
            `ASOS_RECENT_DAYS_ALWAYS_REFETCH` days is served from cache when
            present; a more recent day is always re-fetched regardless.

    Returns:
        Sorted list of `{station_code, valid_utc, temp_f}` dicts (ascending
        `valid_utc`). Rows with a missing/unparseable temperature are
        dropped. Fetching an unrecognized station, or a range with no
        observations at all, returns `[]` -- not an error.

    Raises:
        ValueError: If `station_code` is unrecognized, `start` is after
            `end`, or a response body cannot be parsed as the expected CSV
            (e.g. an HTML/plain-text error page from the endpoint).
    """
    code = station_code.upper()
    asos_id = ASOS_STATION_ID.get(code)
    if asos_id is None:
        raise ValueError(f"Unknown station_code {station_code!r}; expected one of {sorted(ASOS_STATION_ID)}")
    start_d = _as_date(start)
    end_d = _as_date(end)
    if start_d > end_d:
        raise ValueError(f"start {start_d} must be on or before end {end_d}")

    sess = session if session is not None else _get_session()
    today = _today()

    all_rows: list[dict] = []
    cur = start_d
    while cur <= end_d:
        is_recent = abs((today - cur).days) <= ASOS_RECENT_DAYS_ALWAYS_REFETCH
        cache_params = {"station": code, "date": cur.isoformat()}
        cached = (
            cache_get("iem_asos_obs", cache_params, ttl_seconds=LONG_CACHE_TTL_SECONDS)
            if (use_cache and not is_recent)
            else None
        )
        if cached is not None:
            all_rows.extend(cached)
            cur += timedelta(days=1)
            continue

        text = _fetch_asos_chunk(sess, asos_id, cur, cur + timedelta(days=1))
        parsed = _parse_asos_csv(text)
        for row in parsed:
            row["station_code"] = code
        if use_cache and not is_recent:
            cache_set("iem_asos_obs", cache_params, parsed)
        all_rows.extend(parsed)
        time.sleep(SLEEP_SECONDS)
        cur += timedelta(days=1)

    all_rows.sort(key=lambda r: r["valid_utc"])
    return all_rows


# --- 2. Pure running-max / no-look-ahead helpers -------------------------------

def running_max_by_instant(
    observations: list[dict],
    target_date: str | date,
    utc_offset_hours: int,
) -> list[dict]:
    """Restrict `observations` to one local-standard day and return cumulative maxima. Pure, no I/O.

    Args:
        observations: Rows from `fetch_asos_observations` (or any dicts with
            `valid_utc`, `temp_f`); need not be pre-sorted.
        target_date: The local-standard date to compute the running max for.
        utc_offset_hours: Fixed local-standard UTC offset (see
            `STATION_STANDARD_UTC_OFFSET_HOURS`).

    Returns:
        List of `{valid_utc, temp_f, running_max_f, n_obs}` dicts, in
        ascending `valid_utc` order, restricted to observations with
        `[local_day_start(target_date), local_day_start(target_date) + 24h)`.
        `running_max_f` is the max of `temp_f` over all observations up to
        and including that row; `n_obs` is the count of observations up to
        and including that row (1-indexed). Empty if no observation falls in
        the window.
    """
    target = _as_date(target_date)
    day_start = _local_day_start(target, utc_offset_hours)
    day_end = day_start + timedelta(hours=24)

    in_window = [
        (_parse_utc(obs["valid_utc"]), obs["temp_f"])
        for obs in observations
        if day_start <= _parse_utc(obs["valid_utc"]) < day_end
    ]
    in_window.sort(key=lambda pair: pair[0])

    out: list[dict] = []
    running_max: float | None = None
    for i, (valid, temp_f) in enumerate(in_window, start=1):
        running_max = temp_f if running_max is None else max(running_max, temp_f)
        out.append(
            {
                "valid_utc": _format_utc(valid),
                "temp_f": temp_f,
                "running_max_f": running_max,
                "n_obs": i,
            }
        )
    return out


def observed_max_at(
    running: list[dict],
    decision_instant: datetime,
    *,
    latency_minutes: int = OBSERVATION_LATENCY_MINUTES,
) -> dict | None:
    """Return the latest running-max row usable at `decision_instant`. Pure, NO LOOK-AHEAD.

    A row is usable only if its `valid_utc` plus `latency_minutes` is at or
    before `decision_instant` -- i.e. the observation would actually have
    been available by then. This is the only function in this module meant
    to back a live decision; everything else describes history.

    Args:
        running: Output of `running_max_by_instant` (or anything shaped
            like it); need not be pre-sorted.
        decision_instant: Timezone-aware instant to evaluate availability
            against.
        latency_minutes: Reporting latency allowance (default
            `OBSERVATION_LATENCY_MINUTES`).

    Returns:
        The row (dict) with the latest `valid_utc` satisfying
        `valid_utc + latency_minutes <= decision_instant`, or `None` if no
        row qualifies (including an empty `running`).

    Raises:
        ValueError: If `decision_instant` is not timezone-aware.
    """
    _require_aware(decision_instant, "observed_max_at")
    cutoff = decision_instant - timedelta(minutes=latency_minutes)

    best: dict | None = None
    best_valid: datetime | None = None
    for row in running:
        valid = _parse_utc(row["valid_utc"])
        if valid <= cutoff and (best_valid is None or valid > best_valid):
            best = row
            best_valid = valid
    return best


# --- 3. Pure daily summary (CLI support) ---------------------------------------

def build_daily_summary(
    observations: list[dict],
    start: str | date,
    end: str | date,
    utc_offset_hours: int,
) -> list[dict]:
    """For each local-standard date in [start, end], report the day's final observed max so far. Pure, no I/O.

    Args:
        observations: Rows from `fetch_asos_observations`.
        start: ISO date string or `date`, inclusive.
        end: ISO date string or `date`, inclusive.
        utc_offset_hours: Fixed local-standard UTC offset.

    Returns:
        List of `{date_lst, observed_max_f, n_obs}` dicts, one per date in
        `[start, end]`, in date order. A date with no observations at all
        gets `observed_max_f: None, n_obs: 0`.
    """
    start_d = _as_date(start)
    end_d = _as_date(end)
    out: list[dict] = []
    cur = start_d
    while cur <= end_d:
        running = running_max_by_instant(observations, cur, utc_offset_hours)
        if running:
            out.append(
                {
                    "date_lst": cur.isoformat(),
                    "observed_max_f": running[-1]["running_max_f"],
                    "n_obs": running[-1]["n_obs"],
                }
            )
        else:
            out.append({"date_lst": cur.isoformat(), "observed_max_f": None, "n_obs": 0})
        cur += timedelta(days=1)
    return out


def format_daily_summary(summary: list[dict]) -> str:
    """Render `build_daily_summary`'s output as a human-readable table."""
    lines = [f"{'date':<12} {'observed_max_f':>15} {'n_obs':>7}"]
    for row in summary:
        max_str = "n/a" if row["observed_max_f"] is None else f"{row['observed_max_f']:.1f}"
        lines.append(f"{row['date_lst']:<12} {max_str:>15} {row['n_obs']:>7}")
    return "\n".join(lines)


# --- 4. CLI ---------------------------------------------------------------------

DEFAULT_OUTPUT_PATH = "data/weather/asos_observations.jsonl"


def run_fetch(station_code: str, start: str, end: str, output_path: str) -> list[dict]:
    """Fetch ASOS observations for one station over [start, end] and write JSONL."""
    rows = fetch_asos_observations(station_code, start, end)
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    logger.info("Wrote %d rows to %s", len(rows), out)
    return rows


def run_summary(input_path: str, station_code: str, start: str, end: str) -> list[dict]:
    """Load a JSONL file written by `run_fetch` and summarize per-day observed max vs n_obs."""
    code = station_code.upper()
    offset = STATION_STANDARD_UTC_OFFSET_HOURS[code]
    rows = []
    with open(input_path) as fh:
        for line in fh:
            line = line.strip()
            if line:
                row = json.loads(line)
                if row.get("station_code") == code:
                    rows.append(row)
    return build_daily_summary(rows, start, end, offset)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments for the `fetch`/`summary` subcommands."""
    parser = argparse.ArgumentParser(
        prog="python -m src.data.weather.observations",
        description="Fetch IEM ASOS intraday observed temperature and summarize per-day running max.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    fetch_p = sub.add_parser("fetch", help="Fetch hourly observations for one station and write JSONL.")
    fetch_p.add_argument("--station", required=True, help="Station code, e.g. KNYC")
    fetch_p.add_argument("--start", required=True, help="ISO start date (UTC calendar day), inclusive")
    fetch_p.add_argument("--end", required=True, help="ISO end date, inclusive")
    fetch_p.add_argument("--output", default=DEFAULT_OUTPUT_PATH, help="Output JSONL path")

    summary_p = sub.add_parser(
        "summary", help="Summarize per-day observed max vs number of observations from a fetched JSONL file."
    )
    summary_p.add_argument("--input", required=True, help="JSONL path written by `fetch`")
    summary_p.add_argument("--station", required=True, help="Station code, e.g. KNYC")
    summary_p.add_argument("--start", required=True, help="ISO start date (local-standard), inclusive")
    summary_p.add_argument("--end", required=True, help="ISO end date, inclusive")

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """CLI entry point."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args(argv)
    if args.command == "fetch":
        run_fetch(args.station, args.start, args.end, args.output)
    elif args.command == "summary":
        summary = run_summary(args.input, args.station, args.start, args.end)
        print(format_daily_summary(summary))


if __name__ == "__main__":
    main()
