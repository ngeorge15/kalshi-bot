"""Fee-aware detector for genuine arbitrage within one weather event's bracket ladder.

A weather event's N brackets are mutually exclusive and exhaustive: exactly
one settles YES, the rest NO. That gives two riskless-if-filled trades:

* **Buy-the-ladder**: buy 1 YES contract in every bracket. Whichever bracket
  wins, exactly one YES pays out 100 cents and the rest pay 0, so payout is
  always exactly 100 cents. Cost is the sum of each bracket's best ask plus
  the fee on each of those N separate orders. Genuine arbitrage iff
  ``sum(best_ask_i) + total_fees < 100``.
* **Sell-the-ladder**: buy 1 NO contract in every bracket (equivalently,
  "sell" every bracket's YES at its bid). Exactly one bracket's NO leg loses
  (the one that settles YES) and the other N-1 pay 100 cents each, so payout
  is always exactly ``100 * (N - 1)``. Cost is the sum of ``100 -
  best_bid_i`` (the NO ask) plus per-order fees. Genuine arbitrage iff
  ``sum(best_bid_i) > 100 + total_fees``.

This module does not forecast anything and holds no model of the weather.
It is arithmetic over a recorded or live order-book snapshot that is either
true or false at that instant.

**Fees, exactly.** Kalshi charges its quadratic trading fee once per *order*,
not once per contract, and each bracket leg here is a separate order, so
`total_fees` is a sum over legs of `trading_fee_cents` at that leg's own
price and the trade's contract count -- never one fee computed at a
representative price and multiplied by N, which would round differently
than N separate ceilings do. Every Kalshi weather series is confirmed
`fee_type="quadratic"` with `fee_multiplier=1` (see `src/paper/fees.py`'s
module docstring), so those are used unconditionally rather than read from
the snapshot, which does not carry them.

**Refusing a false positive is the point.** Three guards run before any
profit number is trusted, and any one of them failing makes a direction
`incomplete` rather than silently treating a gap in the data as free:

1. *Completeness*: every bracket in the event must carry a live quote on the
   side this direction needs (an ask to buy, a bid to sell). A missing leg
   cannot be assumed to cost 0 or fill at any price.
2. *Partition*: the brackets must actually tile the real line with no gaps,
   no overlaps, and both tails open (reusing `src.paper.watchlist.
   bracket_coverage`, the same check the paper-research watchlist uses for
   the same reason). A gapped or double-covered ladder, or one with a closed
   tail, breaks the "exactly one settles YES" assumption the payout math
   depends on -- the real temperature could land in the gap (nothing pays)
   or in an overlap (two brackets could pay), and a closed tail means a
   temperature outside the ladder's bounds pays nothing at all even though
   this module assumed exactly 100 (or 100*(N-1)) cents always comes back.
3. *Liveness*: every bracket must be in an open/active status with a
   `close_time` still in the future -- "future" measured against this
   snapshot's own `captured_utc`, since this module (and its `scan_file`
   replay tool) is about what was actually tradeable at the instant
   recorded, not the wall clock at analysis time. A closed or expiring
   market cannot actually be filled.

Depth is real, not assumed infinite: profit is computed against
`tradeable_contracts`, the requested contract count capped at
`limiting_size`, the smallest size available at the touched price level
across all legs. An arbitrage only a single contract deep is reported as
exactly that, not extrapolated.

**Input shape** (matches `src.live.recorder`'s snapshot exactly; see that
module's docstring)::

    {"captured_utc": str, "source": str, "event_ticker": str, "ticker": str,
     "station": str, "date_lst": str, "lower_bound_f": float | None,
     "upper_bound_f": float | None, "yes_bids": [[price_cents, size], ...],
     "yes_asks": [[price_cents, size], ...], "status": str, "close_time": str}

`scan_file` replays a recorder ndjson file: it groups snapshot lines by
`(event_ticker, captured_utc)`, runs `check_event` on each group, and
reports every found opportunity plus a scan summary. It tolerates the
recorder's own failure-reporting conventions: a poll-level failure is
written as `{"source": "gap", ...}` (see `src.live.recorder.record`), and any
line that is not valid JSON, not a JSON object, or missing a field this
module needs is treated as malformed -- both are skipped and counted, never
allowed to abort the scan or silently vanish.
"""
from __future__ import annotations

import argparse
import glob
import json
import logging
import math
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

from src.paper.fees import trading_fee_cents
from src.paper.watchlist import bracket_coverage

logger = logging.getLogger(__name__)

# Fields this module actually reads from a recorder snapshot. `lower_bound_f`
# / `upper_bound_f` must be *present* (possibly None, for an open tail);
# every other key must be present and non-null. `source`, `station`, and
# `date_lst` are part of the recorder's schema but unused here, so their
# absence does not make a line malformed.
REQUIRED_SNAPSHOT_KEYS = (
    "captured_utc", "event_ticker", "ticker", "lower_bound_f", "upper_bound_f",
    "yes_bids", "yes_asks", "status", "close_time",
)

# Mirrors src.paper.venue.KalshiVenue.parse_market's `is_open` convention:
# a market not in one of these is not tradeable.
OPEN_STATUSES = frozenset({"active", "open"})

# Every Kalshi weather series is `fee_type="quadratic"`, `fee_multiplier=1`
# (see src/paper/fees.py's module docstring) -- fixed here rather than read
# from the snapshot, which does not carry a series' fee schedule.
FEE_TYPE = "quadratic"
FEE_MULTIPLIER = 1.0

MIN_PRICE_CENTS, MAX_PRICE_CENTS = 1, 99

# Short, stable codes for scan_file to bucket incompleteness by, distinct
# from the human-readable `incomplete` message (which names the offending
# ticker(s) and so has unbounded cardinality).
CODE_NOT_PARTITIONED = "not_partitioned"
CODE_INACTIVE = "inactive_or_expired"
CODE_MISSING_QUOTE = "missing_quote"
CODE_INSUFFICIENT_DEPTH = "insufficient_depth"

SIDES = ("buy", "sell")


# --- Small local time helper (duplicated across this repo's data modules by
# convention -- see src/research/weather_backtest.py's own comment on the
# same pattern) --------------------------------------------------------------

def _parse_utc(value: object) -> datetime | None:
    """Parse a timezone-aware ISO 8601 timestamp, returning None on any failure."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _incomplete(code: str, message: str, contracts: int) -> dict:
    """The shape returned for one direction when a guard refuses to compute a profit."""
    return {
        "incomplete": message,
        "incomplete_code": code,
        "opportunity": False,
        "requested_contracts": contracts,
        "tradeable_contracts": None,
        "limiting_size": None,
        "cost_cents": None,
        "payout_cents": None,
        "fee_cents": None,
        "net_profit_cents": None,
        "legs": [],
    }


def _valid_level(level: object) -> tuple[int, float] | None:
    """Return `(price_cents, size)` if `level` is a well-formed `[price_cents, size]` book row, else None."""
    if not isinstance(level, (list, tuple)) or len(level) != 2:
        return None
    price_cents, size = level
    if isinstance(price_cents, bool) or not isinstance(price_cents, int):
        return None
    if not MIN_PRICE_CENTS <= price_cents <= MAX_PRICE_CENTS:
        return None
    if isinstance(size, bool) or not isinstance(size, (int, float)) or not math.isfinite(size) or size <= 0:
        return None
    return price_cents, size


def _check_side(
    snapshots: list[dict], side: str, contracts: int, partition_problem: str | None, inactive_tickers: list[str],
) -> dict:
    """Compute one direction ("buy" or "sell") of `check_event` for an already-validated snapshot group."""
    if partition_problem is not None:
        return _incomplete(CODE_NOT_PARTITIONED, partition_problem, contracts)
    if inactive_tickers:
        message = (f"bracket(s) not open/active with a close_time after captured_utc: "
                   f"{sorted(inactive_tickers)}")
        return _incomplete(CODE_INACTIVE, message, contracts)

    book_key = "yes_asks" if side == "buy" else "yes_bids"
    side_name = "ask" if side == "buy" else "bid"
    legs: list[dict] = []
    missing: list[str] = []
    for snap in snapshots:
        book = snap.get(book_key)
        level = _valid_level(book[0]) if isinstance(book, list) and book else None
        if level is None:
            missing.append(snap["ticker"])
            continue
        quote_price, size = level
        leg_price = quote_price if side == "buy" else 100 - quote_price
        legs.append({
            "ticker": snap["ticker"],
            "lower_bound_f": snap.get("lower_bound_f"),
            "upper_bound_f": snap.get("upper_bound_f"),
            "price_cents": leg_price,
            "size_available": size,
        })
    if missing:
        message = f"missing or invalid {side_name} quote on bracket(s): {sorted(missing)}"
        return _incomplete(CODE_MISSING_QUOTE, message, contracts)

    n_brackets = len(legs)
    limiting_size = min(leg["size_available"] for leg in legs)
    tradeable_contracts = min(contracts, math.floor(limiting_size))
    if tradeable_contracts < 1:
        message = f"less than 1 contract available at the touched price level (limiting_size={limiting_size})"
        result = _incomplete(CODE_INSUFFICIENT_DEPTH, message, contracts)
        result["limiting_size"] = limiting_size
        return result

    total_cost_cents = 0
    total_fee_cents = 0
    leg_details = []
    for leg in legs:
        fee = trading_fee_cents(
            leg["price_cents"], tradeable_contracts,
            fee_type=FEE_TYPE, fee_multiplier=FEE_MULTIPLIER, is_maker=False,
        )
        total_cost_cents += leg["price_cents"] * tradeable_contracts
        total_fee_cents += fee
        leg_details.append({**leg, "fee_cents": fee})

    payout_cents = 100 * tradeable_contracts if side == "buy" else 100 * (n_brackets - 1) * tradeable_contracts
    net_profit_cents = payout_cents - total_cost_cents - total_fee_cents

    return {
        "incomplete": None,
        "incomplete_code": None,
        "opportunity": net_profit_cents > 0,
        "requested_contracts": contracts,
        "tradeable_contracts": tradeable_contracts,
        "limiting_size": limiting_size,
        "cost_cents": total_cost_cents,
        "payout_cents": payout_cents,
        "fee_cents": total_fee_cents,
        "net_profit_cents": net_profit_cents,
        "legs": leg_details,
    }


def check_event(snapshots: list[dict], contracts: int = 1) -> dict:
    """Check one event's bracket ladder, at one instant, for genuine arbitrage both ways.

    Args:
        snapshots: One recorder-shaped snapshot dict per bracket market of a
            single event, all sharing one `event_ticker` and one
            `captured_utc` (see module docstring for the exact shape).
        contracts: Contracts requested per leg. Actual profit is computed at
            `min(contracts, limiting_size)` -- see `limiting_size` below.

    Returns:
        `{"event_ticker", "captured_utc", "n_brackets", "buy_ladder",
        "sell_ladder"}`. Each ladder dict is either:

        * Refused: `{"incomplete": <human-readable reason>, "incomplete_code":
          one of CODE_NOT_PARTITIONED/CODE_INACTIVE/CODE_MISSING_QUOTE/
          CODE_INSUFFICIENT_DEPTH, "opportunity": False, ...all other numeric
          fields None, "legs": []}`.
        * Computed: `{"incomplete": None, "incomplete_code": None,
          "opportunity": bool, "requested_contracts", "tradeable_contracts",
          "limiting_size" (the smallest size available at the touched price
          level across legs, *not* capped by `contracts` -- so a ladder only
          tradeable for 1 contract is visible as such even when `contracts`
          asked for more), "cost_cents", "payout_cents", "fee_cents",
          "net_profit_cents" (all at `tradeable_contracts`, not `contracts`),
          "legs": [{"ticker", "lower_bound_f", "upper_bound_f", "price_cents",
          "size_available", "fee_cents"}, ...]}`.

    Raises:
        ValueError: If `snapshots` is empty, `contracts` is not a positive
            int, a snapshot is not a dict or is missing a required key (see
            `REQUIRED_SNAPSHOT_KEYS`), or the snapshots do not all share one
            `event_ticker` and one `captured_utc`. These are caller-shape
            errors, distinct from the business-logic guards below that
            produce an `incomplete` ladder instead of raising.
    """
    if not snapshots:
        raise ValueError("snapshots must be a nonempty list")
    if isinstance(contracts, bool) or not isinstance(contracts, int) or contracts < 1:
        raise ValueError(f"contracts must be a positive integer, got {contracts!r}")

    event_tickers: set[str] = set()
    captured_utcs: set[str] = set()
    for snap in snapshots:
        if not isinstance(snap, dict):
            raise ValueError(f"each snapshot must be a dict, got {type(snap).__name__}")
        missing_keys = [key for key in REQUIRED_SNAPSHOT_KEYS if key not in snap]
        if missing_keys:
            raise ValueError(f"snapshot for ticker {snap.get('ticker')!r} missing key(s) {missing_keys}")
        event_tickers.add(snap["event_ticker"])
        captured_utcs.add(snap["captured_utc"])
    if len(event_tickers) != 1:
        raise ValueError(f"snapshots must all share one event_ticker, got {sorted(event_tickers)}")
    if len(captured_utcs) != 1:
        raise ValueError(f"snapshots must all share one captured_utc, got {sorted(captured_utcs)}")
    event_ticker = event_tickers.pop()
    captured_utc = captured_utcs.pop()

    now = _parse_utc(captured_utc)
    if now is None:
        raise ValueError(f"captured_utc is not a valid timezone-aware timestamp: {captured_utc!r}")

    # --- Guard: the ladder must partition the real line. ---
    coverage_entries = [
        {"event_key": event_ticker, "ticker": snap["ticker"],
         "weather_spec": {"lower_bound_f": snap["lower_bound_f"], "upper_bound_f": snap["upper_bound_f"]}}
        for snap in snapshots
    ]
    coverage = bracket_coverage(coverage_entries).get(event_ticker)
    partition_problem = None
    # bracket_coverage silently drops any entry whose bounds are unusable
    # (non-finite, disordered, or both null); a dropped entry must still
    # refuse the ladder rather than let the remaining brackets look complete.
    if coverage is None or coverage["n_brackets"] != len(snapshots) or not coverage["partitions"]:
        reasons = []
        if coverage is None:
            reasons.append("no bracket has usable (finite, ordered) bounds")
        else:
            if coverage["n_brackets"] != len(snapshots):
                reasons.append(f"{len(snapshots) - coverage['n_brackets']} bracket(s) have invalid bounds")
            if coverage["gaps"]:
                reasons.append(f"{len(coverage['gaps'])} gap(s) in coverage")
            if coverage["overlaps"]:
                reasons.append(f"{len(coverage['overlaps'])} overlapping bracket(s)")
            if not coverage["has_open_lower_tail"]:
                reasons.append("lower tail is closed (bounded, not open-ended)")
            if not coverage["has_open_upper_tail"]:
                reasons.append("upper tail is closed (bounded, not open-ended)")
        partition_problem = f"ladder does not partition the real line: {'; '.join(reasons)}"

    # --- Guard: every bracket must be open/active with a close_time still
    # ahead of this snapshot's captured_utc. ---
    inactive_tickers = []
    for snap in snapshots:
        close = _parse_utc(snap["close_time"])
        if snap["status"] not in OPEN_STATUSES or close is None or close <= now:
            inactive_tickers.append(snap["ticker"])

    return {
        "event_ticker": event_ticker,
        "captured_utc": captured_utc,
        "n_brackets": len(snapshots),
        "buy_ladder": _check_side(snapshots, "buy", contracts, partition_problem, inactive_tickers),
        "sell_ladder": _check_side(snapshots, "sell", contracts, partition_problem, inactive_tickers),
    }


# --- Replay over recorded files ---------------------------------------------

def _classify_line(line: str) -> tuple[str, dict | None]:
    """Classify one ndjson line as `("snapshot", dict)`, `("gap", dict)`, or `("malformed", None)`.

    A gap marker is `src.live.recorder.record`'s own failure-reporting
    convention: `{"source": "gap", ...}`, written in place of a snapshot
    when an entire poll cycle failed. Anything else that does not parse as a
    JSON object carrying every key in `REQUIRED_SNAPSHOT_KEYS` is malformed.
    """
    try:
        parsed = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return "malformed", None
    if not isinstance(parsed, dict):
        return "malformed", None
    if parsed.get("source") == "gap":
        return "gap", parsed
    if any(key not in parsed for key in REQUIRED_SNAPSHOT_KEYS):
        return "malformed", None
    if not isinstance(parsed.get("ticker"), str) or not isinstance(parsed.get("event_ticker"), str):
        return "malformed", None
    return "snapshot", parsed


def scan_file(path: str | Path, contracts: int = 1) -> dict:
    """Replay one recorder ndjson file, checking every recorded instant for arbitrage.

    Groups snapshot lines by `(event_ticker, captured_utc)` (a poll pass
    typically records several events, and each event several brackets, at
    the same wall-clock moment) and runs `check_event` on each group, in the
    order first encountered in the file.

    Args:
        path: Path to a recorder ndjson file (see `src.live.recorder`).
        contracts: Passed through to `check_event`.

    Returns:
        `{"opportunities": [check_event() result, ...],   # only instants
                                                            # with opportunity
                                                            # True on either side
          "n_instants_scanned": int,       # (event_ticker, captured_utc) groups
          "n_instants_incomplete": int,    # groups where buy and/or sell was incomplete
          "incomplete_reasons": {"<side>:<incomplete_code>": count, ...},
          "n_gap_lines": int,              # recorder gap markers skipped
          "n_malformed_lines": int}`       # unparseable/malformed lines skipped

        A returned `dict` rather than a bare list of opportunities, because
        the scan summary (how many instants were scanned, how many were
        incomplete and why, how many lines were unusable) is exactly as
        important a result as the opportunity list itself -- dropping it
        would let a scan of an all-`incomplete` file look identical to a
        clean scan that found nothing, which are very different findings.
    """
    groups: dict[tuple[str, str], list[dict]] = {}
    order: list[tuple[str, str]] = []
    n_gap = 0
    n_malformed = 0

    with open(path, "r") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line:
                continue
            kind, parsed = _classify_line(line)
            if kind == "gap":
                n_gap += 1
                continue
            if kind == "malformed":
                n_malformed += 1
                continue
            key = (parsed["event_ticker"], parsed["captured_utc"])
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(parsed)

    opportunities = []
    n_scanned = 0
    n_incomplete = 0
    incomplete_reasons: dict[str, int] = defaultdict(int)

    for key in order:
        n_scanned += 1
        result = check_event(groups[key], contracts=contracts)
        any_incomplete = False
        for side in SIDES:
            ladder = result[f"{side}_ladder"]
            if ladder["incomplete_code"] is not None:
                any_incomplete = True
                incomplete_reasons[f"{side}:{ladder['incomplete_code']}"] += 1
        if any_incomplete:
            n_incomplete += 1
        if result["buy_ladder"]["opportunity"] or result["sell_ladder"]["opportunity"]:
            opportunities.append(result)

    return {
        "opportunities": opportunities,
        "n_instants_scanned": n_scanned,
        "n_instants_incomplete": n_incomplete,
        "incomplete_reasons": dict(incomplete_reasons),
        "n_gap_lines": n_gap,
        "n_malformed_lines": n_malformed,
    }


def scan_paths(paths: Sequence[str | Path], contracts: int = 1) -> dict:
    """`scan_file` over several files, merged into one report (see `scan_file` for the shape)."""
    merged = {
        "opportunities": [], "n_instants_scanned": 0, "n_instants_incomplete": 0,
        "incomplete_reasons": defaultdict(int), "n_gap_lines": 0, "n_malformed_lines": 0,
    }
    for path in paths:
        result = scan_file(path, contracts=contracts)
        merged["opportunities"].extend(result["opportunities"])
        merged["n_instants_scanned"] += result["n_instants_scanned"]
        merged["n_instants_incomplete"] += result["n_instants_incomplete"]
        for reason, count in result["incomplete_reasons"].items():
            merged["incomplete_reasons"][reason] += count
        merged["n_gap_lines"] += result["n_gap_lines"]
        merged["n_malformed_lines"] += result["n_malformed_lines"]
    merged["incomplete_reasons"] = dict(merged["incomplete_reasons"])
    return merged


# --- CLI ----------------------------------------------------------------------

def _expand_inputs(patterns: list[str]) -> list[str]:
    """Expand shell-style glob patterns (or accept literal paths verbatim), de-duplicated, sorted per pattern."""
    paths: list[str] = []
    seen: set[str] = set()
    for pattern in patterns:
        matches = sorted(glob.glob(pattern))
        for match in matches if matches else [pattern]:
            if match not in seen:
                seen.add(match)
                paths.append(match)
    return paths


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m src.live.arb",
        description="Detect genuine, fee-aware arbitrage in recorded Kalshi weather bracket ladders.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    scan_p = sub.add_parser("scan", help="Scan recorded ndjson book files for arbitrage.")
    scan_p.add_argument("--input", nargs="+", required=True,
                        help="One or more ndjson paths or glob patterns (e.g. data/live/books-*.jsonl)")
    scan_p.add_argument("--contracts", type=int, default=1, help="Contracts requested per leg (default 1)")
    scan_p.add_argument("--output", default=None, help="Optional path to write the full JSON report")

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args(argv)
    if args.command != "scan":
        return

    paths = _expand_inputs(args.input)
    if not paths:
        raise SystemExit(f"No input files matched: {args.input}")

    report = scan_paths(paths, contracts=args.contracts)
    n_opportunities = len(report["opportunities"])
    print(
        f"Scanned {report['n_instants_scanned']} instant(s) across {len(paths)} file(s): "
        f"{n_opportunities} opportunit{'y' if n_opportunities == 1 else 'ies'} found, "
        f"{report['n_instants_incomplete']} instant(s) incomplete, "
        f"{report['n_gap_lines']} gap marker(s) skipped, {report['n_malformed_lines']} malformed line(s) skipped."
    )
    if report["incomplete_reasons"]:
        print("Incomplete breakdown:")
        for reason, count in sorted(report["incomplete_reasons"].items(), key=lambda kv: -kv[1]):
            print(f"  {reason}: {count}")
    for opp in report["opportunities"]:
        for side in SIDES:
            ladder = opp[f"{side}_ladder"]
            if ladder["opportunity"]:
                print(f"  {opp['event_ticker']} @ {opp['captured_utc']} [{side}]: "
                      f"net_profit_cents={ladder['net_profit_cents']} at "
                      f"{ladder['tradeable_contracts']} contract(s) (limiting_size={ladder['limiting_size']})")

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2, allow_nan=False, default=str))
        print(f"Wrote report to {out}")


if __name__ == "__main__":
    main()
