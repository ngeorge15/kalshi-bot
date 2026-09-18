"""Maker-side simulation: resting a sell-YES offer (== buying NO) on weather brackets.

**Background.** `research/longshot-bias.md` finds a real favourite-longshot bias in
Kalshi weather-bracket prices, but it sits inside the 3.8c mean bid-ask spread, so a
*taker* cannot capture it (`research/weather-backtest-validation.md`: a taker rule
loses ~12-13% of capital deployed). Selling the same brackets *at the ask* -- i.e.
resting an offer that someone else crosses into -- nets an apparent +1.5 to +3.9c per
contract *if filled*. That "if" is the whole question this module measures: does a
resting order actually fill, and are the fills it gets adversely selected (does the
market only cross into you right before it moves against you)?

**No forecast is used here.** This is a pure market-microstructure strategy: it posts
an offer relative to the market's own quote at the decision instant T and asks whether
the market later trades through that price. It consumes no NBM/NWS forecast, so
`src.data.weather.archive.NBM_ARCHIVE_USABLE_START` does not apply and is not checked.

**Fill rule.** At T (local-standard day start - 1 minute, same instant
`src.research.weather_backtest` trades at), post a sell-YES offer at a price derived
from the prevailing ask (`--offer ask`, `ask-1`, or `ask+1`). Then walk forward through
that bracket's own candles from T to its close. A fill is recorded only when a later
candle's **bid high** reaches or exceeds the offer:
  - `bid_high > offer`: always a fill -- the market traded cleanly through your price,
    which would have cleared every resting order at or below it, including yours.
  - `bid_high == offer` (an exact touch, not a cross): filled only in a fraction
    `queue_fill_fraction` (default 0.5) of such cases, chosen **deterministically by a
    seeded hash of the ticker** (not per-run randomness) -- modeling that some resting
    orders sit at the back of the queue at a touched price and never actually trade.
  - No candle at or before T can ever produce a fill.

**A note on candlestick fields.** `src.data.kalshi_history.fetch_candlesticks`
deliberately normalizes every candle down to its bid/ask **close** price only, and
caches that shape forever. The real payload carries full OHLC per side --
confirmed live on 2026-09-17 against both `/historical/markets/{ticker}/candlesticks`
(`yes_bid: {open, high, low, close}`, plain dollar-string fields) and
`/series/{series}/markets/{ticker}/candlesticks` (`yes_bid: {open_dollars,
high_dollars, low_dollars, close_dollars}`), matching `fetch_candlesticks`'s own
`_dollars`-suffix convention for the live/historical split. This fill rule needs the
candle's **high**, which `fetch_candlesticks` throws away, and every prior use of this
series (the backtest, the longshot-bias analysis) only ever needed a single
point-in-time quote, so the existing `kalshi_history_candles` cache holds no OHLC data
to reuse. Rather than editing `kalshi_history.py`'s normalizer or cache shape (other
reviewed modules depend on it staying close-only), this module fetches and normalizes
candlesticks itself (`fetch_ohlc_candles` / `_normalize_ohlc_candle`), duplicating the
small pieces of `kalshi_history.py`'s request/cache/live-vs-historical logic it needs
(a documented duplication, in the same spirit as this repo's small date-helper
duplication across `src/data/weather/archive.py`, `src/research/weather_backtest.py`,
and this module) and caching the result under its own namespace,
`maker_sim_ohlc_candles`.

**Adverse selection is the point.** For every posted offer -- filled or not -- this
module records the bracket's real settlement. The deliverable is not the average P&L
of filled offers alone; it is the **comparison** between what filled offers settled at
and what unfilled ones would have settled at. If fills are benign, the two groups
should look similar. If the market only crosses into you when it is about to move
against you, filled offers will show a higher YES-settlement rate (bad for a YES
seller) than unfilled ones.

**Selection policy (disclosed, not tuned).** By default at most one offer is posted
per city-day event (`--max-offers-per-event`, default 1): the single bracket with the
**cheapest** tradeable ask in the event. This is a deliberately simple, non-cherry-
picked rule -- it does not restrict candidacy to the ask band `research/longshot-bias.md`
already found most profitable (10-25c), which would bias the result toward a
previously observed answer. The price-band breakdown in the report shows where any
edge actually concentrates.

**Guards.** No NBM-archive-start guard applies (no forecast is used). Any evaluation
date after `HARD_CUTOFF` (2025-12-31) is refused unless given a predeclared, verified
`src.paper.protocol` naming this experiment (`PROTOCOL_HYPOTHESIS_MARKER`) with
`run_kind="replay"`. 2026 is a held-out period; this module never declares a protocol
itself.

Usage::

    from src.research.maker_sim import run_maker_sim

    report = run_maker_sim(
        station_codes=["KNYC", "KMDW", "KMIA", "KAUS"],
        start_date="2024-12-01", end_date="2025-12-31",
        offer_mode="ask", contracts=10, queue_fill_fraction=0.5,
    )
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import statistics
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterator

import numpy as np
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from src.data.cache import cache_get, cache_set
from src.data.kalshi_history import (
    BASE_URL,
    REQUEST_TIMEOUT,
    SERIES_STATIONS,
    STATION_STANDARD_UTC_OFFSET_HOURS,
    decision_instant_utc,
    fetch_event_at_decision,
    get_historical_cutoff,
)
from src.paper import protocol as protocol_mod
from src.paper.fees import trading_fee_cents
from src.paper.uncertainty import DEFAULT_CONFIDENCE, DEFAULT_N_RESAMPLES, DEFAULT_SEED
from src.research.weather_backtest import event_clustered_bootstrap_ci, max_drawdown_cents

logger = logging.getLogger(__name__)

STATION_TO_SERIES: dict[str, str] = {station: series for series, station in SERIES_STATIONS.items()}

# --- HTTP (small, deliberate duplication of kalshi_history.py's session setup;
# see module docstring) -----------------------------------------------------

CONTACT_EMAIL = os.environ.get("NWS_CONTACT_EMAIL", "kalshi-bot@example.com")
SLEEP_SECONDS = 1.0  # politeness delay between real network calls
LONG_CACHE_TTL_SECONDS = 100 * 365 * 24 * 3600  # settled OHLC candles never change

_SESSION: requests.Session | None = None


def _get_session() -> requests.Session:
    """Return the module-level requests.Session (built once). GET-only, retries 429/5xx."""
    global _SESSION
    if _SESSION is None:
        session = requests.Session()
        retry = Retry(
            total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"], raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        session.headers.update({"User-Agent": f"(kalshi-bot-research-maker-sim, {CONTACT_EMAIL})"})
        _SESSION = session
    return _SESSION


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


# --- 1. Raw OHLC candlesticks, preserving bid-high (see module docstring) -------

OHLC_CACHE_NAMESPACE = "maker_sim_ohlc_candles"


def _dollars_to_cents(value: object) -> int | None:
    """Parse a Kalshi dollar-string ("0.0300") to integer cents, or None."""
    if value is None:
        return None
    try:
        return int((Decimal(str(value)) * 100).to_integral_value(rounding=ROUND_HALF_UP))
    except (ArithmeticError, ValueError):
        return None


def _normalize_ohlc_candle(raw: dict, is_historical: bool) -> dict:
    """Normalize one raw candlestick, keeping the bid's `high` (see module docstring).

    Only the bid side is kept: a resting sell-YES offer is filled by a buyer's bid
    rising to meet it, so the ask side's OHLC is irrelevant to the fill rule.
    """
    price_suffix = "" if is_historical else "_dollars"
    yes_bid = raw.get("yes_bid") or {}
    return {
        "end_period_ts": raw["end_period_ts"],
        "yes_bid_high_cents": _dollars_to_cents(yes_bid.get(f"high{price_suffix}")),
        "yes_bid_close_cents": _dollars_to_cents(yes_bid.get(f"close{price_suffix}")),
    }


def fetch_ohlc_candles(
    series: str,
    ticker: str,
    station: str,
    target_date: str | date,
    start_ts: int,
    end_ts: int,
    period_interval: int,
    session: requests.Session | None = None,
    cutoff: dict[str, datetime] | None = None,
) -> list[dict]:
    """Fetch and normalize candlesticks for one market, keeping bid-high.

    Duplicates `kalshi_history.fetch_candlesticks`'s live/historical endpoint
    selection and dollar-to-cents parsing (see module docstring for why); cached
    under `OHLC_CACHE_NAMESPACE`, a namespace `kalshi_history.py`'s own cache never
    touches.

    Args:
        series: One of `SERIES_STATIONS`.
        ticker: The market ticker.
        station: That market's station code (for the UTC-offset lookup).
        target_date: The event's local-standard target date.
        start_ts, end_ts: Unix-second window, inclusive.
        period_interval: Minutes per candle; Kalshi accepts 1, 60, or 1440.
        session: Optional session (tests).
        cutoff: Optional pre-fetched `get_historical_cutoff()` result.

    Returns:
        List of `{end_period_ts, yes_bid_high_cents, yes_bid_close_cents}` dicts,
        ordered as returned by the API.
    """
    day = _as_date(target_date)
    offset = STATION_STANDARD_UTC_OFFSET_HOURS[station]
    expected_close = _local_day_start(day + timedelta(days=1), offset)

    sess = session if session is not None else _get_session()
    cutoff_map = cutoff if cutoff is not None else get_historical_cutoff(sess)
    use_historical = expected_close <= cutoff_map["market_settled_ts"]

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
        cached = cache_get(OHLC_CACHE_NAMESPACE, cache_params, ttl_seconds=LONG_CACHE_TTL_SECONDS)
        if cached is not None:
            return cached

    resp = sess.get(url, params=params, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    time.sleep(SLEEP_SECONDS)
    raw_candles = resp.json().get("candlesticks", [])
    normalized = [_normalize_ohlc_candle(c, use_historical) for c in raw_candles]

    if use_historical:
        cache_set(OHLC_CACHE_NAMESPACE, cache_params, normalized)
    return normalized


# --- 2. Offer price and the fill rule --------------------------------------------

OFFER_MODES = ("ask", "ask-1", "ask+1")

DEFAULT_QUEUE_FILL_FRACTION = 0.5
DEFAULT_CONTRACTS = 10
DEFAULT_FEE_TYPE = "quadratic"
DEFAULT_FEE_MULTIPLIER = 1.0
DEFAULT_MAX_OFFERS_PER_EVENT = 1
DEFAULT_PERIOD_INTERVAL = 60

# Spec-mandated price bands for the report breakdown (cents; lo <= price < hi).
PRICE_BANDS: list[tuple[int, int]] = [(2, 5), (5, 10), (10, 20), (20, 35)]


def offer_price_cents(yes_ask_cents: int, offer_mode: str) -> int | None:
    """The sell-YES offer price for `offer_mode`, or `None` if untradeable ([1, 99]).

    Raises:
        ValueError: If `offer_mode` is not one of `OFFER_MODES`.
    """
    if offer_mode not in OFFER_MODES:
        raise ValueError(f"Unknown offer_mode {offer_mode!r}; expected one of {OFFER_MODES}")
    delta = {"ask": 0, "ask-1": -1, "ask+1": 1}[offer_mode]
    price = yes_ask_cents + delta
    return price if 1 <= price <= 99 else None


def _queue_fill_decision(ticker: str, queue_fill_fraction: float) -> bool:
    """Deterministic (per-ticker, not per-run) "was this order at the front of the queue?"

    A hash of the ticker maps to a uniform value in [0, 1); the order is deemed
    front-of-queue (and thus fillable on an exact touch) iff that value falls below
    `queue_fill_fraction`. The same ticker always gets the same answer, so repeated
    runs and repeated exact touches are consistent rather than independently random.
    """
    if queue_fill_fraction <= 0.0:
        return False
    if queue_fill_fraction >= 1.0:
        return True
    digest = hashlib.sha256(ticker.encode("utf-8")).hexdigest()
    fraction = int(digest[:8], 16) / 0xFFFFFFFF
    return fraction < queue_fill_fraction


@dataclass(frozen=True)
class FillResult:
    """Outcome of walking one bracket's post-T candles against a posted offer.

    Attributes:
        filled: Whether the offer filled.
        fill_reason: `"strict_cross"` (a later bid-high strictly exceeded the offer --
            always a fill), `"queue_touch"` (a later bid-high exactly matched the
            offer and this ticker's deterministic queue draw won it), or `None`
            (never filled).
        fill_end_period_ts: The filling candle's `end_period_ts`, or `None`.
        n_candles_checked: Candles strictly after T that were examined.
        max_bid_high_cents: The highest bid-high seen after T, or `None` if no
            candle after T had a usable bid-high (diagnostic: how close the market
            came to the offer even when it never filled).
    """

    filled: bool
    fill_reason: str | None = None
    fill_end_period_ts: int | None = None
    n_candles_checked: int = 0
    max_bid_high_cents: int | None = None


def simulate_fill(
    ticker: str, offer_cents: int, candles: list[dict], decision_ts: float, queue_fill_fraction: float,
) -> FillResult:
    """Walk `candles` strictly after `decision_ts`, applying the fill rule (see module docstring).

    Args:
        ticker: The bracket's ticker (input to the deterministic queue draw).
        offer_cents: The posted sell-YES offer price, in cents.
        candles: Normalized OHLC candles from `fetch_ohlc_candles` (bid-high only
            needs to be present; any order is accepted, sorted here).
        decision_ts: T as a Unix timestamp. No candle at or before this instant can
            ever produce a fill.
        queue_fill_fraction: Passed to `_queue_fill_decision`.

    Returns:
        A `FillResult`.
    """
    eligible = sorted(
        (c for c in candles if c["end_period_ts"] > decision_ts),
        key=lambda c: c["end_period_ts"],
    )
    max_seen: int | None = None
    for candle in eligible:
        bid_high = candle.get("yes_bid_high_cents")
        if bid_high is None:
            continue
        max_seen = bid_high if max_seen is None else max(max_seen, bid_high)
        if bid_high > offer_cents:
            return FillResult(True, "strict_cross", candle["end_period_ts"], len(eligible), max_seen)
        if bid_high == offer_cents and _queue_fill_decision(ticker, queue_fill_fraction):
            return FillResult(True, "queue_touch", candle["end_period_ts"], len(eligible), max_seen)
    return FillResult(False, None, None, len(eligible), max_seen)


# --- 3. Costs and settlement (mirrors weather_backtest.py's NO-side arithmetic) --

def settle_short_yes_pnl_cents(offer_cents: int, fee_cents: float, contracts: int, result: str) -> float:
    """Total order P&L for a filled sell-YES offer, in cents.

    Selling YES at `offer_cents` is exactly equivalent to buying NO at
    `100 - offer_cents` (see module docstring): the seller collects `offer_cents` and
    is on the hook for the full 100 if the bracket resolves YES.

    Args:
        offer_cents: The filled offer price (cents received per contract).
        fee_cents: The order-level fee for the whole order (charged once, not per
            contract).
        contracts: Number of contracts in the order.
        result: The bracket's settlement, `"yes"` or `"no"`.
    """
    cost_cents = 100 - offer_cents  # NO-equivalent cost
    outcome_cents = 100 * contracts if result == "no" else 0
    return outcome_cents - cost_cents * contracts - fee_cents


# --- 4. Metrics -------------------------------------------------------------------

def _price_band(price_cents: int) -> str:
    for lo, hi in PRICE_BANDS:
        if lo <= price_cents < hi:
            return f"{lo}-{hi}c"
    return "other"


def _bootstrap_mean_ci(
    values: list[float], n_resamples: int, confidence: float, seed: int,
) -> tuple[float | None, float | None]:
    """Percentile bootstrap CI for the mean of `values`; `(None, None)` if n < 2."""
    n = len(values)
    if n < 2:
        return None, None
    arr = np.array(values, dtype=float)
    rng = np.random.default_rng(seed)
    resample_indices = rng.integers(0, n, size=(n_resamples, n))
    resample_means = arr[resample_indices].mean(axis=1)
    alpha = 1 - confidence
    lo, hi = np.percentile(resample_means, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    return float(lo), float(hi)


def fill_vs_unfilled_comparison(
    offers: list[dict], n_resamples: int = DEFAULT_N_RESAMPLES, confidence: float = DEFAULT_CONFIDENCE,
    seed: int = DEFAULT_SEED,
) -> dict[str, Any]:
    """Compare settlement outcomes of filled vs. unfilled posted offers. The deliverable.

    Each group's YES-settlement rate (bad for a YES seller) and hypothetical
    per-contract P&L (at the posted offer price, ignoring fees, so the comparison
    isolates the settlement difference from any cost/fee asymmetry) are reported
    side by side. With the default of at most one offer per event, each group is
    already one observation per event, so this bootstrap is effectively
    event-clustered; with `max_offers_per_event > 1` it resamples individual offers
    and may understate correlation within an event -- a documented limitation (see
    `research/maker-simulation.md`).

    Args:
        offers: Rows with `"filled"` (bool), `"result"` (`"yes"`/`"no"`), and
            `"offer_price_cents"` (int).
        n_resamples, confidence, seed: Bootstrap parameters.

    Returns:
        `{"filled": {...}, "unfilled": {...}}`, each with `n`, `yes_rate`,
        `yes_rate_ci_low`, `yes_rate_ci_high`, `mean_hypothetical_pnl_per_contract`.
    """
    def summarize(group: list[dict]) -> dict[str, Any]:
        n = len(group)
        if n == 0:
            return {
                "n": 0, "yes_rate": None, "yes_rate_ci_low": None, "yes_rate_ci_high": None,
                "mean_hypothetical_pnl_per_contract": None,
            }
        yes_indicator = [1.0 if o["result"] == "yes" else 0.0 for o in group]
        hypothetical_pnl = [
            (100.0 if o["result"] == "no" else 0.0) - (100.0 - o["offer_price_cents"]) for o in group
        ]
        ci_low, ci_high = _bootstrap_mean_ci(yes_indicator, n_resamples, confidence, seed)
        return {
            "n": n,
            "yes_rate": statistics.mean(yes_indicator),
            "yes_rate_ci_low": ci_low,
            "yes_rate_ci_high": ci_high,
            "mean_hypothetical_pnl_per_contract": statistics.mean(hypothetical_pnl),
        }

    filled = [o for o in offers if o["filled"]]
    unfilled = [o for o in offers if not o["filled"]]
    return {"filled": summarize(filled), "unfilled": summarize(unfilled)}


# --- 5. Guard: 2026 is held out ---------------------------------------------------

# No forecast is used here, so `NBM_ARCHIVE_USABLE_START` does not apply. This is
# the only period guard this module enforces: 2026 stays untouched without a
# predeclared, verified protocol naming this experiment.
HARD_CUTOFF = date(2025, 12, 31)

PROTOCOL_HYPOTHESIS_MARKER = "maker_sim"
REQUIRED_PROTOCOL_RUN_KIND = "replay"


def _enforce_period_guard(eval_end: date, protocol_path: str | Path | None) -> protocol_mod.Protocol | None:
    """Refuse an evaluation window past `HARD_CUTOFF` without a verified protocol.

    Returns:
        The verified `Protocol`, if one was required and supplied; `None` if
        `eval_end <= HARD_CUTOFF`.

    Raises:
        ValueError: If `eval_end` is past `HARD_CUTOFF` without a `protocol_path`,
            or if the given protocol fails verification, has the wrong `run_kind`,
            or its `hypothesis` does not reference this experiment.
    """
    if eval_end <= HARD_CUTOFF:
        return None
    if protocol_path is None:
        raise ValueError(
            f"eval_end {eval_end} is after HARD_CUTOFF ({HARD_CUTOFF}); evaluating past it "
            "requires a predeclared, verified --protocol. None was given. 2026 is a held-out "
            "period for this experiment."
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
            f"Protocol at {protocol_path} has run_kind {declared.run_kind!r}; this experiment "
            f"requires {REQUIRED_PROTOCOL_RUN_KIND!r} (it replays historical market data)."
        )
    if PROTOCOL_HYPOTHESIS_MARKER not in declared.hypothesis:
        raise ValueError(
            f"Protocol at {protocol_path}'s hypothesis does not contain the required marker "
            f"{PROTOCOL_HYPOTHESIS_MARKER!r}; it does not verifiably govern this experiment: "
            f"{declared.hypothesis!r}"
        )
    return declared


# --- 6. Orchestration --------------------------------------------------------------

def _select_offers_for_event(candidates: list[dict], max_offers_per_event: int) -> list[dict]:
    """The `max_offers_per_event` cheapest tradeable-ask candidates for one event.

    Cheapest-ask-first is a deliberately simple, non-cherry-picked default -- see
    module docstring's "Selection policy" note. Ties broken by ticker for
    determinism.
    """
    ordered = sorted(candidates, key=lambda c: (c["offer_price_cents"], c["ticker"]))
    return ordered[:max_offers_per_event]


def run_maker_sim(
    station_codes: list[str],
    start_date: str | date,
    end_date: str | date,
    offer_mode: str = "ask",
    contracts: int = DEFAULT_CONTRACTS,
    queue_fill_fraction: float = DEFAULT_QUEUE_FILL_FRACTION,
    max_offers_per_event: int = DEFAULT_MAX_OFFERS_PER_EVENT,
    fee_type: str = DEFAULT_FEE_TYPE,
    fee_multiplier: float = DEFAULT_FEE_MULTIPLIER,
    period_interval: int = DEFAULT_PERIOD_INTERVAL,
    protocol_path: str | Path | None = None,
    session: Any = None,
    n_resamples: int = DEFAULT_N_RESAMPLES,
    confidence: float = DEFAULT_CONFIDENCE,
    seed: int = DEFAULT_SEED,
) -> dict[str, Any]:
    """Post one sell-YES offer per city-day event (by default) and simulate fills.

    See module docstring for the fill rule, cost model, and guard.

    Args:
        station_codes: e.g. `["KNYC", "KMDW", "KMIA", "KAUS"]`.
        start_date, end_date: Evaluation date range (local-standard), inclusive.
        offer_mode: One of `OFFER_MODES`.
        contracts: Fixed contract count per posted offer.
        queue_fill_fraction: Passed to `_queue_fill_decision`.
        max_offers_per_event: At most this many offers posted per city-day event
            (cheapest tradeable ask first).
        fee_type, fee_multiplier: Passed to `trading_fee_cents`.
        period_interval: Candlestick granularity in minutes for both the T-quote
            fetch and the post-T OHLC walk.
        protocol_path: See `_enforce_period_guard`.
        session: Optional `requests.Session` (tests), threaded through to every
            fetch.
        n_resamples, confidence, seed: Bootstrap parameters.

    Returns:
        A JSON-serializable (`allow_nan=False` safe) report dict.

    Raises:
        ValueError: From `_enforce_period_guard`, an invalid `offer_mode`, or an
            invalid date range.
    """
    start_d, end_d = _as_date(start_date), _as_date(end_date)
    if start_d > end_d:
        raise ValueError(f"start_date {start_d} must be on or before end_date {end_d}")
    if offer_mode not in OFFER_MODES:
        raise ValueError(f"Unknown offer_mode {offer_mode!r}; expected one of {OFFER_MODES}")
    verified_protocol = _enforce_period_guard(end_d, protocol_path)

    offers: list[dict] = []
    skip_reasons: Counter = Counter()

    for target_date in _date_range(start_d, end_d):
        for station in station_codes:
            series = STATION_TO_SERIES.get(station)
            if series is None:
                raise ValueError(f"No Kalshi series mapped for station {station!r}")

            event_rows = fetch_event_at_decision(
                series, target_date, session=session, period_interval=period_interval,
            )
            if not event_rows:
                skip_reasons["no_market_data"] += 1
                continue
            event_key = event_rows[0]["event_ticker"]
            decision_time = decision_instant_utc(series, target_date)
            decision_ts = decision_time.timestamp()

            candidates: list[dict] = []
            for row in event_rows:
                price = row["price"]
                if price.status != "ok":
                    skip_reasons[f"price_{price.status}"] += 1
                    continue
                offer = offer_price_cents(price.yes_ask_cents, offer_mode)
                if offer is None:
                    skip_reasons["untradeable_offer_price"] += 1
                    continue
                if row["result"] not in ("yes", "no"):
                    skip_reasons["unsettled_result"] += 1
                    continue
                if not row.get("close_time"):
                    skip_reasons["no_close_time"] += 1
                    continue
                candidates.append({
                    "event_key": event_key, "ticker": row["ticker"], "station": station,
                    "date_lst": row["date_lst"], "offer_price_cents": offer,
                    "ask_cents_at_t": price.yes_ask_cents, "result": row["result"],
                    "close_time": row["close_time"],
                })

            if not candidates:
                skip_reasons["no_candidates"] += 1
                continue

            cutoff_map = get_historical_cutoff(session if session is not None else _get_session())
            for chosen in _select_offers_for_event(candidates, max_offers_per_event):
                close_ts = int(_parse_utc(chosen["close_time"]).timestamp())
                candles = fetch_ohlc_candles(
                    series, chosen["ticker"], station, target_date,
                    int(decision_ts), close_ts, period_interval,
                    session=session, cutoff=cutoff_map,
                )
                fill = simulate_fill(
                    chosen["ticker"], chosen["offer_price_cents"], candles, decision_ts, queue_fill_fraction,
                )
                offer_record = {**chosen, "filled": fill.filled, "fill_reason": fill.fill_reason,
                                 "n_candles_checked": fill.n_candles_checked,
                                 "max_bid_high_cents": fill.max_bid_high_cents}
                if fill.filled:
                    fee = trading_fee_cents(
                        100 - chosen["offer_price_cents"], contracts,
                        fee_type=fee_type, fee_multiplier=fee_multiplier, is_maker=True,
                    )
                    pnl = settle_short_yes_pnl_cents(chosen["offer_price_cents"], fee, contracts, chosen["result"])
                    offer_record.update({"contracts": contracts, "fee_cents": fee, "pnl_cents": pnl,
                                          "cost_cents": 100 - chosen["offer_price_cents"]})
                else:
                    offer_record.update({"contracts": contracts, "fee_cents": 0.0, "pnl_cents": 0.0,
                                          "cost_cents": None})
                offers.append(offer_record)

    return _build_report(
        offers, skip_reasons,
        offer_mode=offer_mode, contracts=contracts, queue_fill_fraction=queue_fill_fraction,
        max_offers_per_event=max_offers_per_event, fee_type=fee_type, fee_multiplier=fee_multiplier,
        start_date=start_d, end_date=end_d, protocol_verified=verified_protocol is not None,
        n_resamples=n_resamples, confidence=confidence, seed=seed,
    )


def _build_report(
    offers: list[dict], skip_reasons: Counter, *, offer_mode: str, contracts: int, queue_fill_fraction: float,
    max_offers_per_event: int, fee_type: str, fee_multiplier: float, start_date: date, end_date: date,
    protocol_verified: bool, n_resamples: int, confidence: float, seed: int,
) -> dict[str, Any]:
    """Assemble the final metrics dict from the run's posted-offer log."""
    trades = [o for o in offers if o["filled"]]
    n_offers, n_fills = len(offers), len(trades)
    n_events = len({o["event_key"] for o in offers})

    total_pnl_cents = float(sum(t["pnl_cents"] for t in trades))
    total_cost_cents = float(sum(t["cost_cents"] * t["contracts"] for t in trades))
    wins = sum(1 for t in trades if t["pnl_cents"] > 0)

    pnl_by_event: dict[str, float] = defaultdict(float)
    pnl_by_station: dict[str, float] = defaultdict(float)
    pnl_by_month: dict[str, float] = defaultdict(float)
    pnl_by_band: dict[str, list[float]] = defaultdict(list)
    n_by_band: dict[str, int] = defaultdict(int)
    n_fills_by_band: dict[str, int] = defaultdict(int)
    for t in trades:
        pnl_by_event[t["event_key"]] += t["pnl_cents"]
        pnl_by_station[t["station"]] += t["pnl_cents"]
        pnl_by_month[t["date_lst"][:7]] += t["pnl_cents"]
    for o in offers:
        band = _price_band(o["offer_price_cents"])
        n_by_band[band] += 1
        if o["filled"]:
            n_fills_by_band[band] += 1
            pnl_by_band[band].append(o["pnl_cents"])

    band_breakdown = {}
    for lo, hi in PRICE_BANDS:
        band = f"{lo}-{hi}c"
        pnls = pnl_by_band.get(band, [])
        band_breakdown[band] = {
            "n_posted": n_by_band.get(band, 0),
            "n_filled": n_fills_by_band.get(band, 0),
            "fill_rate": (n_fills_by_band.get(band, 0) / n_by_band[band]) if n_by_band.get(band) else None,
            "total_pnl_cents": float(sum(pnls)) if pnls else 0.0,
            "mean_pnl_cents": (statistics.mean(pnls) if pnls else None),
        }

    trades_for_drawdown = [
        {"date_lst": t["date_lst"], "event_key": t["event_key"], "ticker": t["ticker"], "side": "no",
         "pnl_cents": t["pnl_cents"]}
        for t in trades
    ]

    return {
        "offer_mode": offer_mode, "contracts_per_offer": contracts, "queue_fill_fraction": queue_fill_fraction,
        "max_offers_per_event": max_offers_per_event, "fee_type": fee_type, "fee_multiplier": fee_multiplier,
        "start_date": start_date.isoformat(), "end_date": end_date.isoformat(),
        "protocol_verified": protocol_verified,
        "n_events": n_events,
        "n_offers_posted": n_offers,
        "n_fills": n_fills,
        "fill_rate": (n_fills / n_offers) if n_offers else None,
        "hit_rate": (wins / n_fills) if n_fills else None,
        "total_pnl_cents": total_pnl_cents,
        "mean_pnl_cents": (total_pnl_cents / n_fills) if n_fills else None,
        "roi_on_capital_at_risk": (total_pnl_cents / total_cost_cents) if total_cost_cents else None,
        "max_drawdown_cents": max_drawdown_cents(trades_for_drawdown) if trades_for_drawdown else 0.0,
        "worst_single_loss_cents": (min(t["pnl_cents"] for t in trades) if trades else None),
        "pnl_per_event_ci": event_clustered_bootstrap_ci(dict(pnl_by_event), n_resamples, confidence, seed),
        "pnl_by_station_cents": dict(pnl_by_station),
        "pnl_by_month_cents": dict(pnl_by_month),
        "price_band_breakdown": band_breakdown,
        "filled_vs_unfilled": fill_vs_unfilled_comparison(offers, n_resamples, confidence, seed),
        "skip_reasons": dict(skip_reasons),
        "offers": offers,
    }


# --- 7. CLI -------------------------------------------------------------------

def _split_stations(value: str) -> list[str]:
    return [s.strip().upper() for s in value.split(",") if s.strip()]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m src.research.maker_sim",
        description="Simulate resting sell-YES offers on Kalshi weather brackets and measure fills.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    default_stations = ",".join(sorted(STATION_TO_SERIES))

    run_p = sub.add_parser("run", help="Post offers over a date range and write a JSON report.")
    run_p.add_argument("--stations", default=default_stations, help="Comma-separated station codes")
    run_p.add_argument("--start", required=True, help="ISO start date (local-standard), inclusive")
    run_p.add_argument("--end", required=True, help="ISO end date, inclusive")
    run_p.add_argument("--offer", choices=OFFER_MODES, default="ask")
    run_p.add_argument("--contracts", type=int, default=DEFAULT_CONTRACTS)
    run_p.add_argument("--queue-fill-fraction", type=float, default=DEFAULT_QUEUE_FILL_FRACTION)
    run_p.add_argument("--max-offers-per-event", type=int, default=DEFAULT_MAX_OFFERS_PER_EVENT)
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
        report = run_maker_sim(
            _split_stations(args.stations), args.start, args.end,
            offer_mode=args.offer, contracts=args.contracts, queue_fill_fraction=args.queue_fill_fraction,
            max_offers_per_event=args.max_offers_per_event, fee_type=args.fee_type,
            fee_multiplier=args.fee_multiplier, period_interval=args.period_interval,
            protocol_path=args.protocol,
        )
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2, allow_nan=False, default=str))
        print(
            f"Wrote report to {out} ({report['n_offers_posted']} offers, {report['n_fills']} fills, "
            f"fill_rate={report['fill_rate']})"
        )


if __name__ == "__main__":
    main()
