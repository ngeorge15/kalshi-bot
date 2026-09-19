"""Retrospective test of "the intraday floor": buying NO on arithmetically-dead weather brackets.

See `research/intraday-floor.md` for the predeclared design -- every parameter
here (the safety margins, the observation latency, the minimum bid, the
contract count, the stations, the period) is fixed by that document and is
not tunable from this module's API.

**The idea.** Kalshi's daily-high markets partition the day's *maximum*
temperature into brackets. The maximum only ever goes up. Once the
temperature observed so far today has risen above a bracket's upper bound by
`safety_margin_f`, that bracket cannot contain the day's high -- its YES is
worth zero by arithmetic, not by forecast. If such a dead bracket still shows
a YES bid of `b` cents, the executable trade is to buy NO at `100 - b`
(lifting the NO ask), which settles at 100 if the bracket really is dead.

**This is not a forecasting strategy.** No NBM/NWS forecast is consumed here,
so `src.data.weather.archive.NBM_ARCHIVE_USABLE_START` does not apply. The
only external inputs are IEM ASOS hourly observations
(`src.data.weather.observations`) and Kalshi's own historical market/candle
data (`src.data.kalshi_history`).

**Kalshi history coverage (determined empirically, 2026-09-18).** All four
mapped series (`KXHIGHNY`, `KXHIGHCHI`, `KXHIGHMIA`, `KXHIGHAUS`) first have
markets starting **2024-10-24** (`fetch_event_markets` returns zero markets
for any earlier date, confirmed by binary search against the live API; no
archive-fill-value hazard analogous to NBM's first month was found -- the
first day's candles carry real bid/ask/volume data). This module does not
hard-enforce that date as a guard (unlike `NBM_ARCHIVE_USABLE_START`, which
guards against known-bad fill values); a `start_date` before it will simply
scan city-days with no market data, tallied under `skip_reasons`.

**The primary deliverable is not P&L.** It is the count and rate of brackets
that ASOS-observed data declared impossible but which nonetheless settled
YES, broken out by safety margin (see module docstring's "hazard" section in
`research/intraday-floor.md`). A non-zero rate at the headline margin (2.0F)
falsifies the strategy regardless of what the P&L says; `format_report`
prints this first and states the verdict explicitly.

**No-look-ahead, in both directions.**

* An ASOS observation is usable only via `observations.observed_max_at`,
  which enforces `OBSERVATION_LATENCY_MINUTES`.
* A market's quote is read only via `kalshi_history.price_at_instant`, which
  never selects a candle ending after the decision instant.
* A market is never evaluated before its own `open_time` (the raw field
  `parse_market` does not carry -- read directly off the raw payload here,
  the same "small deliberate duplication" this repo uses elsewhere for a
  field `kalshi_history.py`'s normalizer discards; see `maker_sim.py`'s own
  note about `yes_bid_high_cents`). This guards against exactly the kind of
  bug this repo has shipped before: reading pre-open data as a market fact.

**Deduplication.** A bracket, once dead, stays dead for the rest of the local
day and would otherwise generate a fresh "candidate" at every subsequent
hourly instant. Each (market, day) is traded **at most once** -- the first
instant at which it is both dead and shows a bid >= `MIN_BID_CENTS`. Every
later qualifying instant for the same ticker is tallied as a repeat
candidate (an availability statistic), never as additional P&L.

**Settlement truth.** A market's `result` field is trusted only when its
`status` is in `kalshi_history.KALSHI_SETTLED_STATUSES` (`"finalized"` or
`"settled"`) -- Kalshi's own settlement, never reconstructed from CLI text.

**Depth is not computable from candlestick data.** `fetch_candlesticks`
carries `volume` and `open_interest` per candle, but no bid size --
confirmed by `kalshi_history._normalize_candle`, which has no size field to
read. This module carries `volume`/`open_interest` through on every trade as
the weak proxies they are and states this as a limitation in the report
rather than substituting one for the other. Top-of-book size is available
live via `src.live.recorder`, just not retrospectively.

Usage::

    from src.research.intraday_floor import run_backtest

    report = run_backtest(
        station_codes=["KNYC", "KMDW", "KMIA", "KAUS"],
        start_date="2024-10-24", end_date="2025-12-31",
    )
"""
from __future__ import annotations

import argparse
import json
import logging
import statistics
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import requests

from src.data.weather import observations
from src.data.kalshi_history import (
    KALSHI_SETTLED_STATUSES,
    SERIES_STATIONS,
    STATION_STANDARD_UTC_OFFSET_HOURS,
    fetch_candlesticks,
    fetch_event_markets,
    get_historical_cutoff,
    parse_market,
    price_at_instant,
)
from src.data.weather.observations import OBSERVATION_LATENCY_MINUTES
from src.paper import protocol as protocol_mod
from src.paper.fees import trading_fee_cents
from src.paper.uncertainty import DEFAULT_CONFIDENCE, DEFAULT_N_RESAMPLES, DEFAULT_SEED
from src.research.weather_backtest import (
    event_clustered_bootstrap_ci,
    max_drawdown_cents,
    settle_pnl_cents,
)

logger = logging.getLogger(__name__)

STATION_TO_SERIES: dict[str, str] = {station: series for series, station in SERIES_STATIONS.items()}

# --- Predeclared parameters (research/intraday-floor.md) -- not tunable from
# the CLI or API beyond selecting which of the predeclared margins to report. --

DEFAULT_SAFETY_MARGIN_F = 2.0  # the headline, predeclared before any run
SAFETY_MARGINS_F: tuple[float, ...] = (0.0, 1.0, 2.0, 3.0)  # all four, always reported
DEFAULT_CONTRACTS_PER_TRADE = 10
MIN_BID_CENTS = 1
DEFAULT_FEE_TYPE = "quadratic"
DEFAULT_FEE_MULTIPLIER = 1.0
DEFAULT_PERIOD_INTERVAL = 1  # minutes per candle -- decision instants are hourly, but a 60-min
# candle's close can be up to 59 minutes stale relative to the decision instant itself; 1-minute
# candles avoid manufacturing false "stale" skips purely from coarse candle granularity (this is a
# no-look-ahead-neutral choice: price_at_instant still never selects a candle ending after T).
DEFAULT_LOOKBACK_HOURS = 24  # candle-fetch buffer before local day start

# No evaluation may extend past this date without a verified, predeclared
# protocol naming this backtest -- mirrors `weather_backtest.TRAIN_END` /
# `maker_sim.HARD_CUTOFF`. The 2026 holdout stays unspent.
TRAIN_END = date(2025, 12, 31)

# Determined empirically (2026-09-18, see module docstring): the first date
# any of the four mapped series has markets at all. Not hard-enforced (no
# known bad-data hazard like NBM's fill-value month was found before it);
# documented for anyone choosing a `start_date`.
KALSHI_CANDLESTICK_HISTORY_USABLE_START = date(2024, 10, 24)

PROTOCOL_HYPOTHESIS_MARKER = "intraday_floor"
REQUIRED_PROTOCOL_RUN_KIND = "replay"

# Bucket edges (minutes) for the staleness-decay breakdown: how long before
# the decision instant did the candle actually used last update. A value
# under the first edge includes small negative numbers (a candle can end up
# to `OBSERVATION_LATENCY_MINUTES` after the observation's own valid time,
# since the decision instant is valid_time + that latency).
STALENESS_BUCKET_EDGES_MINUTES: tuple[float, ...] = (15.0, 30.0, 60.0, 180.0)

DEPTH_LIMITATION_NOTE = (
    "Bid depth at the decision instant is not computable from Kalshi candlestick data: "
    "fetch_candlesticks/_normalize_candle carry only close price, volume, and open_interest "
    "per candle, never a bid size. volume and open_interest are carried through per trade "
    "below as the weak, non-substitutable proxies they are. Top-of-book size is available "
    "live via src.live.recorder, not retrospectively."
)


# --- Small local date/time helpers (duplicated across this repo's data
# modules by convention -- see src/data/weather/archive.py's own comment) --

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
    return datetime.combine(
        target, datetime.min.time(), timezone(timedelta(hours=utc_offset_hours))
    ).astimezone(timezone.utc)


def _date_range(start: date, end: date) -> Iterator[date]:
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def _margin_key(margin: float) -> str:
    return f"{margin:.1f}"


# --- 1. The pure arithmetic: which brackets are dead ------------------------

def dead_brackets_at(markets: list[dict], running_max_f: float, safety_margin_f: float) -> list[dict]:
    """The markets whose upper bound is arithmetically impossible. Pure, no I/O.

    A bracket is dead when its `upper_bound_f` is strictly below
    `running_max_f - safety_margin_f` -- i.e. the temperature already
    observed today has cleared it by more than the safety margin. A bracket
    with no upper bound (`upper_bound_f is None`, the top "greater" bracket)
    can never be dead by this arithmetic and is never returned.

    Args:
        markets: Parsed market dicts (as from `parse_market`), each with
            `upper_bound_f`.
        running_max_f: The observed running maximum usable at the decision
            instant (from `observations.observed_max_at`).
        safety_margin_f: How far past the upper bound the observation must
            already be before the bracket is called dead.

    Returns:
        The subset of `markets` that are dead, in input order.
    """
    threshold = running_max_f - safety_margin_f
    return [m for m in markets if m.get("upper_bound_f") is not None and m["upper_bound_f"] < threshold]


# --- 2. One city-day: fetch, no-look-ahead simulation across all margins ----

def _empty_day_result(station: str, day: date, skip_reason: str) -> dict[str, Any]:
    return {
        "station": station, "date_lst": day.isoformat(), "event_key": None,
        "skip_reason": skip_reason, "by_margin": {}, "bound_check": None,
    }


def _bound_check_for_day(
    markets: list[dict], running: list[dict], station: str, date_lst: str, event_key: str
) -> dict[str, Any] | None:
    """Compare the settled winning bracket's bounds against the day's final ASOS max.

    A diagnostic, not a trading decision -- uses the full day's final
    observed max (the last row of `running`), not a latency-gated decision
    instant. `None` if the day has no observations or no settled YES market
    (e.g. the event is still open, or genuinely has no winner on record).
    """
    if not running:
        return None
    final_asos_max_f = running[-1]["running_max_f"]
    winner = next(
        (m for m in markets if m["status"] in KALSHI_SETTLED_STATUSES and m["result"] == "yes"), None
    )
    if winner is None:
        return None
    upper = winner["upper_bound_f"]
    settled_high_below_asos = upper is not None and upper < final_asos_max_f
    return {
        "event_key": event_key, "station": station, "date_lst": date_lst,
        "winning_ticker": winner["ticker"], "lower_bound_f": winner["lower_bound_f"],
        "upper_bound_f": upper, "final_asos_running_max_f": final_asos_max_f,
        "settled_high_below_asos": settled_high_below_asos,
    }


def _simulate_margin(
    markets: list[dict],
    candles_by_ticker: dict[str, list[dict]],
    running: list[dict],
    *,
    margin: float,
    station: str,
    date_lst: str,
    event_key: str,
    contracts: int,
    fee_type: str,
    fee_multiplier: float,
) -> dict[str, Any]:
    """Walk one city-day's hourly instants for a single safety margin. Pure given fetched inputs.

    Returns `{"trades", "dead_records", "n_repeat_candidates", "skip_reasons"}`.
    See module docstring for the dedup and no-look-ahead rules this enforces.
    """
    trades: list[dict] = []
    dead_records: list[dict] = []
    seen_dead: set[str] = set()
    traded: set[str] = set()
    n_repeat_candidates = 0
    skip_reasons: Counter = Counter()

    for row in running:
        valid_time = _parse_utc(row["valid_utc"])
        decision_instant = valid_time + timedelta(minutes=OBSERVATION_LATENCY_MINUTES)
        obs = observations.observed_max_at(running, decision_instant)
        if obs is None:
            continue
        running_max_f = obs["running_max_f"]

        eligible_markets = []
        for market in markets:
            open_time = market.get("open_time")
            if not open_time:
                skip_reasons["missing_open_time"] += 1
                continue
            if decision_instant < _parse_utc(open_time):
                continue  # market not open yet at this instant -- no look-ahead
            eligible_markets.append(market)

        for market in dead_brackets_at(eligible_markets, running_max_f, margin):
            ticker = market["ticker"]
            settled = market["status"] in KALSHI_SETTLED_STATUSES
            if ticker not in seen_dead:
                seen_dead.add(ticker)
                dead_records.append({
                    "event_key": event_key, "ticker": ticker, "station": station, "date_lst": date_lst,
                    "safety_margin_f": margin, "decision_instant_utc": _format_utc(decision_instant),
                    "running_max_f": running_max_f, "n_obs": obs["n_obs"],
                    "lower_bound_f": market["lower_bound_f"], "upper_bound_f": market["upper_bound_f"],
                    "status": market["status"], "result": market["result"],
                    "settled_yes": settled and market["result"] == "yes",
                })

            candles = candles_by_ticker.get(ticker, [])
            price = price_at_instant(candles, decision_instant)
            qualifies = price.status == "ok" and price.yes_bid_cents >= MIN_BID_CENTS

            if ticker in traded:
                if qualifies:
                    n_repeat_candidates += 1
                continue

            if not qualifies:
                if price.status != "ok":
                    skip_reasons[f"price_{price.status}"] += 1
                else:
                    skip_reasons["bid_below_minimum"] += 1
                continue

            if not settled or market["result"] not in ("yes", "no"):
                skip_reasons["unsettled_result"] += 1
                continue

            no_cost_cents = 100 - price.yes_bid_cents
            if not 1 <= no_cost_cents <= 99:
                skip_reasons["cost_out_of_range"] += 1
                continue

            fee = trading_fee_cents(
                no_cost_cents, contracts, fee_type=fee_type, fee_multiplier=fee_multiplier, is_maker=False
            )
            pnl = settle_pnl_cents("no", no_cost_cents, fee, contracts, market["result"])
            staleness_minutes = (
                (valid_time - _parse_utc(price.candle_end_utc)).total_seconds() / 60.0
                if price.candle_end_utc else None
            )
            trades.append({
                "event_key": event_key, "ticker": ticker, "station": station, "date_lst": date_lst,
                "side": "no", "safety_margin_f": margin, "decision_instant_utc": _format_utc(decision_instant),
                "running_max_f": running_max_f, "n_obs": obs["n_obs"],
                "lower_bound_f": market["lower_bound_f"], "upper_bound_f": market["upper_bound_f"],
                "bid_cents": price.yes_bid_cents, "cost_cents": no_cost_cents, "fee_cents": fee,
                "contracts": contracts, "result": market["result"], "pnl_cents": pnl,
                "candle_end_utc": price.candle_end_utc, "staleness_minutes": staleness_minutes,
                "volume": price.volume, "open_interest": price.open_interest,
            })
            traded.add(ticker)

    return {
        "trades": trades, "dead_records": dead_records,
        "n_repeat_candidates": n_repeat_candidates, "skip_reasons": skip_reasons,
    }


def scan_event_day(
    series: str,
    target_date: str | date,
    *,
    contracts: int = DEFAULT_CONTRACTS_PER_TRADE,
    fee_type: str = DEFAULT_FEE_TYPE,
    fee_multiplier: float = DEFAULT_FEE_MULTIPLIER,
    period_interval: int = DEFAULT_PERIOD_INTERVAL,
    lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
    safety_margins: tuple[float, ...] = SAFETY_MARGINS_F,
    kalshi_session: Any = None,
    obs_session: Any = None,
    cutoff: dict[str, datetime] | None = None,
) -> dict[str, Any]:
    """Fetch and simulate one city-day, across every margin in `safety_margins` at once.

    Observations and market/candle data are fetched once per city-day and
    shared across margins (only the "is this bracket dead yet" arithmetic
    differs between them).

    Returns:
        `{"station", "date_lst", "event_key", "skip_reason", "by_margin", "bound_check"}`.
        `by_margin` maps each float margin to `_simulate_margin`'s output;
        empty (with `skip_reason` set) if the day has no observations or no
        market data at all.
    """
    day = _as_date(target_date)
    station = SERIES_STATIONS.get(series)
    if station is None:
        raise ValueError(f"Unknown series {series!r}; expected one of {sorted(SERIES_STATIONS)}")
    offset = STATION_STANDARD_UTC_OFFSET_HOURS[station]
    day_start = _local_day_start(day, offset)
    day_end = day_start + timedelta(hours=24)

    obs_rows = observations.fetch_asos_observations(
        station, day - timedelta(days=1), day + timedelta(days=1), session=obs_session,
    )
    running = observations.running_max_by_instant(obs_rows, day, offset)
    if not running:
        return _empty_day_result(station, day, "no_observations")

    cutoff_map = cutoff if cutoff is not None else get_historical_cutoff(kalshi_session)
    raw_markets = fetch_event_markets(series, day, kalshi_session, cutoff_map)
    if not raw_markets:
        return _empty_day_result(station, day, "no_market_data")

    markets: list[dict] = []
    for raw in raw_markets:
        market = parse_market(raw, series)
        # `parse_market` does not carry `open_time` (kalshi_history.py's
        # normalizer has no use for it elsewhere); read it directly off the
        # raw payload -- the same documented small duplication maker_sim.py
        # uses for the OHLC `high` field kalshi_history.py's own candle
        # normalizer discards.
        market["open_time"] = raw.get("open_time")
        markets.append(market)
    event_key = markets[0]["event_ticker"]

    start_ts = int((day_start - timedelta(hours=lookback_hours)).timestamp())
    end_ts = int(day_end.timestamp())
    candles_by_ticker: dict[str, list[dict]] = {}
    for market in markets:
        candles_by_ticker[market["ticker"]] = fetch_candlesticks(
            series, market["ticker"], market["event_ticker"], start_ts, end_ts,
            period_interval, kalshi_session, cutoff_map,
        )

    by_margin: dict[float, dict[str, Any]] = {}
    for margin in safety_margins:
        by_margin[margin] = _simulate_margin(
            markets, candles_by_ticker, running,
            margin=margin, station=station, date_lst=day.isoformat(), event_key=event_key,
            contracts=contracts, fee_type=fee_type, fee_multiplier=fee_multiplier,
        )

    bound_check = _bound_check_for_day(markets, running, station, day.isoformat(), event_key)

    return {
        "station": station, "date_lst": day.isoformat(), "event_key": event_key,
        "skip_reason": None, "by_margin": by_margin, "bound_check": bound_check,
    }


# --- 3. Guardrail: training window only --------------------------------------

def _enforce_period_guards(eval_end: date, protocol_path: str | Path | None) -> protocol_mod.Protocol | None:
    """Refuse an evaluation window past `TRAIN_END` without a verified protocol.

    Mirrors `weather_backtest._enforce_period_guards`'s TRAIN_END semantics
    (this experiment needs no NBM archive and no settlement-source-switch
    guard: settlement truth here is read directly off each market's own
    `result`/`status`, source-agnostic by construction -- see module
    docstring).

    Returns:
        The verified `Protocol`, if one was required and supplied; `None` if
        `eval_end <= TRAIN_END`.

    Raises:
        ValueError: If `eval_end` is past `TRAIN_END` without a
            `protocol_path`, or if the given protocol fails verification,
            has the wrong `run_kind`, or its `hypothesis` does not reference
            this backtest.
    """
    if eval_end <= TRAIN_END:
        return None
    if protocol_path is None:
        raise ValueError(
            f"eval_end {eval_end} is after TRAIN_END ({TRAIN_END}); evaluating past it requires "
            "a predeclared, verified --protocol. None was given. 2026 is a held-out period."
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


# --- 4. Orchestration ---------------------------------------------------------

def run_backtest(
    station_codes: list[str],
    start_date: str | date,
    end_date: str | date,
    *,
    contracts: int = DEFAULT_CONTRACTS_PER_TRADE,
    fee_type: str = DEFAULT_FEE_TYPE,
    fee_multiplier: float = DEFAULT_FEE_MULTIPLIER,
    period_interval: int = DEFAULT_PERIOD_INTERVAL,
    lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
    safety_margins: tuple[float, ...] = SAFETY_MARGINS_F,
    protocol_path: str | Path | None = None,
    kalshi_session: Any = None,
    obs_session: Any = None,
    n_resamples: int = DEFAULT_N_RESAMPLES,
    confidence: float = DEFAULT_CONFIDENCE,
    seed: int = DEFAULT_SEED,
) -> dict[str, Any]:
    """Scan every (station, date) in range and build the full report. See module docstring.

    Args:
        station_codes: e.g. `["KNYC", "KMDW", "KMIA", "KAUS"]`.
        start_date, end_date: Local-standard date range, inclusive.
        contracts, fee_type, fee_multiplier: Order sizing/fee parameters
            (predeclared defaults; see module constants).
        period_interval: Candlestick granularity in minutes.
        lookback_hours: Candle-fetch buffer before local day start.
        safety_margins: Which margins to report; defaults to all four
            predeclared values.
        protocol_path: See `_enforce_period_guards`.
        kalshi_session, obs_session: Optional sessions (tests), threaded
            through to `src.data.kalshi_history` and
            `src.data.weather.observations` fetchers respectively.
        n_resamples, confidence, seed: Bootstrap parameters.

    Returns:
        A JSON-serializable (`allow_nan=False` safe) report dict.

    Raises:
        ValueError: From `_enforce_period_guards`, an unmapped station, or an
            invalid date range.
    """
    start_d, end_d = _as_date(start_date), _as_date(end_date)
    if start_d > end_d:
        raise ValueError(f"start_date {start_d} must be on or before end_date {end_d}")
    verified_protocol = _enforce_period_guards(end_d, protocol_path)

    per_margin_trades: dict[float, list[dict]] = {m: [] for m in safety_margins}
    per_margin_dead: dict[float, list[dict]] = {m: [] for m in safety_margins}
    per_margin_repeat: dict[float, int] = {m: 0 for m in safety_margins}
    per_margin_skip: dict[float, Counter] = {m: Counter() for m in safety_margins}
    bound_checks: list[dict] = []
    day_skip_reasons: Counter = Counter()
    n_city_days_scanned = 0

    cutoff_map = get_historical_cutoff(kalshi_session)

    for target_date in _date_range(start_d, end_d):
        for station in station_codes:
            series = STATION_TO_SERIES.get(station)
            if series is None:
                raise ValueError(f"No Kalshi series mapped for station {station!r}")
            try:
                result = scan_event_day(
                    series, target_date,
                    contracts=contracts, fee_type=fee_type, fee_multiplier=fee_multiplier,
                    period_interval=period_interval, lookback_hours=lookback_hours,
                    safety_margins=safety_margins,
                    kalshi_session=kalshi_session, obs_session=obs_session, cutoff=cutoff_map,
                )
            except requests.exceptions.RequestException as exc:
                # A long scan spans thousands of upstream requests; a single
                # transport failure (IEM returns 503 under sustained load)
                # must not discard every city-day already scanned. Counted as
                # a named skip so a run that quietly lost days is visible in
                # the report rather than indistinguishable from a clean one.
                # Deliberately narrow: only transport errors are tolerated,
                # so a logic bug still crashes the run instead of hiding here.
                logger.warning("fetch failed for %s %s: %s", series, target_date, exc)
                day_skip_reasons["fetch_error"] += 1
                continue
            if result["skip_reason"] is not None:
                day_skip_reasons[result["skip_reason"]] += 1
                continue
            n_city_days_scanned += 1
            if result["bound_check"] is not None:
                bound_checks.append(result["bound_check"])
            for margin in safety_margins:
                margin_result = result["by_margin"][margin]
                per_margin_trades[margin].extend(margin_result["trades"])
                per_margin_dead[margin].extend(margin_result["dead_records"])
                per_margin_repeat[margin] += margin_result["n_repeat_candidates"]
                per_margin_skip[margin].update(margin_result["skip_reasons"])

    return _build_report(
        per_margin_trades, per_margin_dead, per_margin_repeat, per_margin_skip,
        bound_checks, day_skip_reasons, n_city_days_scanned,
        station_codes=station_codes, start_date=start_d, end_date=end_d,
        contracts=contracts, fee_type=fee_type, fee_multiplier=fee_multiplier,
        period_interval=period_interval, safety_margins=safety_margins,
        protocol_verified=verified_protocol is not None,
        n_resamples=n_resamples, confidence=confidence, seed=seed,
    )


# --- 5. Report assembly --------------------------------------------------------

def _staleness_bucket_label(minutes: float) -> str:
    edges = STALENESS_BUCKET_EDGES_MINUTES
    if minutes < edges[0]:
        return f"<{edges[0]:.0f}m"
    for lo, hi in zip(edges, edges[1:]):
        if lo <= minutes < hi:
            return f"{lo:.0f}-{hi:.0f}m"
    return f">={edges[-1]:.0f}m"


def _staleness_decay(trades: list[dict]) -> list[dict]:
    """P&L bucketed by minutes elapsed between the observation's valid time and the candle used."""
    buckets: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        if t["staleness_minutes"] is None:
            continue
        buckets[_staleness_bucket_label(t["staleness_minutes"])].append(t)
    order = [f"<{STALENESS_BUCKET_EDGES_MINUTES[0]:.0f}m"] + [
        f"{lo:.0f}-{hi:.0f}m" for lo, hi in zip(STALENESS_BUCKET_EDGES_MINUTES, STALENESS_BUCKET_EDGES_MINUTES[1:])
    ] + [f">={STALENESS_BUCKET_EDGES_MINUTES[-1]:.0f}m"]
    out = []
    for label in order:
        rows = buckets.get(label, [])
        pnls = [r["pnl_cents"] for r in rows]
        out.append({
            "bucket": label, "n": len(rows),
            "total_pnl_cents": float(sum(pnls)) if pnls else 0.0,
            "mean_pnl_cents": (statistics.mean(pnls) if pnls else None),
        })
    return out


def _margin_report(
    margin: float, trades: list[dict], dead_records: list[dict], n_repeat_candidates: int,
    skip_reasons: Counter, n_city_days_scanned: int, n_resamples: int, confidence: float, seed: int,
) -> dict[str, Any]:
    n_dead = len(dead_records)
    dead_settled_yes = [d for d in dead_records if d["settled_yes"]]
    n_dead_settled_yes = len(dead_settled_yes)

    n_trades = len(trades)
    n_events_traded = len({t["event_key"] for t in trades})
    wins = sum(1 for t in trades if t["pnl_cents"] > 0)
    total_pnl_cents = float(sum(t["pnl_cents"] for t in trades))
    total_cost_cents = float(sum(t["cost_cents"] * t["contracts"] for t in trades))

    pnl_by_event: dict[str, float] = defaultdict(float)
    pnl_by_station: dict[str, float] = defaultdict(float)
    for t in trades:
        pnl_by_event[t["event_key"]] += t["pnl_cents"]
        pnl_by_station[t["station"]] += t["pnl_cents"]

    volumes = [t["volume"] for t in trades if t["volume"] is not None]
    ois = [t["open_interest"] for t in trades if t["open_interest"] is not None]

    return {
        "safety_margin_f": margin,
        # --- Primary deliverable ---
        "n_dead_brackets": n_dead,
        "n_dead_settled_yes": n_dead_settled_yes,
        "dead_settled_yes_rate": (n_dead_settled_yes / n_dead) if n_dead else None,
        "dead_settled_yes_details": dead_settled_yes,
        # --- P&L (secondary to the above) ---
        "n_trades": n_trades,
        "n_events_traded": n_events_traded,
        "hit_rate": (wins / n_trades) if n_trades else None,
        "total_pnl_cents": total_pnl_cents,
        "mean_pnl_cents": (total_pnl_cents / n_trades) if n_trades else None,
        "roi_on_cost": (total_pnl_cents / total_cost_cents) if total_cost_cents else None,
        "max_drawdown_cents": max_drawdown_cents(trades) if trades else 0.0,
        "pnl_per_event_ci": event_clustered_bootstrap_ci(dict(pnl_by_event), n_resamples, confidence, seed),
        "pnl_by_station_cents": dict(pnl_by_station),
        # --- Availability (secondary #1) ---
        "n_repeat_candidates": n_repeat_candidates,
        "availability_rate": (n_trades / n_dead) if n_dead else None,
        "opportunities_per_city_day": (n_dead / n_city_days_scanned) if n_city_days_scanned else None,
        # --- Staleness decay (secondary #2) ---
        "staleness_decay": _staleness_decay(trades),
        # --- Depth (secondary #3 -- see DEPTH_LIMITATION_NOTE at report top level) ---
        "mean_volume": (statistics.mean(volumes) if volumes else None),
        "mean_open_interest": (statistics.mean(ois) if ois else None),
        "skip_reasons": dict(skip_reasons),
        "trades": trades,
        "dead_records": dead_records,
    }


def _build_report(
    per_margin_trades: dict[float, list[dict]], per_margin_dead: dict[float, list[dict]],
    per_margin_repeat: dict[float, int], per_margin_skip: dict[float, Counter],
    bound_checks: list[dict], day_skip_reasons: Counter, n_city_days_scanned: int, *,
    station_codes: list[str], start_date: date, end_date: date, contracts: int, fee_type: str,
    fee_multiplier: float, period_interval: int, safety_margins: tuple[float, ...],
    protocol_verified: bool, n_resamples: int, confidence: float, seed: int,
) -> dict[str, Any]:
    by_margin = {
        _margin_key(margin): _margin_report(
            margin, per_margin_trades[margin], per_margin_dead[margin], per_margin_repeat[margin],
            per_margin_skip[margin], n_city_days_scanned, n_resamples, confidence, seed,
        )
        for margin in safety_margins
    }

    n_bound_below = sum(1 for b in bound_checks if b["settled_high_below_asos"])
    bound_check_report = {
        "n_event_days_checked": len(bound_checks),
        "n_settled_high_below_asos": n_bound_below,
        "rate": (n_bound_below / len(bound_checks)) if bound_checks else None,
        "details": [b for b in bound_checks if b["settled_high_below_asos"]],
    }

    headline_key = _margin_key(DEFAULT_SAFETY_MARGIN_F)
    headline = by_margin.get(headline_key)
    falsified = bool(headline and headline["n_dead_settled_yes"] > 0)

    return {
        "station_codes": station_codes, "start_date": start_date.isoformat(), "end_date": end_date.isoformat(),
        "contracts_per_trade": contracts, "fee_type": fee_type, "fee_multiplier": fee_multiplier,
        "period_interval": period_interval, "safety_margins_f": list(safety_margins),
        "headline_safety_margin_f": DEFAULT_SAFETY_MARGIN_F,
        "protocol_verified": protocol_verified,
        "kalshi_history_usable_start": KALSHI_CANDLESTICK_HISTORY_USABLE_START.isoformat(),
        "n_city_days_scanned": n_city_days_scanned,
        "day_skip_reasons": dict(day_skip_reasons),
        "by_margin": by_margin,
        "bound_check": bound_check_report,
        "depth_limitation_note": DEPTH_LIMITATION_NOTE,
        "falsified_at_headline_margin": falsified,
    }


def format_report(report: dict[str, Any]) -> str:
    """Human-readable report text, primary deliverable first (per predeclaration)."""
    lines: list[str] = []
    lines.append("=" * 78)
    lines.append("PRIMARY DELIVERABLE: dead-bracket / settled-YES disagreement rate, by safety margin")
    lines.append("(NOT P&L -- a non-zero rate at the 2.0F headline margin falsifies the strategy)")
    lines.append("=" * 78)
    lines.append(f"{'margin_f':>10} {'n_dead':>10} {'n_settled_yes':>15} {'rate':>10}")
    for margin in report["safety_margins_f"]:
        m = report["by_margin"][_margin_key(margin)]
        rate = m["dead_settled_yes_rate"]
        rate_str = "n/a" if rate is None else f"{rate:.4f}"
        lines.append(f"{margin:>10.1f} {m['n_dead_brackets']:>10} {m['n_dead_settled_yes']:>15} {rate_str:>10}")

    headline = report["by_margin"][_margin_key(report["headline_safety_margin_f"])]
    if report["falsified_at_headline_margin"]:
        lines.append("")
        lines.append(
            f"FALSIFIED: {headline['n_dead_settled_yes']} of {headline['n_dead_brackets']} brackets called "
            f"dead at the headline margin ({report['headline_safety_margin_f']:.1f}F) nonetheless settled "
            "YES. The premise (ASOS-declared-dead brackets never settle YES) is broken; the strategy is "
            "dead regardless of any P&L shown below."
        )
        for detail in headline["dead_settled_yes_details"]:
            lines.append(
                f"  - {detail['station']} {detail['date_lst']} {detail['ticker']}: bounds="
                f"[{detail['lower_bound_f']}, {detail['upper_bound_f']}) asos_running_max_f="
                f"{detail['running_max_f']} at {detail['decision_instant_utc']} (n_obs={detail['n_obs']}); "
                f"winning bracket implies the settled high fell in this same bracket's range."
            )
    else:
        lines.append("")
        lines.append(
            f"Primary deliverable clean at the headline margin ({report['headline_safety_margin_f']:.1f}F): "
            f"0 of {headline['n_dead_brackets']} ASOS-declared-dead brackets settled YES."
        )

    lines.append("")
    lines.append("-" * 78)
    lines.append("P&L (secondary to the above)")
    lines.append("-" * 78)
    lines.append(f"{'margin_f':>10} {'n_trades':>10} {'hit_rate':>10} {'total_pnl_c':>13} {'mean_pnl_c':>12}")
    for margin in report["safety_margins_f"]:
        m = report["by_margin"][_margin_key(margin)]
        hit = "n/a" if m["hit_rate"] is None else f"{m['hit_rate']:.3f}"
        mean_pnl = "n/a" if m["mean_pnl_cents"] is None else f"{m['mean_pnl_cents']:.2f}"
        lines.append(
            f"{margin:>10.1f} {m['n_trades']:>10} {hit:>10} {m['total_pnl_cents']:>13.1f} {mean_pnl:>12}"
        )

    lines.append("")
    lines.append("-" * 78)
    lines.append("Availability (secondary #1)")
    lines.append("-" * 78)
    for margin in report["safety_margins_f"]:
        m = report["by_margin"][_margin_key(margin)]
        opp = "n/a" if m["opportunities_per_city_day"] is None else f"{m['opportunities_per_city_day']:.3f}"
        avail = "n/a" if m["availability_rate"] is None else f"{m['availability_rate']:.3f}"
        lines.append(
            f"  margin={margin:.1f}F: opportunities/city-day={opp}, fraction with bid>=1c={avail}, "
            f"repeat_candidates={m['n_repeat_candidates']}"
        )

    lines.append("")
    lines.append("-" * 78)
    lines.append(f"Staleness decay (secondary #2), headline margin {report['headline_safety_margin_f']:.1f}F")
    lines.append("-" * 78)
    for bucket in headline["staleness_decay"]:
        mean_pnl = "n/a" if bucket["mean_pnl_cents"] is None else f"{bucket['mean_pnl_cents']:.2f}"
        lines.append(f"  {bucket['bucket']:>8}: n={bucket['n']:>4} total_pnl_c={bucket['total_pnl_cents']:>9.1f} mean_pnl_c={mean_pnl}")

    lines.append("")
    lines.append("-" * 78)
    lines.append("Depth (secondary #3) -- LIMITATION")
    lines.append("-" * 78)
    lines.append(f"  {report['depth_limitation_note']}")
    lines.append(
        f"  headline margin mean_volume={headline['mean_volume']}, mean_open_interest={headline['mean_open_interest']}"
    )

    lines.append("")
    lines.append("-" * 78)
    lines.append("Bound check (secondary #4): settled winning bracket implies a high BELOW the ASOS running max")
    lines.append("-" * 78)
    bc = report["bound_check"]
    rate_str = "n/a" if bc["rate"] is None else f"{bc['rate']:.4f}"
    lines.append(f"  {bc['n_settled_high_below_asos']} of {bc['n_event_days_checked']} event-days (rate={rate_str})")

    return "\n".join(lines)


# --- 6. CLI ------------------------------------------------------------------

def _split_stations(value: str) -> list[str]:
    return [s.strip().upper() for s in value.split(",") if s.strip()]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m src.research.intraday_floor",
        description="Test whether Kalshi weather brackets that ASOS observations declare "
                     "arithmetically dead still trade with a live YES bid.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    default_stations = ",".join(sorted(STATION_TO_SERIES))

    run_p = sub.add_parser("run", help="Scan a date range and write a JSON report.")
    run_p.add_argument("--stations", default=default_stations, help="Comma-separated station codes")
    run_p.add_argument("--start", required=True, help="ISO start date (local-standard), inclusive")
    run_p.add_argument("--end", required=True, help="ISO end date, inclusive")
    run_p.add_argument("--contracts", type=int, default=DEFAULT_CONTRACTS_PER_TRADE)
    run_p.add_argument("--fee-type", default=DEFAULT_FEE_TYPE)
    run_p.add_argument("--fee-multiplier", type=float, default=DEFAULT_FEE_MULTIPLIER)
    run_p.add_argument("--period-interval", type=int, default=DEFAULT_PERIOD_INTERVAL)
    run_p.add_argument("--protocol", default=None, help="Path to a predeclared, saved protocol JSON")
    run_p.add_argument("--output", required=True, help="Output JSON report path")

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args(argv)
    if args.command == "run":
        report = run_backtest(
            _split_stations(args.stations), args.start, args.end,
            contracts=args.contracts, fee_type=args.fee_type, fee_multiplier=args.fee_multiplier,
            period_interval=args.period_interval, protocol_path=args.protocol,
        )
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2, allow_nan=False, default=str))
        print(format_report(report))
        print(f"\nWrote full report to {out}")


if __name__ == "__main__":
    main()
