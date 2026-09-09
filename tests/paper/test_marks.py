"""Mark-to-market valuation tests: conservative pricing, staleness, empties."""
from datetime import datetime, timezone
import sqlite3

import pytest

from src.paper.broker import PaperBroker
from src.paper.marks import equity_at_market, mark_to_market


@pytest.fixture
def broker(tmp_path):
    return PaperBroker(str(tmp_path / "paper.db"))


def quote(event_id="q0", second=0, ticker="TEMP", yes_asks=None, no_asks=None, **changes):
    timestamp = f"2026-09-04T12:00:{second:02d}Z"
    return {"event_id": event_id, "type": "quote", "at": timestamp,
            "observed_at": timestamp, "ticker": ticker,
            "market_type": "temperature", "event_key": "NYC-2026-09-04",
            "close_at": "2026-09-04T18:00:00Z", "available": True,
            "yes_asks": yes_asks if yes_asks is not None else [[45, 5]],
            "no_asks": no_asks if no_asks is not None else [[60, 20]], **changes}


def order(event_id="o1", second=1, ticker="TEMP", side="yes", limit_cents=46, quantity=10, **changes):
    return {"event_id": event_id, "type": "order", "at": f"2026-09-04T12:00:{second:02d}Z",
            "ticker": ticker, "side": side, "limit_cents": limit_cents, "quantity": quantity, **changes}


def settlement(event_id="s1", ticker="TEMP", **changes):
    return {"event_id": event_id, "type": "settlement", "at": "2026-09-04T18:00:00Z",
            "ticker": ticker, "result": "yes", **changes}


def at(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc)


def test_empty_book_returns_zeros_never_raises(broker):
    result = mark_to_market(broker.db_path, at("2026-09-04T12:00:00Z"), 60)
    assert result == {"positions": [], "cost_cents": 0, "market_value_cents": 0,
                       "unrealized_pnl_cents": 0, "valued_count": 0,
                       "unvaluable_positions": [], "unvaluable_count": 0}


def test_long_yes_valued_at_complementary_no_ask_not_own_side_ask(broker):
    broker.process(quote())
    broker.process(order())
    broker.process(quote("q2", 2, yes_asks=[[44, 10]], no_asks=[[60, 20]]))
    result = mark_to_market(broker.db_path, at("2026-09-04T12:00:10Z"), 60)
    assert result["unvaluable_count"] == 0
    assert len(result["positions"]) == 1
    position = result["positions"][0]
    assert position["ticker"] == "TEMP"
    assert position["side"] == "yes"
    assert position["quantity"] == 10
    # Filled at 44+1 slippage = 45/contract, plus 2/contract fee => 470 cost.
    assert position["cost_cents"] == 470
    # Liquidation value uses the NO ask (60), not the YES ask (44/45): a long
    # YES position is worth 100-60=40/contract to close, never the price to
    # buy more YES.
    assert position["market_value_cents"] == 400
    assert position["unrealized_pnl_cents"] == -70
    assert result["cost_cents"] == 470
    assert result["market_value_cents"] == 400
    assert result["unrealized_pnl_cents"] == -70


def test_long_no_valued_at_complementary_yes_ask(broker):
    broker.process(quote())
    broker.process(order(side="no", limit_cents=61, quantity=4))
    broker.process(quote("q2", 2, yes_asks=[[45, 5]], no_asks=[[59, 20]]))
    result = mark_to_market(broker.db_path, at("2026-09-04T12:00:10Z"), 60)
    assert len(result["positions"]) == 1
    position = result["positions"][0]
    assert position["side"] == "no"
    # Liquidation value uses the YES ask (45): 100-45=55/contract.
    assert position["market_value_cents"] == 55 * 4


def test_stale_quote_position_is_unvaluable_not_priced_at_cost(broker):
    broker.process(quote())
    broker.process(order())
    broker.process(quote("q2", 2, yes_asks=[[44, 10]], no_asks=[[60, 20]]))
    # Quote observed at 12:00:02; ask for a valuation a full hour later with a
    # 60s staleness budget. This is the single most important behavior in
    # this module: a stale quote must never fall back to cost as if it were a
    # market price.
    result = mark_to_market(broker.db_path, at("2026-09-04T13:00:02Z"), 60)
    assert result["positions"] == []
    assert result["cost_cents"] == 0
    assert result["market_value_cents"] == 0
    assert result["unrealized_pnl_cents"] == 0
    assert result["unvaluable_count"] == 1
    unvaluable = result["unvaluable_positions"][0]
    assert unvaluable["ticker"] == "TEMP"
    assert unvaluable["side"] == "yes"
    assert unvaluable["cost_cents"] == 470
    assert unvaluable["reason"] == "stale_quote"
    # Cost is reported for visibility on the unvaluable row, but must never
    # be mistaken for a market value: it does not appear in any total above.
    assert unvaluable["cost_cents"] not in (result["market_value_cents"],)


def test_missing_complementary_depth_is_unvaluable(broker):
    broker.process(quote(yes_asks=[[45, 5]], no_asks=[]))
    broker.process(order())
    broker.process(quote("q2", 2, yes_asks=[[44, 10]], no_asks=[]))
    result = mark_to_market(broker.db_path, at("2026-09-04T12:00:10Z"), 60)
    assert result["positions"] == []
    assert result["unvaluable_count"] == 1
    assert result["unvaluable_positions"][0]["reason"] == "no_complementary_depth"


def test_settled_positions_are_excluded_from_open_book(broker):
    broker.process(quote())
    broker.process(order())
    broker.process(quote("q2", 2, yes_asks=[[44, 10]], no_asks=[[60, 20]]))
    broker.process(settlement())
    result = mark_to_market(broker.db_path, at("2026-09-04T18:00:01Z"), 60)
    assert result == {"positions": [], "cost_cents": 0, "market_value_cents": 0,
                       "unrealized_pnl_cents": 0, "valued_count": 0,
                       "unvaluable_positions": [], "unvaluable_count": 0}


def test_mixed_valuable_and_unvaluable_positions_totaled_separately(broker):
    # B is quoted and filled first (seconds 0-2); A is quoted and filled much
    # later (seconds 40-42). All events across the whole broker must arrive
    # in chronological order, so B necessarily comes first in wall-clock
    # terms even though it is asserted second below.
    broker.process(quote("qb0", ticker="B", event_key="OTHER"))
    broker.process(order(ticker="B", event_id="ob"))
    broker.process(quote("qb2", 2, ticker="B", event_key="OTHER", yes_asks=[[44, 10]], no_asks=[[60, 20]]))

    broker.process(quote("qa0", 40, ticker="A"))
    broker.process(order(ticker="A", event_id="oa", second=41))
    broker.process(quote("qa2", 42, ticker="A", yes_asks=[[44, 10]], no_asks=[[60, 20]]))

    # At 12:00:50 with a 30s budget: A's quote (age 8s) is fresh, B's (age
    # 48s) is stale.
    result = mark_to_market(broker.db_path, at("2026-09-04T12:00:50Z"), 30)
    assert result["valued_count"] == 1
    assert result["unvaluable_count"] == 1
    assert result["positions"][0]["ticker"] == "A"
    assert result["unvaluable_positions"][0]["ticker"] == "B"
    assert result["cost_cents"] == result["positions"][0]["cost_cents"]


def test_equity_at_market_adds_cash_and_excludes_unvaluable(broker):
    broker.process(quote())
    broker.process(order())
    broker.process(quote("q2", 2, yes_asks=[[44, 10]], no_asks=[[60, 20]]))
    now = at("2026-09-04T12:00:10Z")
    result = equity_at_market(broker.db_path, now, 60)
    report = broker.report()
    assert result["cash_cents"] == report["cash_cents"]
    assert result["market_value_cents"] == 400
    assert result["equity_at_market_cents"] == report["cash_cents"] + 400
    assert result["unvaluable_count"] == 0
    assert "evidence_note" in result and "estimate" in result["evidence_note"]


def test_equity_at_market_on_empty_book_equals_cash(broker):
    now = at("2026-09-04T12:00:00Z")
    result = equity_at_market(broker.db_path, now, 60)
    assert result["cash_cents"] == broker.config.initial_cash_cents
    assert result["equity_at_market_cents"] == broker.config.initial_cash_cents
    assert result["unvaluable_count"] == 0


def test_accepts_an_open_connection_as_well_as_a_db_path(broker):
    broker.process(quote())
    broker.process(order())
    broker.process(quote("q2", 2, yes_asks=[[44, 10]], no_asks=[[60, 20]]))
    conn = sqlite3.connect(broker.db_path)
    try:
        result = mark_to_market(conn, at("2026-09-04T12:00:10Z"), 60)
        assert result["market_value_cents"] == 400
    finally:
        conn.close()
