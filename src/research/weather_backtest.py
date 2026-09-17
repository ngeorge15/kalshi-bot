"""Retrospective backtest: does archived NBM guidance beat Kalshi weather-bracket prices?

Ties together three already-reviewed pieces:

* **Forecast**, without look-ahead. For a target local-standard day, the
  decision instant is ``T = local day start - 1 minute``. For each of the
  day's 24 hourly instants, NBM's ``lead1`` value was produced by a run
  initialized 24h before that hour; assuming a 2h publication-availability
  latency, ``lead1`` is usable only if ``(valid - 24h + 2h) <= T`` --
  otherwise ``lead2`` (issued 48h before, always available by T) is used
  instead. The forecast max for the day is the max of those 24 values. A
  pure ``lead2``-only variant is also supported, as a longer-lead comparison.
  Both read hourly ``lead1``/``lead2`` from
  :func:`src.data.weather.archive.fetch_nbm_previous_runs` (cache-backed),
  not the archive's precomputed per-lead daily max, because the guarded
  variant mixes leads within a single day.

* **Probability**, via :func:`src.paper.weather.bracket_probability`:
  ``observed_max ~ Normal(forecast_max + bias, sigma)`` per station, with
  ``bias``/``sigma`` fit from archived (forecast, observed) pairs on a
  TRAIN-only date range (:func:`fit_bias_sigma`; refuses to overlap the
  evaluation range).

* **Prices**, via :mod:`src.data.kalshi_history` -- each bracket's quote at
  T, historical/live partition and all.

**This backtest is not the deployed model.** ``predict_hourly_high`` consumes
a forecaster-edited NDFD/NWS-hourly snapshot; this backtest consumes archived
NBM guidance, a related but distinct input (see
``research/historical-forecasts.md``'s model-mismatch discussion). Treat any
favorable result here as informative about an NBM-driven strategy, not a
validated backtest of the exact live baseline.

**Guardrails against silent leakage / post-hoc analysis:**

* :data:`TRAIN_END` marks the last date any evaluation may reach without a
  verified, predeclared :mod:`src.paper.protocol` naming this backtest.
* :data:`SETTLEMENT_SOURCE_SWITCH_DATE` marks when Kalshi's own settlement
  source changed underneath this series (NWS CLI -> The Weather Company,
  see ``research/kalshi-public-data.md``); evaluating past it requires an
  explicit opt-in, since a model fit on CLI-truth data has not been checked
  against the new source.

Usage::

    from src.research.weather_backtest import run_backtest

    report = run_backtest(
        station_codes=["KNYC", "KMDW", "KMIA", "KAUS"],
        variant="lead1_guarded",
        train_start="2024-11-01", train_end="2025-06-30",
        eval_start="2025-07-01", eval_end="2025-12-31",
        min_edge=0.05,
    )
"""
from __future__ import annotations

import argparse
import json
import logging
import statistics
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import numpy as np

from src.data.kalshi_history import (
    SERIES_STATIONS,
    STATION_STANDARD_UTC_OFFSET_HOURS,
    fetch_event_at_decision,
)
from src.data.weather.archive import fetch_cli_daily_highs, fetch_nbm_previous_runs
from src.paper import protocol as protocol_mod
from src.paper.fees import trading_fee_cents
from src.paper.uncertainty import (
    DEFAULT_CONFIDENCE,
    DEFAULT_N_RESAMPLES,
    DEFAULT_SEED,
    cluster_bootstrap_ci,
    paired_brier_by_event,
)
from src.paper.weather import bracket_probability

logger = logging.getLogger(__name__)

STATION_TO_SERIES: dict[str, str] = {station: series for series, station in SERIES_STATIONS.items()}

VARIANTS = ("lead1_guarded", "lead2")

# Assumed publication-availability latency for an NBM previous-runs value:
# a run initialized at (valid - 24h) is not actually retrievable until 2h
# later. Not independently verified against Open-Meteo's real latency (see
# research/historical-forecasts.md caveat 2); a documented assumption.
NBM_AVAILABILITY_LATENCY_HOURS = 2.0

# No evaluation may extend past this date without a verified, predeclared
# protocol naming this backtest (see module docstring and
# `_enforce_period_guards`).
TRAIN_END = date(2025, 12, 31)

# Kalshi's settlement source for KXHIGH* switched from NWS CLI to "The
# Weather Company" between KXHIGHNY-26AUG13 and 26AUG16 (see
# research/kalshi-public-data.md). A model fit on CLI-truth data is not
# validated against the new source without an explicit check.
SETTLEMENT_SOURCE_SWITCH_DATE = date(2026, 8, 13)

# A saved Protocol's `hypothesis` text must contain this literal marker for
# `_enforce_period_guards` to accept it as governing THIS backtest, rather
# than some unrelated predeclared experiment that happens to verify fine.
PROTOCOL_HYPOTHESIS_MARKER = "weather_backtest"

# This backtest replays historical market data; only a "replay" protocol
# (src.paper.protocol's run_kind enum) can govern it.
REQUIRED_PROTOCOL_RUN_KIND = "replay"

DEFAULT_CONTRACTS_PER_TRADE = 10
DEFAULT_FEE_TYPE = "quadratic"
DEFAULT_FEE_MULTIPLIER = 1.0

# How far a bracket event's YES-probability sum may drift from 1.0 before
# it's flagged as a partition diagnostic (floating point + sigma/bias
# mis-fit noise, not a hard assertion -- one bad event shouldn't abort a
# whole backtest run).
PARTITION_TOLERANCE = 0.02

DEFAULT_CALIBRATION_BINS = 10


# --- Small local date/time helpers (duplicated across this repo's data
# modules by convention -- see src/data/weather/archive.py's own comment) --

def _as_date(value: str | date) -> date:
    return value if isinstance(value, date) else date.fromisoformat(value)


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _local_day_start(target: date, utc_offset_hours: int) -> datetime:
    return datetime.combine(
        target, datetime.min.time(), timezone(timedelta(hours=utc_offset_hours))
    ).astimezone(timezone.utc)


def _date_range(start: date, end: date) -> Iterator[date]:
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def _finite(value: object) -> bool:
    import math
    return (
        value is not None
        and not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
    )


# --- 1. Forecast, without look-ahead --------------------------------------------

class MissingForecastError(ValueError):
    """Raised when a needed hour's forecast value is unavailable under `variant`'s rule."""


def hourly_forecast_frame(
    station_code: str, target_date: str | date, session: Any = None
) -> dict[datetime, dict[str, float | None]]:
    """Hourly NBM lead1/lead2 forecasts covering `target_date`'s 24 local-standard hours.

    Pads the fetch window by a day on each side so the local day's 24 UTC
    hourly instants (which can spill into an adjacent UTC calendar date for
    negative offsets) are fully covered. Cache-backed via
    `fetch_nbm_previous_runs`; calling this repeatedly for overlapping date
    ranges makes no new network calls beyond the first.

    Returns:
        `{valid_utc_datetime: {"lead1": float | None, "lead2": float | None}}`.
    """
    day = _as_date(target_date)
    rows = fetch_nbm_previous_runs(
        station_code, day - timedelta(days=1), day + timedelta(days=1), session=session
    )
    return {
        _parse_utc(row["valid_utc"]): {"lead1": row.get("lead1_f"), "lead2": row.get("lead2_f")}
        for row in rows
    }


def select_forecast_values(
    variant: str,
    hourly: dict[datetime, dict[str, float | None]],
    needed_valid_times: list[datetime],
    decision_time: datetime,
    latency_hours: float = NBM_AVAILABILITY_LATENCY_HOURS,
) -> list[float]:
    """Pick one forecast value per hour in `needed_valid_times`, per `variant`'s rule.

    `"lead1_guarded"`: for each hour, use `lead1` only if its run would have
    been available by `decision_time` (`valid - 24h + latency_hours <=
    decision_time`); otherwise fall back to `lead2`. `"lead2"`: always use
    `lead2`.

    Args:
        variant: One of `VARIANTS`.
        hourly: Output of `hourly_forecast_frame`.
        needed_valid_times: The 24 hourly UTC instants of the target day.
        decision_time: T -- the instant no forecast used here may exceed in
            "would have been known by" terms.
        latency_hours: Assumed publication latency for a `lead1` value.

    Returns:
        One value per entry in `needed_valid_times`, same order.

    Raises:
        ValueError: If `variant` is not recognized.
        MissingForecastError: If some hour has no usable value under the rule.
    """
    if variant not in VARIANTS:
        raise ValueError(f"Unknown variant {variant!r}; expected one of {VARIANTS}")
    values: list[float] = []
    for valid in needed_valid_times:
        entry = hourly.get(valid, {})
        if variant == "lead2":
            value = entry.get("lead2")
            if not _finite(value):
                raise MissingForecastError(f"lead2 missing for hour {valid.isoformat()}")
            values.append(value)
            continue
        # lead1_guarded
        lead1_available_at = valid - timedelta(hours=24) + timedelta(hours=latency_hours)
        value = entry.get("lead1") if lead1_available_at <= decision_time else None
        if not _finite(value):
            value = entry.get("lead2")
        if not _finite(value):
            raise MissingForecastError(
                f"Neither a timely lead1 nor lead2 is available for hour {valid.isoformat()}"
            )
        values.append(value)
    return values


def forecast_max_for_day(
    station_code: str,
    target_date: str | date,
    variant: str,
    session: Any = None,
    decision_time: datetime | None = None,
) -> float:
    """The day's forecast max under `variant`'s no-look-ahead rule.

    Args:
        station_code: e.g. `"KNYC"`.
        target_date: The local-standard target date.
        variant: One of `VARIANTS`.
        session: Optional `requests.Session` (tests), threaded through to
            `fetch_nbm_previous_runs`.
        decision_time: Override T (tests); defaults to the target day's
            local-standard start minus 1 minute.

    Returns:
        The max of the 24 selected hourly values.

    Raises:
        MissingForecastError: If any needed hour has no usable value.
    """
    day = _as_date(target_date)
    offset = STATION_STANDARD_UTC_OFFSET_HOURS[station_code]
    start = _local_day_start(day, offset)
    needed = [start + timedelta(hours=h) for h in range(24)]
    decision = decision_time if decision_time is not None else start - timedelta(minutes=1)
    hourly = hourly_forecast_frame(station_code, day, session=session)
    return max(select_forecast_values(variant, hourly, needed, decision))


# --- 2. Bias/sigma fit, train-only -----------------------------------------------

@dataclass(frozen=True)
class StationFit:
    station: str
    bias_f: float
    sigma_f: float
    n: int


def build_forecast_observed_rows(
    station_codes: list[str],
    start_date: str | date,
    end_date: str | date,
    variant: str,
    session: Any = None,
) -> list[dict]:
    """One row per (station, date) with `forecast_max_f` (per `variant`) and `observed_max_f`.

    Only I/O here is through cache-backed fetchers (`fetch_cli_daily_highs`,
    `fetch_nbm_previous_runs` via `forecast_max_for_day`); a day missing
    either an observation or a complete forecast under `variant`'s rule is
    silently excluded (not padded with a guess).
    """
    start, end = _as_date(start_date), _as_date(end_date)
    if start > end:
        raise ValueError(f"start_date {start} must be on or before end_date {end}")
    rows: list[dict] = []
    for station in station_codes:
        cli_rows = fetch_cli_daily_highs(station, start, end, session=session)
        observed_by_date = {row["date_lst"]: row["max_f"] for row in cli_rows if row["max_f"] is not None}
        for day in _date_range(start, end):
            date_lst = day.isoformat()
            observed = observed_by_date.get(date_lst)
            if observed is None:
                continue
            try:
                forecast_max = forecast_max_for_day(station, day, variant, session=session)
            except MissingForecastError:
                continue
            rows.append({
                "station": station, "date_lst": date_lst,
                "forecast_max_f": forecast_max, "observed_max_f": observed,
            })
    return rows


def fit_bias_sigma(
    station_codes: list[str],
    variant: str,
    train_start: str | date,
    train_end: str | date,
    eval_start: str | date | None = None,
    eval_end: str | date | None = None,
    session: Any = None,
) -> dict[str, StationFit]:
    """Fit per-station bias/sigma of `(observed - forecast)` on `[train_start, train_end]` only.

    Args:
        station_codes: Stations to fit.
        variant: One of `VARIANTS`; must match the variant used at eval time.
        train_start, train_end: The TRAIN date range (inclusive).
        eval_start, eval_end: If given (both required together), the
            evaluation date range this fit will be used against -- checked
            for zero overlap with the train range.
        session: Optional `requests.Session` (tests).

    Returns:
        `{station_code: StationFit}`. A station with fewer than 2 usable
        training days is omitted (a sample std-dev needs n >= 2).

    Raises:
        ValueError: If `train_start > train_end`, if only one of `eval_start`/
            `eval_end` is given, if `eval_start > eval_end`, or if the train
            and eval ranges overlap.
    """
    train_start_d, train_end_d = _as_date(train_start), _as_date(train_end)
    if train_start_d > train_end_d:
        raise ValueError(f"train_start {train_start_d} must be on or before train_end {train_end_d}")
    if (eval_start is None) != (eval_end is None):
        raise ValueError("eval_start and eval_end must be given together, or not at all")
    if eval_start is not None:
        eval_start_d, eval_end_d = _as_date(eval_start), _as_date(eval_end)
        if eval_start_d > eval_end_d:
            raise ValueError(f"eval_start {eval_start_d} must be on or before eval_end {eval_end_d}")
        if not (train_end_d < eval_start_d or eval_end_d < train_start_d):
            raise ValueError(
                f"Train range [{train_start_d}, {train_end_d}] overlaps the evaluation range "
                f"[{eval_start_d}, {eval_end_d}]; refusing to fit on data that includes eval days"
            )
    rows = build_forecast_observed_rows(station_codes, train_start_d, train_end_d, variant, session=session)
    errors_by_station: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        errors_by_station[row["station"]].append(row["observed_max_f"] - row["forecast_max_f"])
    fits: dict[str, StationFit] = {}
    for station, errors in errors_by_station.items():
        if len(errors) < 2:
            logger.warning("Station %s has only %d training day(s); skipping fit", station, len(errors))
            continue
        fits[station] = StationFit(
            station=station, bias_f=statistics.mean(errors), sigma_f=statistics.stdev(errors), n=len(errors)
        )
    return fits


# --- 3. Trading rule: EV, fees, position cap, settlement ------------------------

def evaluate_sides(
    p_yes: float, yes_bid_cents: int, yes_ask_cents: int, fee_type: str, fee_multiplier: float
) -> dict[str, dict | None]:
    """Per-contract EV (in cents, fee-inclusive) for the YES and NO side of one bracket.

    YES costs `yes_ask_cents`; NO costs `100 - yes_bid_cents` (buying NO is
    selling YES at the bid). A side is `None` when its cost falls outside
    the tradeable `[1, 99]` cent range (a 0 or 100 cent quote is not a real
    fill price).

    Args:
        p_yes: Model P(bracket resolves YES).
        yes_bid_cents, yes_ask_cents: The bracket's quote at T, in cents.
        fee_type, fee_multiplier: Passed to `trading_fee_cents`.

    Returns:
        `{"yes": {"cost_cents", "fee_cents", "ev_cents"} | None, "no": {...} | None}`.
    """
    yes_cost, no_cost = yes_ask_cents, 100 - yes_bid_cents
    result: dict[str, dict | None] = {"yes": None, "no": None}
    if 1 <= yes_cost <= 99:
        fee = trading_fee_cents(yes_cost, 1, fee_type=fee_type, fee_multiplier=fee_multiplier, is_maker=False)
        result["yes"] = {"cost_cents": yes_cost, "fee_cents": fee, "ev_cents": p_yes * 100 - yes_cost - fee}
    if 1 <= no_cost <= 99:
        fee = trading_fee_cents(no_cost, 1, fee_type=fee_type, fee_multiplier=fee_multiplier, is_maker=False)
        result["no"] = {"cost_cents": no_cost, "fee_cents": fee, "ev_cents": (1 - p_yes) * 100 - no_cost - fee}
    return result


def select_top_trades_for_event(candidates: list[dict], max_positions_per_event: int) -> list[dict]:
    """The `max_positions_per_event` highest-EV candidates for one event, ties broken by input order."""
    return sorted(candidates, key=lambda c: c["ev_cents"], reverse=True)[:max_positions_per_event]


def settle_pnl_cents(side: str, cost_cents: int, fee_cents: int, result: str) -> int:
    """Per-contract P&L in cents: `100` if `side` won, else `0`, minus cost and fee.

    Args:
        side: `"yes"` or `"no"`.
        cost_cents, fee_cents: From `evaluate_sides`.
        result: The market's settlement, `"yes"` or `"no"`.
    """
    outcome_cents = 100 if result == side else 0
    return outcome_cents - cost_cents - fee_cents


# --- 4. Metrics -------------------------------------------------------------------

def event_clustered_bootstrap_ci(
    values_by_event: dict[str, float],
    n_resamples: int = DEFAULT_N_RESAMPLES,
    confidence: float = DEFAULT_CONFIDENCE,
    seed: int = DEFAULT_SEED,
) -> dict[str, Any]:
    """Bootstrap a CI for a per-event scalar (e.g. total P&L) by resampling whole events.

    Mirrors `src.paper.uncertainty.cluster_bootstrap_ci`'s method (resample
    event indices with replacement, percentile CI) rather than calling it
    directly: that module validates every value into `[0, 1]` as a Brier
    score, which P&L in cents is not.
    """
    n_events = len(values_by_event)
    base = {"n_events": n_events, "n_resamples": n_resamples, "confidence": confidence}
    if n_events == 0:
        return {"estimate": None, "ci_low": None, "ci_high": None, **base}
    values = np.array(list(values_by_event.values()), dtype=float)
    point_estimate = float(values.mean())
    if n_events < 2:
        return {"estimate": point_estimate, "ci_low": None, "ci_high": None, **base}
    rng = np.random.default_rng(seed)
    resample_indices = rng.integers(0, n_events, size=(n_resamples, n_events))
    resample_means = values[resample_indices].mean(axis=1)
    alpha = 1 - confidence
    ci_low, ci_high = np.percentile(resample_means, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    return {"estimate": point_estimate, "ci_low": float(ci_low), "ci_high": float(ci_high), **base}


def calibration_bins(rows: list[dict], n_bins: int = DEFAULT_CALIBRATION_BINS) -> list[dict]:
    """Bucket `rows` (each `{"p", "outcome"}`) into `n_bins` equal-width predicted-probability bins."""
    buckets: list[list[dict]] = [[] for _ in range(n_bins)]
    for row in rows:
        p = min(max(row["p"], 0.0), 1.0)
        index = min(int(p * n_bins), n_bins - 1)
        buckets[index].append(row)
    result = []
    for i, bucket in enumerate(buckets):
        entry = {"bin_low": i / n_bins, "bin_high": (i + 1) / n_bins, "n": len(bucket)}
        if bucket:
            entry["mean_predicted_p"] = sum(r["p"] for r in bucket) / len(bucket)
            entry["empirical_frequency"] = sum(r["outcome"] for r in bucket) / len(bucket)
        else:
            entry["mean_predicted_p"] = None
            entry["empirical_frequency"] = None
        result.append(entry)
    return result


def max_drawdown_cents(trades: list[dict]) -> float:
    """Max peak-to-trough drawdown of cumulative P&L, trades ordered by (date, event, ticker, side)."""
    ordered = sorted(trades, key=lambda t: (t["date_lst"], t["event_key"], t["ticker"], t["side"]))
    cumulative = 0.0
    peak = 0.0
    worst = 0.0
    for trade in ordered:
        cumulative += trade["pnl_cents"]
        peak = max(peak, cumulative)
        worst = min(worst, cumulative - peak)
    return worst


# --- 5. Guardrails: test-period and settlement-source ----------------------------

def _enforce_period_guards(
    eval_end: date,
    protocol_path: str | Path | None,
    allow_weather_company_settlement: bool,
) -> protocol_mod.Protocol | None:
    """Refuse an evaluation window this backtest is not allowed to run, per module docstring.

    Returns:
        The verified `Protocol`, if one was required and supplied; `None`
        if `eval_end <= TRAIN_END` (no protocol required).

    Raises:
        ValueError: If `eval_end` is past `SETTLEMENT_SOURCE_SWITCH_DATE`
            without `allow_weather_company_settlement=True`; if `eval_end`
            is past `TRAIN_END` without a `protocol_path`; or if the given
            protocol fails verification, has the wrong `run_kind`, or its
            `hypothesis` does not reference this backtest.
    """
    if eval_end > SETTLEMENT_SOURCE_SWITCH_DATE and not allow_weather_company_settlement:
        raise ValueError(
            f"eval_end {eval_end} is after the settlement-source switch "
            f"({SETTLEMENT_SOURCE_SWITCH_DATE}, NWS CLI -> The Weather Company); pass "
            "allow_weather_company_settlement=True only after confirming settled values "
            "from the new source agree with NWS CLI for this station."
        )
    if eval_end <= TRAIN_END:
        return None
    if protocol_path is None:
        raise ValueError(
            f"eval_end {eval_end} is after TRAIN_END ({TRAIN_END}); evaluating past it requires "
            "a predeclared, verified --protocol. None was given."
        )
    try:
        declared = protocol_mod.load(protocol_path)
    except (FileNotFoundError, OSError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"Could not load a protocol from {protocol_path}: {exc}") from exc
    verification = protocol_mod.verify(declared, protocol_path)
    if not verification.valid:
        raise ValueError(f"Protocol at {protocol_path} failed verification: {verification.message}")
    if declared.run_kind != REQUIRED_PROTOCOL_RUN_KIND:
        raise ValueError(
            f"Protocol at {protocol_path} has run_kind {declared.run_kind!r}; this backtest "
            f"requires {REQUIRED_PROTOCOL_RUN_KIND!r} (it replays historical market data)."
        )
    if PROTOCOL_HYPOTHESIS_MARKER not in declared.hypothesis:
        raise ValueError(
            f"Protocol at {protocol_path}'s hypothesis does not contain the required marker "
            f"{PROTOCOL_HYPOTHESIS_MARKER!r}; it does not verifiably govern this backtest: "
            f"{declared.hypothesis!r}"
        )
    return declared


# --- 6. Orchestration --------------------------------------------------------------

def run_backtest(
    station_codes: list[str],
    variant: str,
    train_start: str | date,
    train_end: str | date,
    eval_start: str | date,
    eval_end: str | date,
    min_edge: float,
    max_positions_per_event: int = 1,
    contracts: int = DEFAULT_CONTRACTS_PER_TRADE,
    fee_type: str = DEFAULT_FEE_TYPE,
    fee_multiplier: float = DEFAULT_FEE_MULTIPLIER,
    protocol_path: str | Path | None = None,
    allow_weather_company_settlement: bool = False,
    session: Any = None,
    n_resamples: int = DEFAULT_N_RESAMPLES,
    confidence: float = DEFAULT_CONFIDENCE,
    seed: int = DEFAULT_SEED,
) -> dict[str, Any]:
    """Fit on TRAIN, trade on EVAL, report metrics. See module docstring for the full pipeline.

    Args:
        station_codes: e.g. `["KNYC", "KMDW", "KMIA", "KAUS"]`.
        variant: One of `VARIANTS`.
        train_start, train_end: TRAIN date range for `fit_bias_sigma`.
        eval_start, eval_end: Evaluation date range to trade over.
        min_edge: Minimum `ev_cents / 100` required to take a position.
        max_positions_per_event: At most this many highest-EV side/bracket
            positions per event.
        contracts: Fixed contract count per trade (no depth data available
            from candlesticks; a documented sizing assumption).
        fee_type, fee_multiplier: Passed to `trading_fee_cents`.
        protocol_path, allow_weather_company_settlement: See
            `_enforce_period_guards`.
        session: Optional `requests.Session` (tests), threaded through to
            every fetch.
        n_resamples, confidence, seed: Bootstrap parameters, shared across
            the P&L and Brier confidence intervals.

    Returns:
        A JSON-serializable (allow_nan=False safe) report dict.

    Raises:
        ValueError: From `_enforce_period_guards`, `fit_bias_sigma`, or an
            invalid `variant`/date range.
    """
    if variant not in VARIANTS:
        raise ValueError(f"Unknown variant {variant!r}; expected one of {VARIANTS}")
    eval_start_d, eval_end_d = _as_date(eval_start), _as_date(eval_end)
    if eval_start_d > eval_end_d:
        raise ValueError(f"eval_start {eval_start_d} must be on or before eval_end {eval_end_d}")
    verified_protocol = _enforce_period_guards(eval_end_d, protocol_path, allow_weather_company_settlement)

    fits = fit_bias_sigma(station_codes, variant, train_start, train_end, eval_start_d, eval_end_d, session=session)

    trades: list[dict] = []
    skip_reasons: Counter = Counter()
    brier_rows: list[dict] = []
    calibration_rows: list[dict] = []
    spreads: list[int] = []
    partition_failures: list[dict] = []

    for target_date in _date_range(eval_start_d, eval_end_d):
        for station in station_codes:
            fit = fits.get(station)
            if fit is None:
                skip_reasons["no_station_fit"] += 1
                continue
            try:
                forecast_max = forecast_max_for_day(station, target_date, variant, session=session)
            except MissingForecastError:
                skip_reasons["no_forecast"] += 1
                continue
            series = STATION_TO_SERIES.get(station)
            if series is None:
                raise ValueError(f"No Kalshi series mapped for station {station!r}")
            mu, sigma = forecast_max + fit.bias_f, fit.sigma_f

            event_rows = fetch_event_at_decision(series, target_date, session=session)
            if not event_rows:
                skip_reasons["no_market_data"] += 1
                continue

            event_key = event_rows[0]["event_ticker"]
            candidates: list[dict] = []
            p_sum = 0.0
            for row in event_rows:
                p_yes = bracket_probability(mu, sigma, row["lower_bound_f"], row["upper_bound_f"])
                p_sum += p_yes
                price = row["price"]
                outcome = 1.0 if row["result"] == "yes" else (0.0 if row["result"] == "no" else None)

                if price.status != "ok":
                    skip_reasons[price.status] += 1
                    continue
                spreads.append(price.yes_ask_cents - price.yes_bid_cents)
                if outcome is not None:
                    market_mid = (price.yes_bid_cents + price.yes_ask_cents) / 200.0
                    brier_rows.append({
                        "event_key": event_key,
                        "model_brier": (p_yes - outcome) ** 2,
                        "market_brier": (market_mid - outcome) ** 2,
                    })
                    calibration_rows.append({"p": p_yes, "outcome": outcome})
                else:
                    skip_reasons["unsettled_result"] += 1
                    continue

                sides = evaluate_sides(p_yes, price.yes_bid_cents, price.yes_ask_cents, fee_type, fee_multiplier)
                for side, info in sides.items():
                    if info is None:
                        continue
                    if info["ev_cents"] / 100.0 >= min_edge:
                        candidates.append({
                            "event_key": event_key, "ticker": row["ticker"], "side": side,
                            "station": station, "date_lst": row["date_lst"], "p_yes": p_yes,
                            "cost_cents": info["cost_cents"], "fee_cents": info["fee_cents"],
                            "ev_cents": info["ev_cents"], "result": row["result"],
                        })

            if abs(p_sum - 1.0) > PARTITION_TOLERANCE:
                partition_failures.append({"event_key": event_key, "sum_p": p_sum, "n_markets": len(event_rows)})

            for chosen in select_top_trades_for_event(candidates, max_positions_per_event):
                pnl_per_contract = settle_pnl_cents(
                    chosen["side"], chosen["cost_cents"], chosen["fee_cents"], chosen["result"]
                )
                trades.append({**chosen, "contracts": contracts, "pnl_cents": pnl_per_contract * contracts})

    return _build_report(
        trades, skip_reasons, brier_rows, calibration_rows, spreads, partition_failures,
        variant=variant, min_edge=min_edge, max_positions_per_event=max_positions_per_event,
        contracts=contracts, fee_type=fee_type, fee_multiplier=fee_multiplier,
        train_start=_as_date(train_start), train_end=_as_date(train_end),
        eval_start=eval_start_d, eval_end=eval_end_d, fits=fits,
        protocol_verified=verified_protocol is not None,
        n_resamples=n_resamples, confidence=confidence, seed=seed,
    )


def _build_report(
    trades: list[dict], skip_reasons: Counter, brier_rows: list[dict], calibration_rows: list[dict],
    spreads: list[int], partition_failures: list[dict], *, variant: str, min_edge: float,
    max_positions_per_event: int, contracts: int, fee_type: str, fee_multiplier: float,
    train_start: date, train_end: date, eval_start: date, eval_end: date,
    fits: dict[str, StationFit], protocol_verified: bool, n_resamples: int, confidence: float, seed: int,
) -> dict[str, Any]:
    """Assemble the final metrics dict from a completed run's raw trade/skip/row lists."""
    n_trades = len(trades)
    n_events_traded = len({t["event_key"] for t in trades})
    wins = sum(1 for t in trades if t["pnl_cents"] > 0)
    total_pnl_cents = float(sum(t["pnl_cents"] for t in trades))
    total_cost_cents = float(sum(t["cost_cents"] * t["contracts"] for t in trades))

    pnl_by_event: dict[str, float] = defaultdict(float)
    pnl_by_station: dict[str, float] = defaultdict(float)
    pnl_by_month: dict[str, float] = defaultdict(float)
    for t in trades:
        pnl_by_event[t["event_key"]] += t["pnl_cents"]
        pnl_by_station[t["station"]] += t["pnl_cents"]
        pnl_by_month[t["date_lst"][:7]] += t["pnl_cents"]

    brier = paired_brier_by_event(brier_rows) if brier_rows else {
        "n_events": 0, "n_markets": 0, "model_brier": None, "market_brier": None,
        "brier_improvement": None, "per_event": [],
    }
    brier_ci = cluster_bootstrap_ci(brier_rows, n_resamples, confidence, seed) if brier_rows else {
        "estimate": None, "ci_low": None, "ci_high": None, "n_events": 0, "n_markets": 0,
        "n_resamples": n_resamples, "confidence": confidence,
    }

    return {
        "variant": variant, "min_edge": min_edge, "max_positions_per_event": max_positions_per_event,
        "contracts_per_trade": contracts, "fee_type": fee_type, "fee_multiplier": fee_multiplier,
        "train_start": train_start.isoformat(), "train_end": train_end.isoformat(),
        "eval_start": eval_start.isoformat(), "eval_end": eval_end.isoformat(),
        "protocol_verified": protocol_verified,
        "station_fits": {station: asdict(fit) for station, fit in fits.items()},
        "n_trades": n_trades,
        "n_events_traded": n_events_traded,
        "hit_rate": (wins / n_trades) if n_trades else None,
        "total_pnl_cents": total_pnl_cents,
        "mean_pnl_cents": (total_pnl_cents / n_trades) if n_trades else None,
        "roi_on_cost": (total_pnl_cents / total_cost_cents) if total_cost_cents else None,
        "max_drawdown_cents": max_drawdown_cents(trades) if trades else 0.0,
        "pnl_per_event_ci": event_clustered_bootstrap_ci(dict(pnl_by_event), n_resamples, confidence, seed),
        "pnl_by_station_cents": dict(pnl_by_station),
        "pnl_by_month_cents": dict(pnl_by_month),
        "paired_brier_by_event": brier,
        "paired_brier_ci": brier_ci,
        "calibration": calibration_bins(calibration_rows),
        "skip_reasons": dict(skip_reasons),
        "mean_spread_cents": (statistics.mean(spreads) if spreads else None),
        "n_partition_check_failures": len(partition_failures),
        "partition_check_failures_sample": partition_failures[:10],
        "trades": trades,
    }


# --- 7. fetch-prices: warm the cache / dump a price snapshot --------------------

def run_fetch_prices(
    station_codes: list[str],
    start_date: str | date,
    end_date: str | date,
    output_path: str | Path,
    period_interval: int = 60,
    session: Any = None,
) -> int:
    """Fetch and write one JSONL row per (event, market) in `[start_date, end_date]` for each station.

    A thin, side-effect-only wrapper around `fetch_event_at_decision`; its
    real purpose is warming `src.data.kalshi_history`'s on-disk cache and
    producing an inspectable snapshot, not defining new logic. `run_backtest`
    does not require this to have been run first -- it fetches (cache-backed)
    on demand -- but running it ahead of time makes a subsequent `run` fast
    and lets liquidity/coverage be eyeballed before committing to a backtest.

    Returns:
        Number of market-price rows written.
    """
    start, end = _as_date(start_date), _as_date(end_date)
    if start > end:
        raise ValueError(f"start_date {start} must be on or before end_date {end}")
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with out.open("w") as fh:
        for station in station_codes:
            series = STATION_TO_SERIES.get(station)
            if series is None:
                raise ValueError(f"No Kalshi series mapped for station {station!r}")
            for target_date in _date_range(start, end):
                rows = fetch_event_at_decision(series, target_date, session=session, period_interval=period_interval)
                for row in rows:
                    record = dict(row)
                    record["price"] = asdict(record["price"])
                    fh.write(json.dumps(record, default=str) + "\n")
                    count += 1
    logger.info("Wrote %d market-price rows to %s", count, out)
    return count


# --- 8. CLI -------------------------------------------------------------------

def _split_stations(value: str) -> list[str]:
    return [s.strip().upper() for s in value.split(",") if s.strip()]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m src.research.weather_backtest",
        description="Backtest archived NBM forecasts against Kalshi weather-bracket prices.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    default_stations = ",".join(sorted(STATION_TO_SERIES))

    fp = sub.add_parser("fetch-prices", help="Fetch and cache market prices at the decision instant.")
    fp.add_argument("--stations", default=default_stations, help="Comma-separated station codes")
    fp.add_argument("--start", required=True, help="ISO start date (local-standard), inclusive")
    fp.add_argument("--end", required=True, help="ISO end date, inclusive")
    fp.add_argument("--output", required=True, help="Output JSONL path")
    fp.add_argument("--period-interval", type=int, default=60, help="Candlestick minutes: 1, 60, or 1440")

    run_p = sub.add_parser("run", help="Fit on TRAIN, trade on EVAL, write a JSON report.")
    run_p.add_argument("--stations", default=default_stations, help="Comma-separated station codes")
    run_p.add_argument("--train-start", required=True)
    run_p.add_argument("--train-end", required=True)
    run_p.add_argument("--eval-start", required=True)
    run_p.add_argument("--eval-end", required=True)
    run_p.add_argument("--min-edge", type=float, required=True)
    run_p.add_argument("--variant", choices=VARIANTS, required=True)
    run_p.add_argument("--protocol", default=None, help="Path to a predeclared, saved protocol JSON")
    run_p.add_argument("--allow-weather-company-settlement", action="store_true")
    run_p.add_argument("--max-positions-per-event", type=int, default=1)
    run_p.add_argument("--contracts", type=int, default=DEFAULT_CONTRACTS_PER_TRADE)
    run_p.add_argument("--fee-type", default=DEFAULT_FEE_TYPE)
    run_p.add_argument("--fee-multiplier", type=float, default=DEFAULT_FEE_MULTIPLIER)
    run_p.add_argument("--output", required=True, help="Output JSON report path")

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args(argv)
    if args.command == "fetch-prices":
        run_fetch_prices(_split_stations(args.stations), args.start, args.end, args.output, args.period_interval)
    elif args.command == "run":
        report = run_backtest(
            _split_stations(args.stations), args.variant, args.train_start, args.train_end,
            args.eval_start, args.eval_end, args.min_edge,
            max_positions_per_event=args.max_positions_per_event, contracts=args.contracts,
            fee_type=args.fee_type, fee_multiplier=args.fee_multiplier,
            protocol_path=args.protocol,
            allow_weather_company_settlement=args.allow_weather_company_settlement,
        )
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2, allow_nan=False, default=str))
        print(f"Wrote report to {out} ({report['n_trades']} trades, {report['n_events_traded']} events traded)")


if __name__ == "__main__":
    main()
