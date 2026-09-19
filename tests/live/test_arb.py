"""Genuine-arbitrage detector tests: hand-computed fixtures, false-positive guards, and a replay scan."""
from __future__ import annotations

import json
import random

import pytest

from src.live.arb import (
    CODE_INACTIVE,
    CODE_INSUFFICIENT_DEPTH,
    CODE_MISSING_QUOTE,
    CODE_NOT_PARTITIONED,
    FEE_MULTIPLIER,
    FEE_TYPE,
    check_event,
    scan_file,
)
from src.paper.fees import trading_fee_cents

EVENT = "KXHIGHNY-26SEP19"
CAPTURED = "2026-09-19T12:00:00Z"
FUTURE_CLOSE = "2026-09-20T00:00:00Z"
PAST_CLOSE = "2026-09-19T00:00:00Z"


def bracket(ticker, lower, upper, *, yes_bids=(), yes_asks=(), status="active",
           close_time=FUTURE_CLOSE, captured_utc=CAPTURED, event_ticker=EVENT) -> dict:
    """One recorder-shaped snapshot dict for a single bracket market."""
    return {
        "captured_utc": captured_utc,
        "source": "rest_poll",
        "event_ticker": event_ticker,
        "ticker": ticker,
        "station": "KNYC",
        "date_lst": "2026-09-19",
        "lower_bound_f": lower,
        "upper_bound_f": upper,
        "yes_bids": [list(level) for level in yes_bids],
        "yes_asks": [list(level) for level in yes_asks],
        "status": status,
        "close_time": close_time,
    }


def four_brackets(asks=None, bids=None, sizes=None) -> list[dict]:
    """A clean, partitioning 4-bracket ladder: (-inf,60) [60,70) [70,80) [80,+inf)."""
    bounds = [(None, 60.0), (60.0, 70.0), (70.0, 80.0), (80.0, None)]
    asks = asks or [None] * 4
    bids = bids or [None] * 4
    sizes = sizes or [10] * 4
    out = []
    for i, ((lo, hi), ask, bid, size) in enumerate(zip(bounds, asks, bids, sizes)):
        yes_asks = [[ask, size]] if ask is not None else []
        yes_bids = [[bid, size]] if bid is not None else []
        out.append(bracket(f"{EVENT}-B{i}", lo, hi, yes_bids=yes_bids, yes_asks=yes_asks))
    return out


def independent_fee(price_cents: int, contracts: int) -> int:
    """Recompute a single leg's fee independently of arb.py's own call, for the property test."""
    return trading_fee_cents(price_cents, contracts, fee_type=FEE_TYPE, fee_multiplier=FEE_MULTIPLIER,
                             is_maker=False)


# --- Genuine arbitrage, hand-computed --------------------------------------

def test_genuine_buy_side_arb_exact_profit():
    # Asks 20/20/20/20 = 80 total; fee at price 20, 1 contract is 2c/leg (verified
    # independently via trading_fee_cents) => total fee 8c. Cost 88 < 100 payout.
    snapshots = four_brackets(asks=[20, 20, 20, 20])
    result = check_event(snapshots, contracts=1)
    buy = result["buy_ladder"]
    assert buy["incomplete"] is None
    assert buy["opportunity"] is True
    assert buy["cost_cents"] == 80
    assert buy["fee_cents"] == 8
    assert buy["payout_cents"] == 100
    assert buy["net_profit_cents"] == 12
    assert buy["tradeable_contracts"] == 1
    assert buy["limiting_size"] == 10
    assert len(buy["legs"]) == 4
    # Sell side on the same book should not be a coincidental opportunity too.
    assert result["sell_ladder"]["incomplete"] == "missing or invalid bid quote on bracket(s): " + \
        str(sorted(l["ticker"] for l in snapshots))


def test_fee_blind_near_miss_is_actually_a_loss():
    """The single most important case: raw asks sum under 100, but fees flip it to a loss."""
    # Asks 30/30/30/9 = 99 (looks like a 1c arb if you ignore fees).
    # Fees: 30->2c (x3) + 9->1c = 7c total. Cost = 99 + 7 = 106 > 100 payout.
    snapshots = four_brackets(asks=[30, 30, 30, 9])
    result = check_event(snapshots, contracts=1)
    buy = result["buy_ladder"]
    assert buy["incomplete"] is None
    assert buy["cost_cents"] == 99
    assert buy["fee_cents"] == 7
    assert buy["net_profit_cents"] == -6
    assert buy["opportunity"] is False, "a fee-blind checker would wrongly call this an arbitrage"


def test_genuine_sell_side_arb_exact_profit():
    # Bids 30/30/30/20 = 110. NO legs cost 70/70/70/80; fees 2c each => 8c total.
    # Cost = 290 + 8 = 298; payout = 100*(4-1) = 300; profit = 2.
    snapshots = four_brackets(bids=[30, 30, 30, 20])
    result = check_event(snapshots, contracts=1)
    sell = result["sell_ladder"]
    assert sell["incomplete"] is None
    assert sell["opportunity"] is True
    assert sell["cost_cents"] == 290
    assert sell["fee_cents"] == 8
    assert sell["payout_cents"] == 300
    assert sell["net_profit_cents"] == 2
    assert sell["tradeable_contracts"] == 1
    assert [leg["price_cents"] for leg in sell["legs"]] == [70, 70, 70, 80]


# --- False-positive guards ---------------------------------------------------

def test_missing_ask_leg_reports_incomplete_not_free():
    snapshots = four_brackets(asks=[20, 20, None, 20])  # bracket 2 has no ask at all
    result = check_event(snapshots, contracts=1)
    buy = result["buy_ladder"]
    assert buy["opportunity"] is False
    assert buy["incomplete_code"] == CODE_MISSING_QUOTE
    assert f"{EVENT}-B2" in buy["incomplete"]
    assert buy["net_profit_cents"] is None
    assert buy["legs"] == []


def test_overlapping_ladder_is_refused_both_ways():
    snapshots = [
        bracket(f"{EVENT}-B0", None, 60.0, yes_asks=[[20, 10]], yes_bids=[[15, 10]]),
        bracket(f"{EVENT}-B1", 55.0, 70.0, yes_asks=[[20, 10]], yes_bids=[[15, 10]]),  # overlaps B0 on [55,60)
        bracket(f"{EVENT}-B2", 70.0, None, yes_asks=[[20, 10]], yes_bids=[[15, 10]]),
    ]
    result = check_event(snapshots, contracts=1)
    for side in ("buy_ladder", "sell_ladder"):
        assert result[side]["incomplete_code"] == CODE_NOT_PARTITIONED
        assert "overlap" in result[side]["incomplete"]
        assert result[side]["opportunity"] is False


def test_closed_tail_ladder_is_refused():
    # No open lower or upper tail: a real high outside [50, 80) settles nothing here.
    snapshots = [
        bracket(f"{EVENT}-B0", 50.0, 60.0, yes_asks=[[20, 10]], yes_bids=[[15, 10]]),
        bracket(f"{EVENT}-B1", 60.0, 70.0, yes_asks=[[20, 10]], yes_bids=[[15, 10]]),
        bracket(f"{EVENT}-B2", 70.0, 80.0, yes_asks=[[20, 10]], yes_bids=[[15, 10]]),
    ]
    result = check_event(snapshots, contracts=1)
    for side in ("buy_ladder", "sell_ladder"):
        assert result[side]["incomplete_code"] == CODE_NOT_PARTITIONED
        assert "tail" in result[side]["incomplete"]


def test_gap_in_ladder_is_refused():
    snapshots = [
        bracket(f"{EVENT}-B0", None, 60.0, yes_asks=[[20, 10]], yes_bids=[[15, 10]]),
        # Gap: nothing covers [65, 70).
        bracket(f"{EVENT}-B1", 60.0, 65.0, yes_asks=[[20, 10]], yes_bids=[[15, 10]]),
        bracket(f"{EVENT}-B2", 70.0, None, yes_asks=[[20, 10]], yes_bids=[[15, 10]]),
    ]
    result = check_event(snapshots, contracts=1)
    for side in ("buy_ladder", "sell_ladder"):
        assert result[side]["incomplete_code"] == CODE_NOT_PARTITIONED
        assert "gap" in result[side]["incomplete"]


def test_depth_limited_sizing_caps_profit_to_available_size():
    # Asks all 20c; one bracket only has 3 contracts of depth. Requesting 10
    # must size the whole ladder down to 3, not assume the other legs' size.
    snapshots = four_brackets(asks=[20, 20, 20, 20], sizes=[10, 10, 3, 10])
    result = check_event(snapshots, contracts=10)
    buy = result["buy_ladder"]
    assert buy["limiting_size"] == 3
    assert buy["requested_contracts"] == 10
    assert buy["tradeable_contracts"] == 3
    assert buy["cost_cents"] == 20 * 3 * 4  # 4 legs x 3 contracts x 20c
    assert buy["fee_cents"] == independent_fee(20, 3) * 4
    assert buy["payout_cents"] == 100 * 3
    assert buy["net_profit_cents"] == buy["payout_cents"] - buy["cost_cents"] - buy["fee_cents"]
    assert buy["opportunity"] is True


def test_insufficient_depth_below_one_contract_is_incomplete():
    snapshots = four_brackets(asks=[20, 20, 20, 20], sizes=[10, 10, 0.5, 10])
    result = check_event(snapshots, contracts=1)
    buy = result["buy_ladder"]
    assert buy["incomplete_code"] == CODE_INSUFFICIENT_DEPTH
    assert buy["opportunity"] is False


def test_closed_market_excludes_the_whole_instant():
    snapshots = four_brackets(asks=[20, 20, 20, 20], bids=[15, 15, 15, 15])
    snapshots[1]["status"] = "finalized"
    result = check_event(snapshots, contracts=1)
    for side in ("buy_ladder", "sell_ladder"):
        assert result[side]["incomplete_code"] == CODE_INACTIVE
        assert f"{EVENT}-B1" in result[side]["incomplete"]


def test_market_past_close_time_excludes_the_whole_instant():
    snapshots = four_brackets(asks=[20, 20, 20, 20], bids=[15, 15, 15, 15])
    snapshots[3]["close_time"] = PAST_CLOSE
    result = check_event(snapshots, contracts=1)
    for side in ("buy_ladder", "sell_ladder"):
        assert result[side]["incomplete_code"] == CODE_INACTIVE


# --- check_event input validation -------------------------------------------

def test_check_event_rejects_empty_input():
    with pytest.raises(ValueError):
        check_event([], contracts=1)


def test_check_event_rejects_mismatched_event_tickers():
    snapshots = four_brackets(asks=[20, 20, 20, 20])
    snapshots[0]["event_ticker"] = "KXHIGHNY-26SEP20"
    with pytest.raises(ValueError, match="event_ticker"):
        check_event(snapshots, contracts=1)


def test_check_event_rejects_non_positive_contracts():
    snapshots = four_brackets(asks=[20, 20, 20, 20])
    with pytest.raises(ValueError):
        check_event(snapshots, contracts=0)


# --- scan_file ----------------------------------------------------------------

def test_scan_file_tolerates_gaps_and_malformed_lines(tmp_path):
    # Bids included on both instants so the sell side is complete (but never
    # the opportunity here) -- this test is about gap/malformed tolerance,
    # not incompleteness, which has its own test below.
    arb_snapshots = four_brackets(asks=[20, 20, 20, 20], bids=[5, 5, 5, 5])  # genuine buy arb, 12c profit
    clean_snapshots = four_brackets(asks=[50, 50, 50, 50], bids=[10, 10, 10, 10])  # no arb either way
    for snap in clean_snapshots:
        snap["captured_utc"] = "2026-09-19T12:15:00Z"

    path = tmp_path / "books.jsonl"
    lines = [json.dumps(s) for s in arb_snapshots]
    lines.append(json.dumps({"captured_utc": "2026-09-19T12:10:00Z", "source": "gap", "error": "timeout"}))
    lines.append("{not valid json")
    lines.append(json.dumps({"captured_utc": "2026-09-19T12:12:00Z", "ticker": "incomplete-row"}))  # missing keys
    lines.extend(json.dumps(s) for s in clean_snapshots)
    path.write_text("\n".join(lines) + "\n")

    report = scan_file(str(path), contracts=1)
    assert report["n_instants_scanned"] == 2
    assert report["n_instants_incomplete"] == 0
    assert report["n_gap_lines"] == 1
    assert report["n_malformed_lines"] == 2
    assert len(report["opportunities"]) == 1
    assert report["opportunities"][0]["captured_utc"] == CAPTURED
    assert report["opportunities"][0]["buy_ladder"]["net_profit_cents"] == 12


def test_scan_file_counts_incomplete_instants_by_reason(tmp_path):
    # Bids present on every leg so only the buy side is incomplete (missing ask).
    snapshots = four_brackets(asks=[20, 20, None, 20], bids=[15, 15, 15, 15])
    path = tmp_path / "books.jsonl"
    path.write_text("\n".join(json.dumps(s) for s in snapshots) + "\n")

    report = scan_file(str(path), contracts=1)
    assert report["n_instants_scanned"] == 1
    assert report["n_instants_incomplete"] == 1
    assert report["incomplete_reasons"] == {f"buy:{CODE_MISSING_QUOTE}": 1}
    assert report["opportunities"] == []


# --- Property-style check: reported profit matches an independent recomputation ---

def test_net_profit_matches_independent_recomputation_for_random_ladders():
    rng = random.Random(20260919)
    for _ in range(200):
        n = rng.randint(2, 6)
        # n-1 interior cut points strictly increasing, in whole degrees, giving a
        # clean open-tailed partition with no gaps or overlaps.
        cuts = sorted(rng.sample(range(1, 200), n - 1))
        bounds = [(None, cuts[0])] + [(cuts[i], cuts[i + 1]) for i in range(len(cuts) - 1)] + [(cuts[-1], None)]
        contracts = rng.randint(1, 5)
        snapshots = []
        prices, sizes = [], []
        for i, (lo, hi) in enumerate(bounds):
            price = rng.randint(1, 99)
            size = rng.randint(contracts, contracts + 20)  # never the limiting leg
            prices.append(price)
            sizes.append(size)
            snapshots.append(bracket(f"PROP-B{i}", lo, hi, yes_asks=[[price, size]], yes_bids=[[price, size]]))

        result = check_event(snapshots, contracts=contracts)
        for side, book_prices in (("buy", prices), ("sell", [100 - p for p in prices])):
            ladder = result[f"{side}_ladder"]
            assert ladder["incomplete"] is None
            expected_fees = sum(independent_fee(p, contracts) for p in book_prices)
            expected_cost = sum(book_prices) * contracts
            expected_payout = 100 * contracts if side == "buy" else 100 * (n - 1) * contracts
            expected_profit = expected_payout - expected_cost - expected_fees
            assert ladder["fee_cents"] == expected_fees
            assert ladder["cost_cents"] == expected_cost
            assert ladder["net_profit_cents"] == expected_profit
            assert ladder["opportunity"] == (expected_profit > 0)
