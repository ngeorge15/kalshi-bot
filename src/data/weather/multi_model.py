"""Archived multi-model previous-runs forecasts, for the multi-model ensemble experiment.

Generalises :mod:`src.data.weather.archive`'s single-model (NBM-only)
pipeline over six independent deterministic models, all served by the same
leak-safe endpoint. Per ``research/multi-model-forecasting.md``:

> Our weather model is one deterministic forecast wrapped in a normal with
> one fitted sigma. Multi-model combination and a state-dependent sigma
> (fitted from the models' disagreement) are the standard, cheap fixes.

**Forecast**: Open-Meteo's ``previous-runs-api`` (same endpoint, same
``temperature_2m_previous_day1``/``_previous_day2`` fixed-lead fields, as
:func:`src.data.weather.archive.fetch_nbm_previous_runs`), queried once per
model in :data:`MODEL_IDS`. **Do not** switch to ``historical-forecast-api``
for any of these models -- it stitches run-start hours into a near-nowcast
and would leak, exactly as documented in ``archive.py``.

**Truth**: reuses :func:`src.data.weather.archive.fetch_cli_daily_highs`
unchanged -- the NWS CLI daily maximum is the settlement source regardless
of which model(s) produced the forecast.

**Per-model archive coverage is not simultaneous.** Each model's fixed-lead
archive begins on its own date (see :data:`MODEL_ARCHIVE_USABLE_START`,
verified by direct query, not documentation). A model queried before its own
coverage begins returns null hourly values, exactly like NBM before
``NBM_ARCHIVE_USABLE_START`` -- :func:`build_multi_model_dataset` treats that
as "not yet available" (the lead is simply incomplete for that model on that
day, contributing ``None`` and excluded from the ensemble aggregates), never
as an error and never as a degenerate-series fill value. ``n_models_lead1``/
``n_models_lead2`` record how many models actually contributed each day, so
a consumer can tell a 2-model spread from a 6-model spread apart -- a
missing model is never imputed.

Caching and chunking follow ``archive.py`` exactly (a completed month is
never re-fetched). Six models means six times the request volume of the
single-model archive, so this module is deliberately not concurrent --
:data:`src.data.weather.archive.SLEEP_SECONDS` is paid between every real
network call, same as before.

:func:`build_multi_model_dataset` is pure (no I/O) and operates on the plain
dicts the fetchers return. Its output schema is frozen (documented on the
function) -- another module is being written against it in parallel; do not
rename a key.
"""
from __future__ import annotations

import argparse
import json
import logging
import statistics
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

from src.data.cache import cache_get, cache_set
from src.data.weather.archive import (
    LONG_CACHE_TTL_SECONDS,
    NBM_ARCHIVE_USABLE_START,
    PREVIOUS_RUNS_CHUNK_DAYS,
    PREVIOUS_RUNS_URL,
    REQUEST_TIMEOUT,
    SLEEP_SECONDS,
    STATION_STANDARD_UTC_OFFSET_HOURS,
    _as_date,
    _finite,
    _format_utc,
    _get_session,
    _local_day_start,
    _month_chunks,
    _parse_previous_runs_payload,
    _parse_utc,
    fetch_cli_daily_highs,
    is_degenerate_series,
)
from src.data.weather.station_map import STATIONS

logger = logging.getLogger(__name__)

# --- Models -------------------------------------------------------------------

# Six independent deterministic models on the previous-runs-api, verified by
# direct query (`research/multi-model-forecasting.md`, 2026-09-18).
# `ecmwf_aifs025` (no `_single`) returns all-null and `graphcast025` is
# rejected outright -- neither is included here.
MODEL_IDS: tuple[str, ...] = (
    "ncep_nbm_conus",
    "ecmwf_ifs025",
    "ecmwf_aifs025_single",
    "gfs_seamless",
    "icon_seamless",
    "ukmo_global_deterministic_10km",
)

# Per-model first date with usable fixed-lead archive coverage at KNYC
# (40.7794, -73.9692), verified by bisecting the live endpoint on 2026-09-18
# (non-null-hour-count probes, not documentation). "Usable" here means the
# hourly archive returns real numbers rather than nulls for both
# `previous_day1` and `previous_day2` -- it is a floor for *availability*,
# not a promise of steady coverage past it (see the UKMO note below).
#
# * ncep_nbm_conus: reuses `archive.NBM_ARCHIVE_USABLE_START` (2024-12-01)
#   verbatim -- that date already accounts for the fill-value/plateau period
#   archive.py found in NBM's first weeks, not just null-onset.
# * ecmwf_ifs025, gfs_seamless, icon_seamless: full 24/24 coverage on both
#   leads at every date probed back to 2023-01-01 (gfs_seamless further, to
#   2022-01-01). Not bisected earlier -- none of these gates the combined
#   dataset, since NBM/AIFS start much later and CLI truth data for our
#   stations does not go back further either.
# * ecmwf_aifs025_single: null through 2025-02-15, partial (lead1 only) at
#   2025-02-18, full on both leads by 2025-02-20. Matches the research
#   survey's "between 2025-02-15 and 2025-03-01" bound.
# * ukmo_global_deterministic_10km: null through 2024-08-06, first non-null
#   hours appear 2024-08-07/08, full on both leads by 2024-08-09. **Finding,
#   not a bug**: unlike the other five models, UKMO's archive does not
#   settle into steady full coverage after this date -- biweekly probes
#   through mid-2025 kept finding whole days with one or both leads entirely
#   null (e.g. 2024-12-19, 2025-03-27, 2025-05-22), seemingly at random. This
#   usable-start date is therefore only a floor below which UKMO is *never*
#   available, not a guarantee it is available after -- treat
#   `n_models_lead1`/`n_models_lead2` as the real per-day coverage signal for
#   this model, not this date.
MODEL_ARCHIVE_USABLE_START: dict[str, date] = {
    "ncep_nbm_conus": NBM_ARCHIVE_USABLE_START,
    "ecmwf_ifs025": date(2023, 1, 1),
    "ecmwf_aifs025_single": date(2025, 2, 20),
    "gfs_seamless": date(2022, 1, 1),
    "icon_seamless": date(2023, 1, 1),
    "ukmo_global_deterministic_10km": date(2024, 8, 9),
}


# --- 1. Per-model previous-runs forecast archive -------------------------------

def _fetch_previous_runs_chunk(
    session: requests.Session, model_id: str, lat: float, lon: float, chunk_start: date, chunk_end: date
) -> dict:
    """GET one previous-runs-api chunk for `model_id` and return the parsed JSON body."""
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "temperature_2m_previous_day1,temperature_2m_previous_day2",
        "models": model_id,
        "temperature_unit": "fahrenheit",
        "timezone": "GMT",
        "start_date": chunk_start.isoformat(),
        "end_date": chunk_end.isoformat(),
    }
    resp = session.get(PREVIOUS_RUNS_URL, params=params, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def fetch_model_previous_runs(
    model_id: str,
    station_code: str,
    start_date: str | date,
    end_date: str | date,
    *,
    session: requests.Session | None = None,
    use_cache: bool = True,
    today: date | None = None,
) -> list[dict]:
    """Fetch hourly fixed-lead forecasts for `model_id` at `station_code`, [start_date, end_date].

    Same shape and caching/chunking behavior as
    :func:`src.data.weather.archive.fetch_nbm_previous_runs`, generalised
    over `model_id`. Requests are chunked to at most
    `PREVIOUS_RUNS_CHUNK_DAYS` days each, with a polite sleep between real
    network calls. A chunk whose end date is on or after `today` (the
    current, still-incomplete month) is always re-fetched; earlier,
    completed chunks are cached on disk forever, keyed by
    (`model_id`, `station_code`, chunk range) so different models never
    collide in cache.

    Args:
        model_id: One of `MODEL_IDS` (not validated against that tuple --
            any Open-Meteo model id the endpoint accepts is accepted here).
        station_code: Key into `src.data.weather.station_map.STATIONS`.
        start_date: ISO date string or `date`, inclusive.
        end_date: ISO date string or `date`, inclusive.
        session: Optional `requests.Session` (for tests); defaults to the
            module-level session shared with `archive.py`.
        use_cache: If `False`, always hit the network (and still write the
            cache). If `True` (default), a completed chunk is served from
            cache when present.
        today: Optional override of "today" for cache-currency decisions
            (for tests); defaults to `date.today()`.

    Returns:
        List of `{valid_utc, lead1_f, lead2_f}` dicts, one per hour, ordered
        by `valid_utc`. `lead1_f`/`lead2_f` are `None` where this model has
        no archive data yet (e.g. before its `MODEL_ARCHIVE_USABLE_START`).

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
            "model": model_id,
        }
        is_current = chunk_end >= ref_today
        cached = None
        if use_cache and not is_current:
            cached = cache_get("multi_model_previous_runs", cache_params, ttl_seconds=LONG_CACHE_TTL_SECONDS)
        if cached is not None:
            rows.extend(cached)
            continue

        payload = _fetch_previous_runs_chunk(sess, model_id, station["lat"], station["lon"], chunk_start, chunk_end)
        parsed = _parse_previous_runs_payload(payload)
        if use_cache and not is_current:
            cache_set("multi_model_previous_runs", cache_params, parsed)
        rows.extend(parsed)
        time.sleep(SLEEP_SECONDS)

    return rows


# --- 2. Pure dataset assembly ---------------------------------------------------

def build_multi_model_dataset(
    station_code: str,
    forecasts_by_model: dict[str, list[dict]],
    cli_rows: list[dict],
    utc_offset_hours: int,
) -> list[dict]:
    """Assemble one row per local-standard day across all models. Pure, no I/O.

    Mirrors `archive.build_daily_dataset`'s per-day, per-lead assembly (24
    hourly instants per lead, a lead complete only if all 24 are present and
    finite, `is_degenerate_series` catching Open-Meteo's constant-fill-value
    leads), independently for each model in `forecasts_by_model`, then folds
    the per-model maxima into simple ensemble statistics.

    A degenerate lead (fill value, e.g. the 32.0F/0C constant Open-Meteo
    returns for an uncovered lead in NBM's early archive) or an incomplete
    lead (missing hours, including the "not yet available" case of querying
    a model before its own archive coverage begins) contributes `None` for
    that model/lead and is excluded from that day's ensemble aggregates --
    never silently averaged in, and never imputed.

    Args:
        station_code: Station ticker, recorded verbatim in each output row.
        forecasts_by_model: `{model_id: rows}`, where `rows` has the shape
            returned by `fetch_model_previous_runs` (`valid_utc`, `lead1_f`,
            `lead2_f`). Any subset of `MODEL_IDS` may be present; a missing
            model is treated the same as one with no rows at all.
        cli_rows: Rows from `archive.fetch_cli_daily_highs` (`date_lst`,
            `max_f`).
        utc_offset_hours: Fixed local-standard UTC offset, in hours (see
            `archive.STATION_STANDARD_UTC_OFFSET_HOURS`).

    Returns:
        List of dicts, sorted by `date_lst`. **Frozen output schema** (do
        not rename a key -- another module consumes this shape)::

            {
              "station": str,
              "date_lst": "YYYY-MM-DD",
              "models": {
                  model_id: {
                      "forecast_max_lead1_f": float | None,
                      "forecast_max_lead2_f": float | None,
                      "degenerate_lead1": bool,
                      "degenerate_lead2": bool,
                  },
                  ...
              },
              "ensemble_mean_lead1_f": float | None,
              "ensemble_spread_lead1_f": float | None,  # population stdev
              "ensemble_min_lead1_f": float | None,
              "ensemble_max_lead1_f": float | None,
              "n_models_lead1": int,
              "ensemble_mean_lead2_f": float | None,
              "ensemble_spread_lead2_f": float | None,  # population stdev
              "n_models_lead2": int,
              "observed_max_f": float | None,
              "complete": bool,
              "reason": str | None,
            }

        `ensemble_*_lead1_f`/`n_models_lead1` (and the `lead2` equivalents)
        are computed only from models with a complete, non-degenerate lead
        that day; `ensemble_spread_*_f` is `None` when fewer than 2 models
        contributed (population stdev of 1 point is meaningless). `complete`
        is `True` only when at least one model contributed a lead1 value
        *and* an observed CLI max exists -- it does not require every model
        to be present; `n_models_lead1` is the signal for how much of the
        ensemble was actually available.
    """
    offset_td = timedelta(hours=utc_offset_hours)

    lead1_by_model: dict[str, dict[datetime, float | None]] = {}
    lead2_by_model: dict[str, dict[datetime, float | None]] = {}
    candidate_dates: set[date] = set()

    all_model_ids = sorted(set(MODEL_IDS) | set(forecasts_by_model))
    for model_id in all_model_ids:
        l1: dict[datetime, float | None] = {}
        l2: dict[datetime, float | None] = {}
        for row in forecasts_by_model.get(model_id, []):
            valid = _parse_utc(row["valid_utc"])
            l1[valid] = row.get("lead1_f")
            l2[valid] = row.get("lead2_f")
            candidate_dates.add((valid + offset_td).date())
        lead1_by_model[model_id] = l1
        lead2_by_model[model_id] = l2

    cli_by_date: dict[date, dict] = {}
    for row in cli_rows:
        d = _as_date(row["date_lst"])
        cli_by_date[d] = row
        candidate_dates.add(d)

    results = []
    for target in sorted(candidate_dates):
        start = _local_day_start(target, utc_offset_hours)
        needed = [start + timedelta(hours=h) for h in range(24)]

        models_out: dict[str, dict] = {}
        lead1_values: list[float] = []
        lead2_values: list[float] = []
        degenerate_reasons: list[str] = []

        for model_id in all_model_ids:
            hourly_lead1 = [lead1_by_model[model_id].get(h) for h in needed]
            hourly_lead2 = [lead2_by_model[model_id].get(h) for h in needed]

            lead1_degenerate = is_degenerate_series(hourly_lead1)
            lead2_degenerate = is_degenerate_series(hourly_lead2)
            lead1_complete = all(_finite(v) for v in hourly_lead1) and not lead1_degenerate
            lead2_complete = all(_finite(v) for v in hourly_lead2) and not lead2_degenerate

            forecast_max_lead1 = max(hourly_lead1) if lead1_complete else None
            forecast_max_lead2 = max(hourly_lead2) if lead2_complete else None

            models_out[model_id] = {
                "forecast_max_lead1_f": forecast_max_lead1,
                "forecast_max_lead2_f": forecast_max_lead2,
                "degenerate_lead1": lead1_degenerate,
                "degenerate_lead2": lead2_degenerate,
            }

            if forecast_max_lead1 is not None:
                lead1_values.append(forecast_max_lead1)
            if forecast_max_lead2 is not None:
                lead2_values.append(forecast_max_lead2)

            if lead1_degenerate:
                degenerate_reasons.append(f"{model_id} lead1 constant (fill value, not a forecast)")
            if lead2_degenerate:
                degenerate_reasons.append(f"{model_id} lead2 constant (fill value, not a forecast)")

        n_models_lead1 = len(lead1_values)
        n_models_lead2 = len(lead2_values)

        ensemble_mean_lead1 = statistics.mean(lead1_values) if lead1_values else None
        ensemble_spread_lead1 = statistics.pstdev(lead1_values) if n_models_lead1 >= 2 else None
        ensemble_min_lead1 = min(lead1_values) if lead1_values else None
        ensemble_max_lead1 = max(lead1_values) if lead1_values else None

        ensemble_mean_lead2 = statistics.mean(lead2_values) if lead2_values else None
        ensemble_spread_lead2 = statistics.pstdev(lead2_values) if n_models_lead2 >= 2 else None

        cli_row = cli_by_date.get(target)
        observed_max = cli_row["max_f"] if cli_row is not None else None

        reasons = list(degenerate_reasons)
        if n_models_lead1 == 0:
            reasons.append("no model produced a complete lead1")
        if cli_row is None:
            reasons.append("no CLI observation for date")
        elif observed_max is None:
            reasons.append("CLI observed max missing (MM)")

        complete = n_models_lead1 > 0 and observed_max is not None

        results.append(
            {
                "station": station_code.upper(),
                "date_lst": target.isoformat(),
                "models": models_out,
                "ensemble_mean_lead1_f": ensemble_mean_lead1,
                "ensemble_spread_lead1_f": ensemble_spread_lead1,
                "ensemble_min_lead1_f": ensemble_min_lead1,
                "ensemble_max_lead1_f": ensemble_max_lead1,
                "n_models_lead1": n_models_lead1,
                "ensemble_mean_lead2_f": ensemble_mean_lead2,
                "ensemble_spread_lead2_f": ensemble_spread_lead2,
                "n_models_lead2": n_models_lead2,
                "observed_max_f": observed_max,
                "complete": complete,
                "reason": "; ".join(reasons) if reasons else None,
            }
        )

    return results


# --- 3. Pure error summary (train/test, no leakage) -----------------------------

_SUMMARY_ENTITIES_LEAD_FIELDS = (("lead1", "forecast_max_lead1_f"), ("lead2", "forecast_max_lead2_f"))
_ENSEMBLE_LEAD_FIELDS = (("lead1", "ensemble_mean_lead1_f"), ("lead2", "ensemble_mean_lead2_f"))

ENSEMBLE_KEY = "ensemble_mean"


def summarize_multi_model_errors(rows: list[dict], train_end_date: str | date) -> dict:
    """Summarize per-model and ensemble-mean coverage/MAE vs CLI truth, train vs. test. Pure, no I/O.

    Only rows with a CLI `observed_max_f` count toward a slice's `n_days`
    denominator (used for `coverage_pct`); within that, a model/entity's MAE
    is computed only over the subset of those days where it actually
    produced a value for that lead -- a missing model-day is excluded, never
    treated as a zero error. Rows with `date_lst` on or before
    `train_end_date` are "train"; everything after is "test", mirroring
    `archive.summarize_errors`.

    Args:
        rows: Rows from `build_multi_model_dataset` (any subset of
            stations).
        train_end_date: ISO date string or `date`; the last date included in
            the train slice.

    Returns:
        `{"train": {...}, "test": {...}}`, each mapping:
        `"n_days"` (int, rows with an observed max in that slice), each
        `model_id` in `MODEL_IDS` to `{"lead1": {n, coverage_pct, mae_f},
        "lead2": {...}}`, and `"ensemble_mean"` to the same shape computed
        against `ensemble_mean_lead{1,2}_f`.
    """
    train_end = _as_date(train_end_date)
    entities = list(MODEL_IDS) + [ENSEMBLE_KEY]
    abs_errors: dict[str, dict[str, dict[str, list[float]]]] = {
        "train": {e: {"lead1": [], "lead2": []} for e in entities},
        "test": {e: {"lead1": [], "lead2": []} for e in entities},
    }
    n_days = {"train": 0, "test": 0}

    for row in rows:
        target = _as_date(row["date_lst"])
        slice_name = "train" if target <= train_end else "test"
        observed = row.get("observed_max_f")
        if observed is None:
            continue
        n_days[slice_name] += 1

        models = row.get("models", {})
        for model_id in MODEL_IDS:
            model_row = models.get(model_id, {})
            for lead_key, field in _SUMMARY_ENTITIES_LEAD_FIELDS:
                forecast = model_row.get(field)
                if forecast is None:
                    continue
                abs_errors[slice_name][model_id][lead_key].append(abs(observed - forecast))

        for lead_key, field in _ENSEMBLE_LEAD_FIELDS:
            forecast = row.get(field)
            if forecast is None:
                continue
            abs_errors[slice_name][ENSEMBLE_KEY][lead_key].append(abs(observed - forecast))

    summary: dict = {"train": {"n_days": n_days["train"]}, "test": {"n_days": n_days["test"]}}
    for slice_name in ("train", "test"):
        total = n_days[slice_name]
        for entity in entities:
            entity_summary = {}
            for lead_key in ("lead1", "lead2"):
                errs = abs_errors[slice_name][entity][lead_key]
                n = len(errs)
                entity_summary[lead_key] = {
                    "n": n,
                    "coverage_pct": (100.0 * n / total) if total else None,
                    "mae_f": statistics.mean(errs) if errs else None,
                }
            summary[slice_name][entity] = entity_summary

    return summary


def _fmt_stat(stats: dict) -> str:
    cov = "n/a" if stats["coverage_pct"] is None else f"{stats['coverage_pct']:.1f}%"
    mae = "n/a" if stats["mae_f"] is None else f"{stats['mae_f']:.2f}F"
    return f"n={stats['n']:<5} coverage={cov:<7} mae={mae}"


def format_multi_model_summary(summary: dict) -> str:
    """Render `summarize_multi_model_errors`'s output as a human-readable report."""
    lines = []
    for slice_name in ("train", "test"):
        slice_summary = summary.get(slice_name, {})
        lines.append(f"=== {slice_name.upper()} (n_days={slice_summary.get('n_days', 0)}) ===")
        for model_id in MODEL_IDS:
            if model_id not in slice_summary:
                continue
            lines.append(f"  {model_id}")
            for lead_key in ("lead1", "lead2"):
                if lead_key in slice_summary[model_id]:
                    lines.append(f"    {lead_key:<6} {_fmt_stat(slice_summary[model_id][lead_key])}")
        if ENSEMBLE_KEY in slice_summary:
            lines.append(f"  {ENSEMBLE_KEY}")
            for lead_key in ("lead1", "lead2"):
                if lead_key in slice_summary[ENSEMBLE_KEY]:
                    lines.append(f"    {lead_key:<6} {_fmt_stat(slice_summary[ENSEMBLE_KEY][lead_key])}")
        lines.append("")
    return "\n".join(lines).rstrip("\n")


# --- 4. CLI ---------------------------------------------------------------------

DEFAULT_OUTPUT_PATH = "data/weather/multi_model_daily.jsonl"


def run_build(
    start: str,
    end: str,
    station_codes: list[str],
    output_path: str,
    model_ids: tuple[str, ...] = MODEL_IDS,
) -> list[dict]:
    """Fetch all models' forecasts + CLI highs for each station, assemble, and write JSONL.

    Args:
        start: ISO start date (inclusive).
        end: ISO end date (inclusive).
        station_codes: Station tickers to process.
        output_path: Destination JSONL path (parent dirs created as needed).
        model_ids: Models to fetch (default `MODEL_IDS`, all six).

    Returns:
        All assembled rows (all stations), sorted by station then date.
    """
    # Same one-extra-UTC-day rationale as `archive.run_build`: the local-
    # standard day for `end` needs forecast hours that run past midnight UTC.
    forecast_end = (_as_date(end) + timedelta(days=1)).isoformat()

    all_rows: list[dict] = []
    for raw_code in station_codes:
        code = raw_code.strip().upper()
        offset = STATION_STANDARD_UTC_OFFSET_HOURS[code]

        forecasts_by_model: dict[str, list[dict]] = {}
        for model_id in model_ids:
            usable_start = MODEL_ARCHIVE_USABLE_START.get(model_id)
            if usable_start is not None and _as_date(end) < usable_start:
                logger.info(
                    "%s: %s has no usable archive before %s; requested range ends %s -- skipping fetch",
                    code, model_id, usable_start, end,
                )
                forecasts_by_model[model_id] = []
                continue
            forecasts_by_model[model_id] = fetch_model_previous_runs(model_id, code, start, forecast_end)

        cli_rows = fetch_cli_daily_highs(code, start, end)
        rows = build_multi_model_dataset(code, forecasts_by_model, cli_rows, offset)
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
    """Load a JSONL dataset written by `run_build` and summarize per-model/ensemble error."""
    rows = []
    with open(input_path) as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return summarize_multi_model_errors(rows, train_end)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments for the `build`/`summarize` subcommands."""
    parser = argparse.ArgumentParser(
        prog="python -m src.data.weather.multi_model",
        description="Build and summarize the archived multi-model-forecast-vs-CLI-observed daily dataset.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    build_p = sub.add_parser(
        "build",
        help="Fetch all models' previous-runs forecasts and IEM CLI daily highs, assemble, write JSONL.",
    )
    build_p.add_argument("--start", required=True, help="ISO start date (local-standard), e.g. 2024-12-01")
    build_p.add_argument("--end", required=True, help="ISO end date (inclusive)")
    build_p.add_argument(
        "--stations",
        default=",".join(sorted(STATIONS)),
        help="Comma-separated station codes (default: all mapped stations)",
    )
    build_p.add_argument(
        "--models",
        default=",".join(MODEL_IDS),
        help="Comma-separated Open-Meteo model ids (default: all six)",
    )
    build_p.add_argument("--output", default=DEFAULT_OUTPUT_PATH, help="Output JSONL path")

    summarize_p = sub.add_parser("summarize", help="Summarize per-model/ensemble coverage and MAE, train vs. test.")
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
        models = tuple(m.strip() for m in args.models.split(",") if m.strip())
        run_build(args.start, args.end, codes, args.output, model_ids=models)
    elif args.command == "summarize":
        summary = run_summarize(args.input, args.train_end)
        print(format_multi_model_summary(summary))


if __name__ == "__main__":
    main()
