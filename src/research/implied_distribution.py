"""Is the SHAPE of the Kalshi implied distribution wrong, not just its location?

See `research/implied-distribution.md` for the predeclared design this module
implements exactly -- every threshold and default here is fixed by that
document and is not tunable from this module's API to produce a result.

**The reframing.** A Kalshi daily-high event is a partition: 10-12 mutually
exclusive brackets spanning the real line whose prices sum to ~100c. The
market is therefore quoting a full discretised probability distribution over
tomorrow's max temperature, not a single number. Every backtest in this repo
so far (`weather_backtest.py`, `ensemble_model.py`, `intraday_floor.py`) has
compared brackets one at a time or bet on a better point forecast -- i.e. bet
on the distribution's LOCATION. This module asks a structurally different
question: is the distribution's SHAPE (its spread) miscalibrated? That needs
no forecast at all, only the ladder and the settled outcome.

**Randomised PIT.** For each settled event, the probability-integral-
transform value of the settled bracket is drawn uniformly within that
bracket's `[F_lo, F_hi)` interval of the (normalised) implied CDF -- the
standard fix for PIT of a discretised distribution (a plain PIT is not
uniform even under a perfectly calibrated market, because "the CDF value at
the settled bracket's edge" is ambiguous within the bracket). U-shaped means
too narrow; hump-shaped means too wide; flat means no shape edge exists.
**Fixed seed** (`PIT_SEED`, from `src.paper.uncertainty.DEFAULT_SEED`): one
`numpy.random.default_rng(seed)` draws exactly one uniform fraction per
event, consumed in a single deterministic (date, station) loop order, so a
re-run with the same seed and the same cached data reproduces identical PIT
values bracket-for-bracket.

**Mid vs. executable is drawn from the SAME uniform fraction per event.**
Both price variants of one event's PIT reuse the identical `u` -- only the
ladder's F_lo/F_hi differ between them. This is deliberate: it isolates the
mid-vs-executable disagreement (falsification check 2) to genuine price
differences, not to independent random noise that would otherwise confound
the sign-flip comparison.

**Executable price.** "The price you would actually pay" to hold one
specific bracket is the ask you'd cross to buy its YES contract -- every
bracket is independently, atomically tradeable, so no combination of fills
is needed to price it. `_executable_cents` therefore always reads the ask,
for every bracket in the ladder, regardless of which side a downstream trade
eventually takes. This mirrors `longshot-bias.md`'s "taking liquidity" cost
side, the side that closed as having no edge once the spread was paid.

**Overround.** Prices never sum to exactly 100c before normalising; that raw
sum is the market's real overround (or underround) and is recorded per event
for BOTH price variants, never silently divided away by normalisation.

**Unbounded terminal brackets -- two explicit, always-computed options**
(`TERMINAL_MODES`):
  - `"include"` (primary): the two open-tail brackets are ordinary brackets
    in the discretised CDF, normalised together with every other bracket. An
    event whose settled outcome landed in one of them contributes a PIT
    value the same way any other bracket would.
  - `"truncate"`: the two open-tail brackets are dropped before building the
    CDF and the remaining (finite) brackets are renormalised among
    themselves. An event whose settled outcome landed in an excluded tail
    has no defined PIT under this mode and is skipped for it (tallied
    separately), even though it is still used under `"include"`.
  Both modes are always computed and reported side by side, so the primary
  result's sensitivity to this choice is visible rather than asserted.

**Partition completeness** is checked once per event by reusing
`src.paper.watchlist.bracket_coverage` (feeding it entries shaped as that
function expects) -- not a second gap/overlap sweep written here. An event
whose ladder does not verifiably partition the real line (a gap, an overlap,
or a missing open tail) is refused before any CDF is built.

**Falsification checks (all four reported explicitly, never adjudicated by
this module -- only presented):**
  1. `include_mid`'s clustered CI on the squared-PIT-deviation statistic
     contains the uniform reference (1/12) -> no shape edge.
  2. The mid-price and executable-price PIT diagnostics (both under
     `"include"`) imply opposite trade directions -> spread artefact, the
     exact failure mode that closed `longshot-bias.md`'s taking-side test.
  3. The tail-only trade's event-clustered P&L CI includes zero after fees.
  4. This module's own calibration slope (fit by the identical band-slope
     method `longshot-bias.md` used, on the SAME bracket-level data collected
     here) lands closer to 1.0 (perfectly calibrated) than both
     `longshot-bias.md`'s own reading (1.11) and Le et al.'s day-ahead-horizon
     readings (0.91-0.97) -> both prior readings were noise.

**The tail trade.** Tail-only, never a condor (`kalshi-fees.md`: the fee
peaks at 50c and vanishes in the tails, so a symmetric condor pays the most
fee exactly where the market is most efficient). Only the two open,
unbounded brackets of each event are traded, at the executable price, one
order per bracket, fee charged once per order via `trading_fee_cents`, at
`DEFAULT_CONTRACTS_PER_TRADE` contracts (this repo's existing default,
imported rather than redefined). **The direction is read off the
`include_mid` PIT diagnostic ONLY** -- `evaluate_tail_trade` asserts its
`direction` argument equals what `determine_tail_direction` independently
derives from that diagnostic, so a caller cannot pass a direction chosen (or
reverse-engineered) to make the P&L come out positive.

Usage::

    from src.research.implied_distribution import run_analysis, format_report

    report = run_analysis(
        station_codes=["KNYC", "KMDW", "KMIA", "KAUS"],
        start_date="2024-10-24", end_date="2025-12-31",
    )
    print(format_report(report))
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import statistics
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterator

import numpy as np
from scipy import stats

from src.data.kalshi_history import (
    KALSHI_SETTLED_STATUSES,
    SERIES_STATIONS,
    fetch_event_at_decision,
)
from src.paper.fees import trading_fee_cents
from src.paper.uncertainty import DEFAULT_CONFIDENCE, DEFAULT_N_RESAMPLES, DEFAULT_SEED
from src.paper.watchlist import bracket_coverage
from src.research.weather_backtest import (
    DEFAULT_CONTRACTS_PER_TRADE,
    DEFAULT_FEE_MULTIPLIER,
    DEFAULT_FEE_TYPE,
    TRAIN_END,
    calibration_bins,
    event_clustered_bootstrap_ci,
    settle_pnl_cents,
)

logger = logging.getLogger(__name__)

STATION_TO_SERIES: dict[str, str] = {station: series for series, station in SERIES_STATIONS.items()}

# The two implied-CDF variants this module builds for every event. See
# module docstring's "Executable price" note for why "executable" always
# means the ask.
PRICE_FIELDS: tuple[str, ...] = ("mid", "executable")

# The two "unbounded terminal bracket" options this module always computes
# side by side. See module docstring.
TERMINAL_MODES: tuple[str, ...] = ("include", "truncate")

# Fixed seed for the randomised PIT draw AND every event-clustered bootstrap
# in this module -- reused (not rederived) from src.paper.uncertainty so a
# report is reproducible against the same cached data. Documented here
# because the module docstring's PIT explanation promises "fixed seed and
# say so."
PIT_SEED = DEFAULT_SEED

# Equal-width PIT histogram bins for the nominal (naive-N) chi-squared test.
# Matches weather_backtest.DEFAULT_CALIBRATION_BINS's choice of 10 for the
# same reason: enough resolution to see U/hump shape without so many bins
# that per-bin counts collapse to noise at this sample size.
DEFAULT_PIT_BINS = 10

# E[(U - 0.5)^2] for U ~ Uniform(0, 1) -- the reference value the
# event-clustered squared-PIT-deviation statistic is compared against.
# Above this: too narrow (U-shaped). Below this: too wide (hump-shaped).
UNIFORM_SQUARED_DEVIATION = 1.0 / 12.0

# research/longshot-bias.md: "Fitting a slope through the endpoints gives
# roughly 1.11" (our own prior measurement, same four stations).
REFERENCE_LONGSHOT_SLOPE = 1.11

# research/implied-distribution.md, quoting Le et al. Table 3: the two
# buckets spanning our day-ahead decision instant (12-24h and 24-48h).
REFERENCE_LE_SLOPES: tuple[float, float] = (0.91, 0.97)

# A calibration bin with fewer than this many bracket-outcome pairs is too
# noisy to trust in the weighted slope fit (mirrors ensemble_model.py's
# MIN_SPREAD_FIT_N reasoning: a fit needs enough points to mean anything).
MIN_CALIBRATION_BIN_N = 5


# --- Small local date helpers (duplicated across this repo's research
# modules by convention -- see src/data/weather/archive.py's own comment) --

def _as_date(value: str | date) -> date:
    return value if isinstance(value, date) else date.fromisoformat(value)


def _date_range(start: date, end: date) -> Iterator[date]:
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


# --- 1. Guardrail: training window only, 2026 holdout untouched -----------

def _enforce_train_window(end_date: date) -> None:
    """Refuse an analysis window extending past `TRAIN_END`.

    Unlike `weather_backtest.py`/`ensemble_model.py`, this module has no
    train/eval split and no protocol escape hatch: it is a diagnostic over
    settled history, not a model evaluated out-of-sample, so there is no
    legitimate reason for it to reach into the 2026 holdout at all.

    Raises:
        ValueError: If `end_date` is after `TRAIN_END`.
    """
    if end_date > TRAIN_END:
        raise ValueError(
            f"end_date {end_date} is after TRAIN_END ({TRAIN_END}); the 2026 holdout is "
            "untouched by this module -- there is no protocol override, unlike the "
            "train/eval backtests, because this is a diagnostic over settled history, not "
            "a model being evaluated out-of-sample."
        )


# --- 2. Price accessors -----------------------------------------------------

def _mid_cents(price: Any) -> float:
    return (price.yes_bid_cents + price.yes_ask_cents) / 2.0


def _executable_cents(price: Any) -> float:
    """The price you would actually pay to buy this bracket's YES contract: the ask."""
    return float(price.yes_ask_cents)


def _price_cents(row: dict, field: str) -> float:
    return _mid_cents(row["price"]) if field == "mid" else _executable_cents(row["price"])


def _bound_sort_key(row: dict) -> tuple[float, float]:
    lower = -math.inf if row["lower_bound_f"] is None else row["lower_bound_f"]
    upper = math.inf if row["upper_bound_f"] is None else row["upper_bound_f"]
    return lower, upper


def _is_terminal(row: dict) -> bool:
    return row["lower_bound_f"] is None or row["upper_bound_f"] is None


# --- 3. Per-event CDF / PIT --------------------------------------------------

def _ladder_rows_for_mode(rows_sorted: list[dict], mode: str) -> list[dict]:
    if mode not in TERMINAL_MODES:
        raise ValueError(f"Unknown terminal mode {mode!r}; expected one of {TERMINAL_MODES}")
    if mode == "include":
        return rows_sorted
    return [r for r in rows_sorted if not _is_terminal(r)]


def event_pit_values(rows_sorted: list[dict], settled_ticker: str, u: float) -> dict[tuple[str, str], float]:
    """PIT of the settled bracket for every (terminal_mode, price_field) combination.

    Args:
        rows_sorted: One event's bracket rows (as returned by
            `fetch_event_at_decision`), already sorted by `_bound_sort_key`,
            every row's `price.status == "ok"`.
        settled_ticker: The ticker of the one row whose `result == "yes"`.
        u: A single uniform-[0, 1) fraction shared by every (mode, field)
            combination for this event -- see module docstring.

    Returns:
        `{(mode, field): pit_value}`. A `(mode, field)` pair is omitted when
        `mode == "truncate"` and the settled bracket is one of the excluded
        open tails -- there is no defined PIT for it under that mode.
    """
    results: dict[tuple[str, str], float] = {}
    for mode in TERMINAL_MODES:
        ladder = _ladder_rows_for_mode(rows_sorted, mode)
        if not any(r["ticker"] == settled_ticker for r in ladder):
            continue
        for field in PRICE_FIELDS:
            prices = [_price_cents(r, field) for r in ladder]
            total = sum(prices)
            cum = 0.0
            f_lo = f_hi = None
            for price, row in zip(prices, ladder):
                f_hi = cum + price / total
                if row["ticker"] == settled_ticker:
                    f_lo = cum
                    break
                cum = f_hi
            results[(mode, field)] = f_lo + u * (f_hi - f_lo)
    return results


# --- 4. PIT diagnostic: histogram, nominal chi-squared, clustered CI --------

def pit_diagnostic(
    records: list[dict], n_bins: int = DEFAULT_PIT_BINS,
    n_resamples: int = DEFAULT_N_RESAMPLES, confidence: float = DEFAULT_CONFIDENCE, seed: int = PIT_SEED,
) -> dict[str, Any]:
    """Histogram + nominal chi-squared + event-clustered CI for one set of per-event PIT values.

    The nominal chi-squared p-value assumes `n` independent events -- exactly
    the "raw event count overstates independence" problem
    `research/implied-distribution.md` warns about (brackets within a day and
    cities within a day are correlated). It is reported for reference ONLY
    and is never the deliverable on its own: the clustered CI on the
    per-event squared-PIT-deviation statistic (via `event_clustered_bootstrap_ci`,
    this repo's existing event-level bootstrap, not a nominal-N interval) is
    what decides `shape_verdict`.

    Args:
        records: `[{"event_key", "pit", "station", "date_lst"}, ...]`, one
            entry per event.
        n_bins: Equal-width PIT histogram bins.
        n_resamples, confidence, seed: Passed to `event_clustered_bootstrap_ci`.

    Returns:
        A dict with `n_events`, `histogram`, `bin_edges`, `nominal_chi2`,
        `nominal_df`, `nominal_p_value`, `clustered_squared_deviation` (the
        `event_clustered_bootstrap_ci` result), `uniform_reference_squared_deviation`,
        and `shape_verdict` (`"too_narrow"`, `"too_wide"`, `"no_shape_edge"`,
        or `"no_data"` if `records` is empty).
    """
    n = len(records)
    base = {"n_events": n, "n_bins": n_bins}
    if n == 0:
        return {
            **base, "histogram": [], "bin_edges": [], "nominal_chi2": None, "nominal_df": None,
            "nominal_p_value": None,
            "clustered_squared_deviation": {"estimate": None, "ci_low": None, "ci_high": None, "n_events": 0},
            "uniform_reference_squared_deviation": UNIFORM_SQUARED_DEVIATION,
            "shape_verdict": "no_data",
        }
    pit_values = np.array([r["pit"] for r in records], dtype=float)
    bin_edges = np.linspace(0.0, 1.0, n_bins + 1)
    counts, _ = np.histogram(pit_values, bins=bin_edges)
    expected = n / n_bins
    chi2_stat = float(np.sum((counts - expected) ** 2 / expected))
    df = n_bins - 1
    nominal_p = float(stats.chi2.sf(chi2_stat, df))

    values_by_event = {r["event_key"]: (r["pit"] - 0.5) ** 2 for r in records}
    clustered = event_clustered_bootstrap_ci(values_by_event, n_resamples, confidence, seed)

    verdict = "no_shape_edge"
    if clustered["ci_low"] is not None and clustered["ci_low"] > UNIFORM_SQUARED_DEVIATION:
        verdict = "too_narrow"
    elif clustered["ci_high"] is not None and clustered["ci_high"] < UNIFORM_SQUARED_DEVIATION:
        verdict = "too_wide"

    return {
        **base, "histogram": counts.tolist(), "bin_edges": bin_edges.tolist(),
        "nominal_chi2": chi2_stat, "nominal_df": df, "nominal_p_value": nominal_p,
        "clustered_squared_deviation": clustered,
        "uniform_reference_squared_deviation": UNIFORM_SQUARED_DEVIATION,
        "shape_verdict": verdict,
    }


def determine_tail_direction(diagnostic: dict[str, Any]) -> str | None:
    """`"buy"` if `diagnostic` says too narrow, `"sell"` if too wide, else `None`.

    The ONLY function allowed to turn a PIT diagnostic into a trade
    direction -- `evaluate_tail_trade` asserts its caller-supplied direction
    against this function's output on the SAME diagnostic, so no caller can
    substitute a different direction.
    """
    verdict = diagnostic["shape_verdict"]
    if verdict == "too_narrow":
        return "buy"
    if verdict == "too_wide":
        return "sell"
    return None


# --- 5. Falsification check 2: mid vs. executable sign flip ----------------

def sign_flip_check(mid_diagnostic: dict[str, Any], executable_diagnostic: dict[str, Any]) -> dict[str, Any]:
    """Do the mid-price and executable-price PIT diagnostics imply opposite trade directions?

    See module docstring's falsification check 2 and `research/longshot-bias.md`:
    a signal that only exists at mid but flips (or vanishes into its mirror)
    once the spread is paid is a spread artefact, not an edge.
    """
    mid_direction = determine_tail_direction(mid_diagnostic)
    executable_direction = determine_tail_direction(executable_diagnostic)
    flipped = (
        mid_direction is not None and executable_direction is not None and mid_direction != executable_direction
    )
    return {"mid_direction": mid_direction, "executable_direction": executable_direction, "sign_flip": flipped}


# --- 6. Falsification check 4: our own calibration slope vs. the priors ----

def weighted_calibration_slope(bins: list[dict], min_bin_n: int = MIN_CALIBRATION_BIN_N) -> dict[str, Any]:
    """Weighted-least-squares slope of empirical frequency on mean predicted price.

    Same shape of analysis `longshot-bias.md`'s own band table used (a slope
    of empirical settle rate against market price, band by band), applied
    here to the identical bracket-level (mid_price, outcome) pairs already
    collected while building this module's CDFs -- not a second pass over
    the data. A slope of 1.0 is perfect calibration; `research/longshot-bias.md`
    reports ~1.11 on this same four-station data, Le et al. reports
    0.91-0.97 at this horizon (see `REFERENCE_LE_SLOPES`).

    Args:
        bins: `calibration_bins()` output.
        min_bin_n: A bin with fewer bracket-outcome pairs than this is
            dropped from the fit as too noisy to trust.

    Returns:
        `{"slope": float | None, "n_bins_used": int}`. `slope` is `None`
        when fewer than 2 usable bins remain (undefined slope) or when all
        usable bins share the same mean predicted price (zero predictor
        variance).
    """
    usable = [b for b in bins if b["n"] >= min_bin_n]
    if len(usable) < 2:
        return {"slope": None, "n_bins_used": len(usable)}
    x = np.array([b["mean_predicted_p"] for b in usable], dtype=float)
    y = np.array([b["empirical_frequency"] for b in usable], dtype=float)
    w = np.array([b["n"] for b in usable], dtype=float)
    xbar = float(np.average(x, weights=w))
    ybar = float(np.average(y, weights=w))
    sxx = float(np.sum(w * (x - xbar) ** 2))
    if sxx == 0.0:
        return {"slope": None, "n_bins_used": len(usable)}
    sxy = float(np.sum(w * (x - xbar) * (y - ybar)))
    return {"slope": sxy / sxx, "n_bins_used": len(usable)}


def priors_check(slope_ours: float | None) -> dict[str, Any]:
    """Falsification check 4: is our own slope closer to 1.0 (calibrated) than BOTH priors?

    See module docstring's falsification check 4.
    """
    if slope_ours is None:
        return {"applicable": False, "closer_than_both_priors": None}
    distance_ours = abs(slope_ours - 1.0)
    distance_longshot = abs(REFERENCE_LONGSHOT_SLOPE - 1.0)
    distance_le_closest = min(abs(s - 1.0) for s in REFERENCE_LE_SLOPES)
    return {
        "applicable": True,
        "slope_ours": slope_ours, "distance_ours": distance_ours,
        "reference_longshot_slope": REFERENCE_LONGSHOT_SLOPE, "distance_longshot": distance_longshot,
        "reference_le_slopes": REFERENCE_LE_SLOPES, "distance_le_closest": distance_le_closest,
        "closer_than_both_priors": distance_ours < distance_longshot and distance_ours < distance_le_closest,
    }


# --- 7. The tail trade -------------------------------------------------------

def evaluate_tail_trade(
    tail_rows: list[dict], direction: str | None, mid_diagnostic: dict[str, Any], *,
    contracts: int = DEFAULT_CONTRACTS_PER_TRADE, fee_type: str = DEFAULT_FEE_TYPE,
    fee_multiplier: float = DEFAULT_FEE_MULTIPLIER,
    n_resamples: int = DEFAULT_N_RESAMPLES, confidence: float = DEFAULT_CONFIDENCE, seed: int = PIT_SEED,
) -> dict[str, Any]:
    """Tail-only trade P&L, priced at the executable side, direction read off the PIT.

    Args:
        tail_rows: One row per open-tail bracket of every usable event:
            `{"event_key", "station", "date_lst", "ticker", "yes_bid_cents",
            "yes_ask_cents", "result"}`.
        direction: `"buy"` or `"sell"`, or `None` for "do not trade".
        mid_diagnostic: The `include_mid` `pit_diagnostic()` result --
            `direction` is asserted against `determine_tail_direction` of
            THIS diagnostic (see module docstring). Passing any other
            diagnostic, or a `direction` not derived from it, raises.
        contracts, fee_type, fee_multiplier: See `trading_fee_cents`.
        n_resamples, confidence, seed: Passed to `event_clustered_bootstrap_ci`.

    Returns:
        `{"traded": False, "reason": str, "direction": ...}` if `direction`
        is `None` or no tail bracket was tradeable; otherwise `{"traded":
        True, "direction", "side", "n_trades", "n_events", "total_pnl_cents",
        "mean_pnl_cents", "hit_rate", "pnl_per_event_ci", "skip_reasons",
        "trades"}`.

    Raises:
        AssertionError: If `direction` does not equal
            `determine_tail_direction(mid_diagnostic)` -- the direction may
            never be chosen independently of the PIT diagnostic.
    """
    expected_direction = determine_tail_direction(mid_diagnostic)
    assert direction == expected_direction, (
        f"Tail trade direction ({direction!r}) must equal what determine_tail_direction() reads "
        f"off the include_mid PIT diagnostic ({expected_direction!r}); it may never be chosen, "
        "overridden, or reverse-engineered to make the P&L come out positive."
    )
    if direction is None:
        return {"traded": False, "reason": "pit_uniform_no_direction", "direction": None}

    side = "yes" if direction == "buy" else "no"
    trades: list[dict] = []
    skip_reasons: Counter = Counter()
    for row in tail_rows:
        bid, ask = row["yes_bid_cents"], row["yes_ask_cents"]
        cost = ask if side == "yes" else 100 - bid
        if not 1 <= cost <= 99:
            skip_reasons["cost_out_of_range"] += 1
            continue
        if row["result"] not in ("yes", "no"):
            skip_reasons["unsettled"] += 1
            continue
        # Fee charged once per order (this bracket's whole `contracts`-sized
        # fill), never once per contract -- see trading_fee_cents/settle_pnl_cents.
        fee = trading_fee_cents(cost, contracts, fee_type=fee_type, fee_multiplier=fee_multiplier, is_maker=False)
        pnl = settle_pnl_cents(side, cost, fee, contracts, row["result"])
        trades.append({**row, "side": side, "cost_cents": cost, "fee_cents": fee,
                       "contracts": contracts, "pnl_cents": pnl})

    if not trades:
        return {"traded": False, "reason": "no_tradeable_tail_brackets", "direction": direction,
                "skip_reasons": dict(skip_reasons)}

    pnl_by_event: dict[str, float] = defaultdict(float)
    for t in trades:
        pnl_by_event[t["event_key"]] += t["pnl_cents"]
    ci = event_clustered_bootstrap_ci(dict(pnl_by_event), n_resamples, confidence, seed)
    total_pnl = float(sum(t["pnl_cents"] for t in trades))
    wins = sum(1 for t in trades if t["pnl_cents"] > 0)
    return {
        "traded": True, "direction": direction, "side": side, "n_trades": len(trades),
        "n_events": len(pnl_by_event), "total_pnl_cents": total_pnl,
        "mean_pnl_cents": total_pnl / len(trades), "hit_rate": wins / len(trades),
        "pnl_per_event_ci": ci, "skip_reasons": dict(skip_reasons), "trades": trades,
    }


# --- 8. Orchestration --------------------------------------------------------

def run_analysis(
    station_codes: list[str],
    start_date: str | date,
    end_date: str | date,
    *,
    contracts: int = DEFAULT_CONTRACTS_PER_TRADE,
    fee_type: str = DEFAULT_FEE_TYPE,
    fee_multiplier: float = DEFAULT_FEE_MULTIPLIER,
    n_bins: int = DEFAULT_PIT_BINS,
    n_resamples: int = DEFAULT_N_RESAMPLES,
    confidence: float = DEFAULT_CONFIDENCE,
    seed: int = PIT_SEED,
    session: Any = None,
) -> dict[str, Any]:
    """Build implied CDFs, compute PIT, run every falsification check, evaluate the tail trade.

    Args:
        station_codes: e.g. `["KNYC", "KMDW", "KMIA", "KAUS"]`.
        start_date, end_date: Local-standard date range (inclusive);
            `end_date` must be on or before `TRAIN_END`.
        contracts, fee_type, fee_multiplier: Passed to the tail trade.
        n_bins: PIT histogram bins.
        n_resamples, confidence, seed: Shared by every bootstrap CI in this
            report.
        session: Optional `requests.Session` (tests), passed to
            `fetch_event_at_decision`.

    Returns:
        A JSON-serializable (`allow_nan=False` safe) report dict.

    Raises:
        ValueError: If `start_date > end_date`, `end_date > TRAIN_END`, or a
            station has no mapped Kalshi series.
    """
    start_d, end_d = _as_date(start_date), _as_date(end_date)
    if start_d > end_d:
        raise ValueError(f"start_date {start_d} must be on or before end_date {end_d}")
    _enforce_train_window(end_d)

    rng = np.random.default_rng(seed)
    skip_reasons: Counter = Counter()
    pit_records: dict[tuple[str, str], list[dict]] = {
        (mode, field): [] for mode in TERMINAL_MODES for field in PRICE_FIELDS
    }
    overround_mid: list[float] = []
    overround_executable: list[float] = []
    calibration_rows: list[dict] = []
    tail_rows: list[dict] = []
    n_events_used = 0

    for target_date in _date_range(start_d, end_d):
        for station in station_codes:
            series = STATION_TO_SERIES.get(station)
            if series is None:
                raise ValueError(f"No Kalshi series mapped for station {station!r}")

            event_rows = fetch_event_at_decision(series, target_date, session=session)
            if not event_rows:
                skip_reasons["no_market_data"] += 1
                continue
            event_key = event_rows[0]["event_ticker"]

            entries = [
                {"event_key": event_key, "ticker": r["ticker"],
                 "weather_spec": {"lower_bound_f": r["lower_bound_f"], "upper_bound_f": r["upper_bound_f"]}}
                for r in event_rows
            ]
            coverage = bracket_coverage(entries).get(event_key)
            if coverage is None or not coverage["partitions"]:
                skip_reasons["not_partitioned"] += 1
                continue

            bad_price = next((r for r in event_rows if r["price"].status != "ok"), None)
            if bad_price is not None:
                skip_reasons[f"price_{bad_price['price'].status}"] += 1
                continue

            if any(r["status"] not in KALSHI_SETTLED_STATUSES for r in event_rows):
                skip_reasons["not_settled"] += 1
                continue
            if any(r["result"] not in ("yes", "no") for r in event_rows):
                skip_reasons["incomplete_settlement"] += 1
                continue
            settled = [r for r in event_rows if r["result"] == "yes"]
            if len(settled) != 1:
                skip_reasons["bad_settlement_count"] += 1
                continue
            settled_ticker = settled[0]["ticker"]

            rows_sorted = sorted(event_rows, key=_bound_sort_key)

            overround_mid.append(sum(_mid_cents(r["price"]) for r in rows_sorted))
            overround_executable.append(sum(_executable_cents(r["price"]) for r in rows_sorted))

            for r in rows_sorted:
                calibration_rows.append({
                    "p": _mid_cents(r["price"]) / 100.0,
                    "outcome": 1.0 if r["ticker"] == settled_ticker else 0.0,
                })
                if _is_terminal(r):
                    tail_rows.append({
                        "event_key": event_key, "station": station, "date_lst": target_date.isoformat(),
                        "ticker": r["ticker"], "yes_bid_cents": r["price"].yes_bid_cents,
                        "yes_ask_cents": r["price"].yes_ask_cents, "result": r["result"],
                    })

            u = float(rng.random())
            for key, pit in event_pit_values(rows_sorted, settled_ticker, u).items():
                pit_records[key].append({
                    "event_key": event_key, "pit": pit, "station": station, "date_lst": target_date.isoformat(),
                })

            n_events_used += 1

    diagnostics = {
        f"{mode}_{field}": pit_diagnostic(pit_records[(mode, field)], n_bins, n_resamples, confidence, seed)
        for mode in TERMINAL_MODES for field in PRICE_FIELDS
    }

    sign_flip = sign_flip_check(diagnostics["include_mid"], diagnostics["include_executable"])

    calibration = calibration_bins(calibration_rows, n_bins)
    slope_info = weighted_calibration_slope(calibration)
    priors = priors_check(slope_info["slope"])

    direction = determine_tail_direction(diagnostics["include_mid"])
    tail_trade = evaluate_tail_trade(
        tail_rows, direction, diagnostics["include_mid"],
        contracts=contracts, fee_type=fee_type, fee_multiplier=fee_multiplier,
        n_resamples=n_resamples, confidence=confidence, seed=seed,
    )

    tail_pnl_ci_includes_zero = None
    if tail_trade.get("traded"):
        ci = tail_trade["pnl_per_event_ci"]
        tail_pnl_ci_includes_zero = (
            ci["ci_low"] is None or (ci["ci_low"] <= 0 <= ci["ci_high"])
        )

    falsifications = {
        "1_pit_uniform_at_discounted_n": diagnostics["include_mid"]["shape_verdict"] == "no_shape_edge",
        "2_sign_flip_mid_vs_executable": sign_flip["sign_flip"],
        "3_tail_pnl_ci_includes_zero": tail_pnl_ci_includes_zero,
        "4_closer_to_uniform_than_priors": priors.get("closer_than_both_priors"),
    }

    def _overround_summary(values: list[float]) -> dict[str, Any]:
        return {
            "n": len(values),
            "mean": statistics.mean(values) if values else None,
            "median": statistics.median(values) if values else None,
        }

    return {
        "station_codes": list(station_codes), "start_date": start_d.isoformat(), "end_date": end_d.isoformat(),
        "contracts": contracts, "fee_type": fee_type, "fee_multiplier": fee_multiplier,
        "n_bins": n_bins, "n_resamples": n_resamples, "confidence": confidence, "seed": seed,
        "n_events_used": n_events_used, "skip_reasons": dict(skip_reasons),
        "overround": {"mid": _overround_summary(overround_mid), "executable": _overround_summary(overround_executable)},
        "pit_diagnostics": diagnostics,
        "sign_flip_check": sign_flip,
        "calibration_slope": slope_info,
        "priors_check": priors,
        "tail_trade": tail_trade,
        "falsifications": falsifications,
    }


# --- 9. Report ----------------------------------------------------------------

def _fmt(value: float | None, spec: str = ".4f") -> str:
    return "n/a" if value is None else format(value, spec)


def format_report(report: dict[str, Any]) -> str:
    """Human-readable report text, primary deliverable first, verdict last.

    1. Overround (the raw pre-normalisation price sum) -- a real quantity,
       not a nicety.
    2. Primary deliverable: the PIT histogram/shape diagnostic for
       `include_mid` and `include_executable`, nominal chi-squared always
       paired with the event-clustered statistic.
    3. Terminal-bracket sensitivity: the same diagnostics under `truncate`.
    4. Each falsification check's outcome, explicitly.
    5. The tail trade, if the PIT direction warranted one.
    6. Verdict, distinguishing a confident loss from an inconclusive result.
    """
    lines: list[str] = []
    r = report

    lines.append("=" * 78)
    lines.append("0. Overround (raw price sum before normalising)")
    lines.append("=" * 78)
    for field in PRICE_FIELDS:
        o = r["overround"][field]
        lines.append(f"  {field:>11}: n={o['n']:>4}  mean={_fmt(o['mean'], '.2f')}c  median={_fmt(o['median'], '.2f')}c")

    lines.append("")
    lines.append("=" * 78)
    lines.append("1. PIT shape diagnostic (primary deliverable) -- terminal brackets INCLUDED")
    lines.append("=" * 78)
    for field in PRICE_FIELDS:
        d = r["pit_diagnostics"][f"include_{field}"]
        clustered = d["clustered_squared_deviation"]
        lines.append(
            f"  {field:>11}: n_events={d['n_events']:>4}  nominal_chi2={_fmt(d['nominal_chi2'], '.2f')} "
            f"(df={d['nominal_df']}, nominal_p={_fmt(d['nominal_p_value'], '.4f')})"
        )
        lines.append(
            f"    {'':>11}  clustered E[(pit-0.5)^2]={_fmt(clustered['estimate'])} "
            f"CI=({_fmt(clustered['ci_low'])}, {_fmt(clustered['ci_high'])})  "
            f"uniform_reference={UNIFORM_SQUARED_DEVIATION:.4f}  verdict={d['shape_verdict']}"
        )

    lines.append("")
    lines.append("-" * 78)
    lines.append("2. Terminal-bracket sensitivity -- same diagnostics, TRUNCATE mode")
    lines.append("-" * 78)
    for field in PRICE_FIELDS:
        d = r["pit_diagnostics"][f"truncate_{field}"]
        clustered = d["clustered_squared_deviation"]
        lines.append(
            f"  {field:>11}: n_events={d['n_events']:>4}  clustered E[(pit-0.5)^2]={_fmt(clustered['estimate'])} "
            f"CI=({_fmt(clustered['ci_low'])}, {_fmt(clustered['ci_high'])})  verdict={d['shape_verdict']}"
        )

    lines.append("")
    lines.append("-" * 78)
    lines.append("3. Falsification checks")
    lines.append("-" * 78)
    sf = r["sign_flip_check"]
    lines.append(
        f"  1. PIT uniform at discounted N: {r['falsifications']['1_pit_uniform_at_discounted_n']} "
        f"(include_mid verdict={r['pit_diagnostics']['include_mid']['shape_verdict']})"
    )
    lines.append(
        f"  2. Sign flip mid vs. executable: {r['falsifications']['2_sign_flip_mid_vs_executable']} "
        f"(mid={sf['mid_direction']}, executable={sf['executable_direction']})"
    )
    lines.append(f"  3. Tail P&L CI includes zero: {r['falsifications']['3_tail_pnl_ci_includes_zero']}")
    pc = r["priors_check"]
    if pc["applicable"]:
        lines.append(
            f"  4. Closer to uniform than both priors: {r['falsifications']['4_closer_to_uniform_than_priors']} "
            f"(our_slope={_fmt(pc['slope_ours'])}, dist_ours={_fmt(pc['distance_ours'])}, "
            f"dist_longshot={_fmt(pc['distance_longshot'])}, dist_le_closest={_fmt(pc['distance_le_closest'])})"
        )
    else:
        lines.append("  4. Closer to uniform than both priors: not applicable (insufficient calibration bins)")

    lines.append("")
    lines.append("-" * 78)
    lines.append("4. Tail-only trade (direction read off include_mid PIT ONLY)")
    lines.append("-" * 78)
    t = r["tail_trade"]
    if not t.get("traded"):
        lines.append(f"  Not traded: {t.get('reason')}")
    else:
        ci = t["pnl_per_event_ci"]
        lines.append(
            f"  direction={t['direction']} side={t['side']} n_trades={t['n_trades']} n_events={t['n_events']} "
            f"hit_rate={_fmt(t['hit_rate'], '.3f')}"
        )
        lines.append(
            f"  total_pnl_cents={_fmt(t['total_pnl_cents'], '.1f')}  mean_pnl_cents={_fmt(t['mean_pnl_cents'], '.2f')}  "
            f"pnl_per_event_ci=({_fmt(ci['ci_low'], '.2f')}, {_fmt(ci['ci_high'], '.2f')})"
        )

    lines.append("")
    lines.append("-" * 78)
    lines.append("5. Verdict")
    lines.append("-" * 78)
    verdict_verdict = r["pit_diagnostics"]["include_mid"]["shape_verdict"]
    if verdict_verdict == "no_shape_edge":
        lines.append(
            "No shape edge: the include_mid PIT is statistically uniform at the discounted "
            "(event-clustered) sample size. A flat PIT is a perfectly good result."
        )
    elif r["falsifications"]["2_sign_flip_mid_vs_executable"]:
        lines.append(
            f"Falsified by check 2: include_mid shows {verdict_verdict!r} but the executable-price PIT "
            f"disagrees on direction ({sf['mid_direction']} vs {sf['executable_direction']}) -- a spread "
            "artefact, not an edge, the same failure mode that closed longshot-bias.md's taking-side test."
        )
    elif not t.get("traded") or t.get("reason") == "no_tradeable_tail_brackets":
        lines.append(
            f"Inconclusive: include_mid shows a {verdict_verdict!r} shape signal but the tail trade could "
            "not be evaluated (no tradeable tail brackets); no P&L verdict is available."
        )
    else:
        ci = t["pnl_per_event_ci"]
        if ci["ci_low"] is not None and ci["ci_low"] > 0:
            lines.append(
                f"Shape edge survives costs: {verdict_verdict!r} PIT, tail trade ({t['direction']}) P&L "
                f"{_fmt(ci['estimate'], '.2f')}c/event with a {r['confidence']:.0%} clustered CI "
                f"({_fmt(ci['ci_low'], '.2f')}, {_fmt(ci['ci_high'], '.2f')}) excluding zero."
            )
        elif ci["ci_high"] is not None and ci["ci_high"] < 0:
            lines.append(
                f"Confident loss: {verdict_verdict!r} PIT, but the tail trade ({t['direction']}) P&L "
                f"{r['confidence']:.0%} clustered CI ({_fmt(ci['ci_low'], '.2f')}, {_fmt(ci['ci_high'], '.2f')}) "
                "lies entirely below zero -- the shape signal does not survive fees."
            )
        else:
            lines.append(
                f"Inconclusive: {verdict_verdict!r} PIT, but the tail trade P&L CI does not exclude zero in "
                "either direction at this confidence."
            )

    return "\n".join(lines)


# --- 10. CLI ------------------------------------------------------------------

def _split_stations(value: str) -> list[str]:
    return [s.strip().upper() for s in value.split(",") if s.strip()]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m src.research.implied_distribution",
        description="Test whether the SHAPE of the Kalshi implied temperature distribution is "
                     "miscalibrated (randomised PIT), independent of forecast location.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    default_stations = ",".join(sorted(STATION_TO_SERIES))

    run_p = sub.add_parser("run", help="Run the PIT/shape analysis and write a JSON report.")
    run_p.add_argument("--stations", default=default_stations, help="Comma-separated station codes")
    run_p.add_argument("--start", required=True, help="ISO start date (local-standard), inclusive")
    run_p.add_argument("--end", required=True, help="ISO end date, inclusive (must be <= TRAIN_END)")
    run_p.add_argument("--contracts", type=int, default=DEFAULT_CONTRACTS_PER_TRADE)
    run_p.add_argument("--fee-type", default=DEFAULT_FEE_TYPE)
    run_p.add_argument("--fee-multiplier", type=float, default=DEFAULT_FEE_MULTIPLIER)
    run_p.add_argument("--output", required=True, help="Output JSON report path")

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args(argv)
    if args.command == "run":
        report = run_analysis(
            _split_stations(args.stations), args.start, args.end,
            contracts=args.contracts, fee_type=args.fee_type, fee_multiplier=args.fee_multiplier,
        )
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2, allow_nan=False, default=str))
        print(format_report(report))
        print(f"\nWrote full report to {out}")


if __name__ == "__main__":
    main()
