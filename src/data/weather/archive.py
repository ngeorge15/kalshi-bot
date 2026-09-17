"""Archived NBM forecasts vs. official CLI daily highs, for backtesting.

Builds a leakage-safe historical dataset pairing:

* **Forecast**: Open-Meteo's ``previous-runs-api``, ``models=ncep_nbm_conus``,
  ``temperature_2m_previous_day1``/``_previous_day2``. Per Open-Meteo's docs,
  ``previous_dayN`` is "the value predicted N*24 hours before valid time" --
  a genuine fixed-lead reforecast, not a blend of run-start hours. **Do not**
  use ``historical-forecast-api`` instead: per its own docs it stitches
  together the first hours of successive model runs (a near-nowcast), which
  would leak same-day information into a same-day "forecast" and grossly
  overstate skill. See ``research/historical-forecasts.md``'s CORRECTION
  block, which supersedes that doc's own original Recommendation section.
  NBM coverage on this endpoint starts around **2024-11** (null before, not
  all stations null on exactly the same date -- treat sparsely-null tails as
  "not yet available" rather than an error).

* **Truth**: the NWS "CLI" (climate report) daily maximum, which is what
  Kalshi's daily-high-temperature contracts settle against -- not NOAA CDO
  GHCND (``src/data/weather/noaa.py``), which can differ in QC/rounding/
  timing. Fetched from the Iowa Environmental Mesonet (IEM), which archives
  the raw NWS text product: ``/api/1/nws/afos/list.json`` to find each day's
  issuances by PIL, then ``/api/1/nwstext/{product_id}`` for the raw text,
  parsed for the "...YESTERDAY..." MAXIMUM line (the finalized full local
  day; a same-day "...TODAY..." issuance is a same-day partial reading and
  is never used as an observed daily high here).

Both fetchers cache raw JSON/text on disk via :mod:`src.data.cache` (under
the gitignored ``data/`` directory), so a completed month/day is never
re-fetched; a currently-in-progress month or a day close to "today" is
always re-fetched, since its data can still change.

:func:`build_daily_dataset` and :func:`summarize_errors` are pure (no I/O)
and operate on the plain dicts the fetchers return.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import statistics
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from src.data.cache import cache_get, cache_set
from src.data.weather.station_map import STATIONS

logger = logging.getLogger(__name__)

# --- HTTP -------------------------------------------------------------------

PREVIOUS_RUNS_URL = "https://previous-runs-api.open-meteo.com/v1/forecast"
IEM_AFOS_LIST_URL = "https://mesonet.agron.iastate.edu/api/1/nws/afos/list.json"
IEM_NWSTEXT_URL_TEMPLATE = "https://mesonet.agron.iastate.edu/api/1/nwstext/{product_id}"

ARCHIVE_CONTACT_EMAIL = os.environ.get("NWS_CONTACT_EMAIL", "kalshi-bot@example.com")

REQUEST_TIMEOUT = (5, 30)  # (connect, read) seconds
SLEEP_SECONDS = 1.0  # politeness delay between real network calls

# ~1 calendar month per previous-runs-api request, per D-style chunking.
PREVIOUS_RUNS_CHUNK_DAYS = 31

# CLI reports issued within this many days of "today" are always re-fetched
# (corrections can land a day or two late; a same-day report may not exist
# yet at all).
CLI_RECENT_DAYS_ALWAYS_REFETCH = 3

# Effectively "never expire" -- a completed month/day's raw archive response
# does not change, so once cached it should never be re-requested.
LONG_CACHE_TTL_SECONDS = 100 * 365 * 24 * 3600

_SESSION: requests.Session | None = None


def _get_session() -> requests.Session:
    """Return the module-level requests.Session (built once).

    GET-only. Retries 429/5xx with backoff via an ``HTTPAdapter``, matching
    the pattern in ``src/data/weather/nws.py`` and ``src/data/weather/noaa.py``.
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
            {"User-Agent": f"(kalshi-bot-research-archive, {ARCHIVE_CONTACT_EMAIL})"}
        )
        _SESSION = session
    return _SESSION


# --- Shared helpers -----------------------------------------------------------

# Fixed local-standard-time UTC offsets (no DST) for the four mapped
# stations. Mirrors ``src.paper.watchlist.STATION_STANDARD_UTC_OFFSET_HOURS``;
# duplicated (not imported) so this data-layer module has no dependency on
# ``src/paper``.
STATION_STANDARD_UTC_OFFSET_HOURS: dict[str, int] = {
    "KNYC": -5,
    "KMDW": -6,
    "KMIA": -5,
    "KAUS": -6,
}

# NWS CLI product PILs for the four mapped stations.
CLI_PIL: dict[str, str] = {
    "KNYC": "CLINYC",
    "KMDW": "CLIMDW",
    "KMIA": "CLIMIA",
    "KAUS": "CLIAUS",
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

    Uses a fixed offset (never a DST-aware zone), so the 24 hourly instants
    this implies are always exactly 1 hour apart in UTC regardless of any
    real-world DST transition on `target`'s date.
    """
    return datetime.combine(
        target, datetime.min.time(), timezone(timedelta(hours=utc_offset_hours))
    ).astimezone(timezone.utc)


def _finite(value: object) -> bool:
    """True if `value` is a real, finite number (rejecting bool and NaN/inf)."""
    return (
        value is not None
        and not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
    )


def _month_chunks(start: date, end: date, max_days: int) -> list[tuple[date, date]]:
    """Split [start, end] (inclusive) into consecutive chunks of at most `max_days` days."""
    chunks: list[tuple[date, date]] = []
    cur = start
    while cur <= end:
        chunk_end = min(cur + timedelta(days=max_days - 1), end)
        chunks.append((cur, chunk_end))
        cur = chunk_end + timedelta(days=1)
    return chunks


# --- 1. NBM previous-runs forecast archive ------------------------------------

def _fetch_previous_runs_chunk(
    session: requests.Session, lat: float, lon: float, chunk_start: date, chunk_end: date
) -> dict:
    """GET one previous-runs-api chunk and return the parsed JSON body."""
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "temperature_2m_previous_day1,temperature_2m_previous_day2",
        "models": "ncep_nbm_conus",
        "temperature_unit": "fahrenheit",
        "timezone": "GMT",
        "start_date": chunk_start.isoformat(),
        "end_date": chunk_end.isoformat(),
    }
    resp = session.get(PREVIOUS_RUNS_URL, params=params, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _parse_previous_runs_payload(payload: dict) -> list[dict]:
    """Turn one previous-runs-api JSON body into `{valid_utc, lead1_f, lead2_f}` rows."""
    hourly = payload.get("hourly", {}) or {}
    times = hourly.get("time", []) or []
    lead1 = hourly.get("temperature_2m_previous_day1", []) or []
    lead2 = hourly.get("temperature_2m_previous_day2", []) or []
    rows = []
    for i, t in enumerate(times):
        valid = _parse_utc(t)
        rows.append(
            {
                "valid_utc": _format_utc(valid),
                "lead1_f": lead1[i] if i < len(lead1) else None,
                "lead2_f": lead2[i] if i < len(lead2) else None,
            }
        )
    return rows


def fetch_nbm_previous_runs(
    station_code: str,
    start_date: str | date,
    end_date: str | date,
    session: requests.Session | None = None,
    today: date | None = None,
) -> list[dict]:
    """Fetch hourly NBM fixed-lead forecasts for `station_code` over [start_date, end_date].

    Requests are chunked to at most ``PREVIOUS_RUNS_CHUNK_DAYS`` days each,
    with a polite sleep between real network calls. A chunk whose end date is
    on or after `today` (i.e. the current, still-incomplete month) is always
    re-fetched; earlier, completed chunks are cached on disk forever.

    Args:
        station_code: Key into ``src.data.weather.station_map.STATIONS``.
        start_date: ISO date string or `date`, inclusive.
        end_date: ISO date string or `date`, inclusive.
        session: Optional `requests.Session` (for tests); defaults to the
            module-level session.
        today: Optional override of "today" for cache-currency decisions
            (for tests); defaults to `date.today()`.

    Returns:
        List of `{valid_utc, lead1_f, lead2_f}` dicts, one per hour, ordered
        by `valid_utc`. `lead1_f`/`lead2_f` are `None` where NBM has no data
        yet (e.g. before ~2024-11).

    Raises:
        ValueError: If `station_code` is unrecognized or `start_date` is
            after `end_date`.
    """
    code = station_code.upper()
    station = STATIONS.get(code)
    if station is None:
        raise ValueError(f"Unknown station_code {station_code!r}; expected one of {sorted(STATIONS)}")
    start = _as_date(start_date)
    end = _as_date(end_date)
    if start > end:
        raise ValueError(f"start_date {start} must be on or before end_date {end}")

    sess = session if session is not None else _get_session()
    ref_today = today if today is not None else date.today()

    rows: list[dict] = []
    for chunk_start, chunk_end in _month_chunks(start, end, PREVIOUS_RUNS_CHUNK_DAYS):
        cache_params = {
            "station": code,
            "start": chunk_start.isoformat(),
            "end": chunk_end.isoformat(),
            "models": "ncep_nbm_conus",
        }
        is_current = chunk_end >= ref_today
        cached = None
        if not is_current:
            cached = cache_get("archive_nbm_previous_runs", cache_params, ttl_seconds=LONG_CACHE_TTL_SECONDS)
        if cached is not None:
            rows.extend(cached)
            continue

        payload = _fetch_previous_runs_chunk(sess, station["lat"], station["lon"], chunk_start, chunk_end)
        parsed = _parse_previous_runs_payload(payload)
        cache_set("archive_nbm_previous_runs", cache_params, parsed)
        rows.extend(parsed)
        time.sleep(SLEEP_SECONDS)

    return rows


# --- 2. IEM CLI daily-high archive ---------------------------------------------

_SUMMARY_DATE_RE = re.compile(r"CLIMATE SUMMARY FOR ([A-Z]+ \d{1,2},? \d{4})")
_TEMP_MAXIMUM_RE = re.compile(r"TEMPERATURE\s*\(F\)\s*\n\s*(YESTERDAY|TODAY)\s*\n\s*MAXIMUM\s+(-?\d+|MM)\b")

_MONTH_NUMBERS = {
    name: i
    for i, name in enumerate(
        [
            "JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE",
            "JULY", "AUGUST", "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER",
        ],
        start=1,
    )
}


def _parse_cli_text(text: str, source: str) -> dict | None:
    """Parse one CLI product's text for its finalized ("YESTERDAY") daily maximum.

    Returns `None` if the product does not contain a completed calendar-day
    summary (e.g. it is a same-day "TODAY" partial issuance) or cannot be
    parsed at all.
    """
    date_match = _SUMMARY_DATE_RE.search(text)
    temp_match = _TEMP_MAXIMUM_RE.search(text)
    if date_match is None or temp_match is None:
        return None
    period, raw_value = temp_match.group(1), temp_match.group(2)
    if period != "YESTERDAY":
        return None
    month_name, day_str, year_str = date_match.group(1).replace(",", "").split()
    month = _MONTH_NUMBERS.get(month_name)
    if month is None:
        return None
    try:
        target = date(int(year_str), month, int(day_str))
    except ValueError:
        return None
    max_f = None if raw_value == "MM" else float(raw_value)
    return {"date_lst": target.isoformat(), "max_f": max_f, "source": source}


def _fetch_cli_day(
    session: requests.Session, station_code: str, pil: str, entered_date: date, use_cache: bool
) -> dict | None:
    """Fetch and parse the CLI product(s) entered (issued) on `entered_date` (UTC).

    Tries each issuance for that UTC day, earliest first, until one parses as
    a finalized ("YESTERDAY") daily maximum. Returns `None` if none does
    (e.g. no report was issued that day, or only a same-day partial exists).
    """
    list_params = {"pil": pil, "date": entered_date.isoformat()}
    entries = cache_get("archive_iem_cli_list", list_params, ttl_seconds=LONG_CACHE_TTL_SECONDS) if use_cache else None
    if entries is None:
        resp = session.get(
            IEM_AFOS_LIST_URL, params={"pil": pil, "date": entered_date.isoformat()}, timeout=REQUEST_TIMEOUT
        )
        resp.raise_for_status()
        entries = resp.json().get("data", [])
        cache_set("archive_iem_cli_list", list_params, entries)
        time.sleep(SLEEP_SECONDS)

    for entry in sorted(entries, key=lambda e: e["entered"]):
        product_id = entry["product_id"]
        text_params = {"product_id": product_id}
        cached_text = (
            cache_get("archive_iem_cli_text", text_params, ttl_seconds=LONG_CACHE_TTL_SECONDS) if use_cache else None
        )
        if cached_text is not None:
            text = cached_text["text"]
        else:
            resp = session.get(IEM_NWSTEXT_URL_TEMPLATE.format(product_id=product_id), timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            text = resp.text
            cache_set("archive_iem_cli_text", text_params, {"text": text})
            time.sleep(SLEEP_SECONDS)

        parsed = _parse_cli_text(text, source=f"IEM CLI {pil} ({product_id})")
        if parsed is not None:
            return parsed
    return None


def fetch_cli_daily_highs(
    station_code: str,
    start_date: str | date,
    end_date: str | date,
    session: requests.Session | None = None,
    today: date | None = None,
) -> list[dict]:
    """Fetch official NWS CLI daily maximum temperatures for `station_code`.

    The CLI (climate report) is what Kalshi's KNYC/KMDW/KMIA/KAUS daily-high
    contracts settle against. **Dates here are local standard time (LST)**:
    the "MAXIMUM" figure in a CLI report is the calendar-day max as recorded
    by the issuing NWS office, for that office's local day -- the same
    LST-day convention `src/paper/weather.py` and `src/paper/watchlist.py`
    use for `utc_offset_hours`.

    Product/field used: the "CLI" text product (e.g. `CLINYC` for KNYC),
    archived by the Iowa Environmental Mesonet (IEM) and retrieved via
    `/api/1/nws/afos/list.json?pil=<PIL>&date=<UTC date>` (IEM only supports
    a single UTC issuance date per call, not a range, so one call is made
    per UTC calendar day) followed by `/api/1/nwstext/{product_id}` for the
    raw text. Only the "...YESTERDAY..." MAXIMUM line is used -- a same-day
    "...TODAY..." issuance is a same-day partial reading (the day is not
    over yet) and is never treated as an observed daily high.

    Args:
        station_code: Key into `CLI_PIL` (KNYC, KMDW, KMIA, KAUS).
        start_date: ISO date string or `date`, inclusive (local-standard
            target date).
        end_date: ISO date string or `date`, inclusive.
        session: Optional `requests.Session` (for tests).
        today: Optional override of "today" for cache-currency decisions
            (for tests); defaults to `date.today()`.

    Returns:
        List of `{date_lst, max_f, source}` dicts, one per date that has a
        finalized CLI report, sorted by `date_lst`. `max_f` is `None` when
        the report itself recorded the value as missing ("MM"). Dates with
        no CLI report at all are simply absent from the result.

    Raises:
        ValueError: If `station_code` has no CLI PIL mapping or `start_date`
            is after `end_date`.
    """
    code = station_code.upper()
    pil = CLI_PIL.get(code)
    if pil is None:
        raise ValueError(f"No CLI PIL mapping for station_code {station_code!r}; expected one of {sorted(CLI_PIL)}")
    start = _as_date(start_date)
    end = _as_date(end_date)
    if start > end:
        raise ValueError(f"start_date {start} must be on or before end_date {end}")

    sess = session if session is not None else _get_session()
    ref_today = today if today is not None else date.today()

    # CLI reports for LST date D are issued shortly after local midnight,
    # which (for offsets -5/-6h) is still on UTC date D+1. Scan one extra
    # UTC day past `end` to catch the last target date's report.
    scan_start = start
    scan_end = end + timedelta(days=1)

    by_date: dict[str, dict] = {}
    cur = scan_start
    while cur <= scan_end:
        use_cache = (ref_today - cur).days >= CLI_RECENT_DAYS_ALWAYS_REFETCH
        row = _fetch_cli_day(sess, code, pil, cur, use_cache)
        if row is not None and start.isoformat() <= row["date_lst"] <= end.isoformat():
            by_date.setdefault(row["date_lst"], row)
        cur += timedelta(days=1)

    return [by_date[d] for d in sorted(by_date)]


# --- 3. Pure dataset assembly ---------------------------------------------------

def build_daily_dataset(
    station_code: str,
    forecasts: list[dict],
    cli_rows: list[dict],
    utc_offset_hours: int,
) -> list[dict]:
    """Assemble one row per local-standard day from raw forecast + CLI rows. Pure, no I/O.

    For each candidate local-standard date (the union of dates implied by
    `forecasts`' hourly timestamps and `cli_rows`' `date_lst` values), gathers
    the 24 hourly instants covering [local_day_start(date), +24h) for each
    lead. A lead is "complete" only if all 24 of its hourly values are
    present and finite -- an incomplete lead's `forecast_max_*_f` is `None`,
    never filled in from partial data. A day with no CLI observation, or a
    CLI observation of "MM" (missing), is marked incomplete with a reason;
    it is never dropped silently -- callers that want it dropped should
    filter on `complete`.

    Args:
        station_code: Station ticker, recorded verbatim in each output row.
        forecasts: Rows from `fetch_nbm_previous_runs` (`valid_utc`,
            `lead1_f`, `lead2_f`).
        cli_rows: Rows from `fetch_cli_daily_highs` (`date_lst`, `max_f`).
        utc_offset_hours: Fixed local-standard UTC offset, in hours (see
            `STATION_STANDARD_UTC_OFFSET_HOURS`).

    Returns:
        List of dicts, sorted by `date_lst`, each with keys `station`,
        `date_lst`, `forecast_max_lead1_f`, `forecast_max_lead2_f`,
        `hourly_lead1` (list of 24, possibly containing `None`),
        `observed_max_f`, `complete`, `reason` (`None` if complete),
        `latest_issued_utc_lead1`.
    """
    lead1_by_valid: dict[datetime, float | None] = {}
    lead2_by_valid: dict[datetime, float | None] = {}
    candidate_dates: set[date] = set()
    offset_td = timedelta(hours=utc_offset_hours)

    for row in forecasts:
        valid = _parse_utc(row["valid_utc"])
        lead1_by_valid[valid] = row.get("lead1_f")
        lead2_by_valid[valid] = row.get("lead2_f")
        candidate_dates.add((valid + offset_td).date())

    cli_by_date: dict[date, dict] = {}
    for row in cli_rows:
        d = _as_date(row["date_lst"])
        cli_by_date[d] = row
        candidate_dates.add(d)

    results = []
    for target in sorted(candidate_dates):
        start = _local_day_start(target, utc_offset_hours)
        needed = [start + timedelta(hours=h) for h in range(24)]

        hourly_lead1 = [lead1_by_valid.get(h) for h in needed]
        hourly_lead2 = [lead2_by_valid.get(h) for h in needed]

        lead1_complete = all(_finite(v) for v in hourly_lead1)
        lead2_complete = all(_finite(v) for v in hourly_lead2)

        forecast_max_lead1 = max(hourly_lead1) if lead1_complete else None
        forecast_max_lead2 = max(hourly_lead2) if lead2_complete else None

        cli_row = cli_by_date.get(target)
        observed_max = cli_row["max_f"] if cli_row is not None else None

        reasons = []
        if not lead1_complete:
            missing = sum(1 for v in hourly_lead1 if not _finite(v))
            reasons.append(f"lead1 missing {missing}/24 hours")
        if not lead2_complete:
            missing = sum(1 for v in hourly_lead2 if not _finite(v))
            reasons.append(f"lead2 missing {missing}/24 hours")
        if cli_row is None:
            reasons.append("no CLI observation for date")
        elif observed_max is None:
            reasons.append("CLI observed max missing (MM)")

        complete = lead1_complete and lead2_complete and observed_max is not None

        # Each hour of `hourly_lead1` comes from a different model run; the
        # latest issuance among them is the one behind the day's last hour
        # (23:00 local), issued 24h before it -- i.e. 1h before local
        # midnight. A market price compared against this "forecast" must be
        # taken at or after this instant.
        latest_issued_utc_lead1 = _format_utc(needed[-1] - timedelta(hours=24))

        results.append(
            {
                "station": station_code.upper(),
                "date_lst": target.isoformat(),
                "forecast_max_lead1_f": forecast_max_lead1,
                "forecast_max_lead2_f": forecast_max_lead2,
                "hourly_lead1": hourly_lead1,
                "observed_max_f": observed_max,
                "complete": complete,
                "reason": "; ".join(reasons) if reasons else None,
                "latest_issued_utc_lead1": latest_issued_utc_lead1,
            }
        )

    return results


# --- 4. Pure error summary (train/test, no leakage) -----------------------------

_SEASON_BY_MONTH = {
    12: "DJF", 1: "DJF", 2: "DJF",
    3: "MAM", 4: "MAM", 5: "MAM",
    6: "JJA", 7: "JJA", 8: "JJA",
    9: "SON", 10: "SON", 11: "SON",
}

_LEAD_FIELDS = (("lead1", "forecast_max_lead1_f"), ("lead2", "forecast_max_lead2_f"))


def _error_stats(errors: list[float]) -> dict:
    """Mean and sample std-dev of `errors` (n=1 gives std=None, not a divide-by-zero)."""
    n = len(errors)
    if n == 0:
        return {"n": 0, "mean_bias_f": None, "std_f": None}
    return {
        "n": n,
        "mean_bias_f": statistics.mean(errors),
        "std_f": statistics.stdev(errors) if n >= 2 else None,
    }


def summarize_errors(rows: list[dict], train_end_date: str | date) -> dict:
    """Summarize forecast error (observed - forecast) bias/std, train vs. test. Pure, no I/O.

    Only rows from `build_daily_dataset` with both a forecast max for a given
    lead and an observed max are counted for that lead (an incomplete lead is
    simply excluded from that lead's stats, not treated as a zero error).
    Rows with `date_lst` on or before `train_end_date` are "train"; everything
    after is "test". This mirrors `src.validation.splitter.TemporalSplitter`'s
    chronological, no-shuffling split so `sigma_f` can be fit on `train` alone
    without peeking at `test`.

    Args:
        rows: Rows from `build_daily_dataset` (any subset of stations).
        train_end_date: ISO date string or `date`; the last date included in
            the train slice.

    Returns:
        `{"train": {...}, "test": {...}}`, each mapping station code to
        `{"lead1": {n, mean_bias_f, std_f}, "lead2": {...},
        "by_season": {"DJF": {"lead1": {...}, "lead2": {...}}, ...}}`.
    """
    train_end = _as_date(train_end_date)
    by_station_lead: dict[str, dict[str, dict[str, list[float]]]] = {"train": {}, "test": {}}
    by_station_season_lead: dict[str, dict[str, dict[str, dict[str, list[float]]]]] = {"train": {}, "test": {}}

    for row in rows:
        target = _as_date(row["date_lst"])
        slice_name = "train" if target <= train_end else "test"
        station = row["station"]
        season = _SEASON_BY_MONTH[target.month]
        observed = row.get("observed_max_f")
        if observed is None:
            continue
        for lead_key, forecast_key in _LEAD_FIELDS:
            forecast = row.get(forecast_key)
            if forecast is None:
                continue
            error = observed - forecast
            (
                by_station_lead[slice_name].setdefault(station, {}).setdefault(lead_key, []).append(error)
            )
            (
                by_station_season_lead[slice_name]
                .setdefault(station, {})
                .setdefault(season, {})
                .setdefault(lead_key, [])
                .append(error)
            )

    summary: dict = {"train": {}, "test": {}}
    for slice_name in ("train", "test"):
        stations = set(by_station_lead[slice_name]) | set(by_station_season_lead[slice_name])
        for station in stations:
            leads = by_station_lead[slice_name].get(station, {})
            station_summary = {lead_key: _error_stats(errs) for lead_key, errs in leads.items()}
            seasons = by_station_season_lead[slice_name].get(station, {})
            station_summary["by_season"] = {
                season: {lead_key: _error_stats(errs) for lead_key, errs in lead_map.items()}
                for season, lead_map in seasons.items()
            }
            summary[slice_name][station] = station_summary

    return summary


# --- 5. CLI -------------------------------------------------------------------

DEFAULT_OUTPUT_PATH = "data/weather/nbm_cli_daily.jsonl"


def run_build(start: str, end: str, station_codes: list[str], output_path: str) -> list[dict]:
    """Fetch forecasts + CLI highs for each station, assemble, and write JSONL.

    Args:
        start: ISO start date (inclusive).
        end: ISO end date (inclusive).
        station_codes: Station tickers to process.
        output_path: Destination JSONL path (parent dirs created as needed).

    Returns:
        All assembled rows (all stations), sorted by station then date.
    """
    # The local-standard day for `end` needs forecast hours that run past
    # midnight UTC on `end` (e.g. a -5h offset's last local hour is 04:00 UTC
    # the next day) -- fetch one extra UTC day of forecast so that day isn't
    # spuriously marked incomplete, then drop anything outside [start, end].
    forecast_end = (_as_date(end) + timedelta(days=1)).isoformat()

    all_rows: list[dict] = []
    for raw_code in station_codes:
        code = raw_code.strip().upper()
        offset = STATION_STANDARD_UTC_OFFSET_HOURS[code]
        forecasts = fetch_nbm_previous_runs(code, start, forecast_end)
        cli_rows = fetch_cli_daily_highs(code, start, end)
        rows = build_daily_dataset(code, forecasts, cli_rows, offset)
        rows = [r for r in rows if start <= r["date_lst"] <= end]
        all_rows.extend(rows)

        n_complete = sum(1 for r in rows if r["complete"])
        pct = 100 * n_complete / len(rows) if rows else 0.0
        logger.info("%s: %d days assembled, %d complete (%.1f%%)", code, len(rows), n_complete, pct)

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as fh:
        for row in all_rows:
            fh.write(json.dumps(row) + "\n")
    logger.info("Wrote %d rows to %s", len(all_rows), out)
    return all_rows


def run_summarize(input_path: str, train_end: str) -> dict:
    """Load a JSONL dataset written by `run_build` and summarize forecast error."""
    rows = []
    with open(input_path) as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return summarize_errors(rows, train_end)


def _fmt_stat(stats: dict) -> str:
    bias = "n/a" if stats["mean_bias_f"] is None else f"{stats['mean_bias_f']:+.2f}F"
    std = "n/a" if stats["std_f"] is None else f"{stats['std_f']:.2f}F"
    return f"n={stats['n']:<5} mean_bias={bias:<9} std={std}"


def format_summary(summary: dict) -> str:
    """Render `summarize_errors`'s output as a human-readable report."""
    lines = []
    for slice_name in ("train", "test"):
        lines.append(f"=== {slice_name.upper()} ===")
        stations = sorted(summary.get(slice_name, {}))
        if not stations:
            lines.append("  (no rows)")
        for station in stations:
            station_summary = summary[slice_name][station]
            lines.append(f"  {station}")
            for lead_key, _ in _LEAD_FIELDS:
                if lead_key in station_summary:
                    lines.append(f"    {lead_key:<6} {_fmt_stat(station_summary[lead_key])}")
            by_season = station_summary.get("by_season", {})
            for season in ("DJF", "MAM", "JJA", "SON"):
                if season not in by_season:
                    continue
                for lead_key, _ in _LEAD_FIELDS:
                    if lead_key in by_season[season]:
                        lines.append(f"    {season} {lead_key:<6} {_fmt_stat(by_season[season][lead_key])}")
        lines.append("")
    return "\n".join(lines).rstrip("\n")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments for the `build`/`summarize` subcommands."""
    parser = argparse.ArgumentParser(
        prog="python -m src.data.weather.archive",
        description="Build and summarize the archived NBM-forecast-vs-CLI-observed daily dataset.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    build_p = sub.add_parser(
        "build",
        help="Fetch NBM previous-runs forecasts and IEM CLI daily highs, assemble, write JSONL.",
    )
    build_p.add_argument("--start", required=True, help="ISO start date (local-standard), e.g. 2024-11-01")
    build_p.add_argument("--end", required=True, help="ISO end date (inclusive)")
    build_p.add_argument(
        "--stations",
        default=",".join(sorted(STATIONS)),
        help="Comma-separated station codes (default: all mapped stations)",
    )
    build_p.add_argument("--output", default=DEFAULT_OUTPUT_PATH, help="Output JSONL path")

    summarize_p = sub.add_parser("summarize", help="Summarize forecast error bias/std, train vs. test.")
    summarize_p.add_argument("--input", required=True, help="JSONL path written by `build`")
    summarize_p.add_argument(
        "--train-end", required=True, dest="train_end",
        help="ISO date; rows on or before this date are 'train', later rows are 'test'",
    )

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """CLI entry point."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args(argv)
    if args.command == "build":
        codes = [c.strip() for c in args.stations.split(",") if c.strip()]
        run_build(args.start, args.end, codes, args.output)
    elif args.command == "summarize":
        summary = run_summarize(args.input, args.train_end)
        print(format_summary(summary))


if __name__ == "__main__":
    main()
