"""Paper execution invariants, including adverse paths and restart recovery."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
import json
import sqlite3

import pytest

from src.paper.broker import PaperBroker
from src.paper.config import PaperConfig
from src.paper.cli import main, replay


@pytest.fixture
def broker(tmp_path):
    return PaperBroker(str(tmp_path / "paper.db"))


def quote(event_id="q0", second=0, ticker="TEMP", **changes):
    timestamp = f"2026-09-04T12:00:{second:02d}Z"
    return {"event_id": event_id, "type": "quote", "at": timestamp,
            "observed_at": timestamp, "ticker": ticker,
            "market_type": "temperature", "event_key": "NYC-2026-09-04",
            "close_at": "2026-09-04T13:00:00Z", "available": True,
            "yes_asks": [[45, 5]], "no_asks": [[60, 20]], **changes}


def order(event_id="o1", second=1, ticker="TEMP", **changes):
    return {"event_id": event_id, "type": "order", "at": f"2026-09-04T12:00:{second:02d}Z",
            "ticker": ticker, "side": "yes", "limit_cents": 46, "quantity": 10, **changes}


def forecast(event_id="p1", second=1, ticker="TEMP", **changes):
    return {"event_id": event_id, "type": "forecast", "at": f"2026-09-04T12:00:{second:02d}Z",
            "ticker": ticker, "yes_probability": 0.7, "model_name": "weather",
            "model_version": "1", **changes}


def settlement(event_id="s1", ticker="TEMP", **changes):
    return {"event_id": event_id, "type": "settlement", "at": "2026-09-04T13:00:00Z",
            "ticker": ticker, "result": "yes", **changes}


def assert_accounting(broker):
    report = broker.report()
    assert report["available_cash_cents"] >= 0
    assert report["cash_cents"] >= report["reserved_cents"]
    assert report["equity_at_cost_cents"] == broker.config.initial_cash_cents + report["realized_pnl_cents"]
    return report


def test_partial_fill_cancel_settle_restart_and_replay(broker):
    broker.process(quote())
    broker.process(order())
    assert broker.report()["reserved_cents"] == 480
    assert broker.report()["fills"] == []
    fill_event = quote("q2", 2, yes_asks=[[44, 3], [45, 5]])
    result = broker.process(fill_event)
    assert [f["quantity"] for f in result["fills"]] == [3, 5]
    report = assert_accounting(broker)
    assert report["cash_cents"] == 100000-381
    assert report["reserved_cents"] == 96
    assert report["orders"][0]["status"] == "partial"
    reopened = PaperBroker(broker.db_path)
    assert reopened.process(fill_event) == result
    assert reopened.report() == report
    reopened.process({"event_id": "c1", "type": "cancel", "at": "2026-09-04T12:00:03Z", "order_id": "o1"})
    assert reopened.report()["reserved_cents"] == 0
    settled = reopened.process(settlement())
    assert settled["payout_cents"] == 800
    assert settled["pnl_cents"] == 419
    assert reopened.process(settlement("s2")) == settled
    assert assert_accounting(reopened)["cash_cents"] == 100419
    with pytest.raises(ValueError, match="Conflicting"):
        reopened.process(settlement("s3", result="no"))
    assert reopened.report()["cash_cents"] == 100419


def test_no_side_prices_and_losing_settlement(broker):
    broker.process(quote())
    broker.process(order(side="no", limit_cents=61, quantity=3))
    broker.process(quote("q2", 2))
    report = assert_accounting(broker)
    assert report["fills"][0]["price_cents"] == 61
    assert report["cash_cents"] == 100000-189
    broker.process(settlement())
    assert assert_accounting(broker)["realized_pnl_cents"] == -189


def test_depth_shared_fifo_and_not_replenished_by_identical_snapshots(broker):
    broker.process(quote())
    broker.process(order(quantity=3))
    broker.process(order("o2", quantity=4))
    result = broker.process(quote("q2", 2))
    assert [(f["order_id"], f["quantity"]) for f in result["fills"]] == [("o1", 3), ("o2", 2)]
    assert PaperBroker(broker.db_path).process(quote("q3", 3))["fills"] == []
    # Only the increase from 5 to 6 supplies one additional contract.
    result = broker.process(quote("q4", 4, yes_asks=[[45, 6]]))
    assert result["fills"][0]["quantity"] == 1
    assert_accounting(broker)


def test_decision_quote_cannot_fill_and_price_must_be_executable(broker):
    broker.process(quote())
    broker.process(order(second=2, limit_cents=45))
    # observation equals order time even though it arrives later
    assert broker.process(quote("q2", 3, observed_at="2026-09-04T12:00:02Z"))["fills"] == []
    # 45 ask + 1 simulated slippage is above the 45 limit
    assert broker.process(quote("q3", 4))["fills"] == []
    assert broker.process(quote("q4", 5, yes_asks=[[44, 2]]))["fills"][0]["price_cents"] == 45


@pytest.mark.parametrize("changes", [{"observed_at": "2026-09-04T11:00:00Z"},
    {"observed_at": "2026-09-04T12:00:01Z"}, {"available": None},
    {"yes_asks": [[45, 1.5]]}, {"yes_asks": [[True, 1]]},
    {"yes_asks": [[45, 1], [45, 2]]}, {"yes_asks": [[39, 1]]},
    {"at": "2026-09-04T12:00:00"}])
def test_bad_quotes_rollback_everything(broker, changes):
    with pytest.raises((ValueError, TypeError)):
        broker.process(quote(**changes))
    with sqlite3.connect(broker.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM paper_events").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM paper_markets").fetchone()[0] == 0


def test_duplicate_ids_and_quote_time_rejected(broker):
    q = quote()
    result = broker.process(q)
    assert broker.process(q) == result
    with pytest.raises(ValueError, match="different content"):
        broker.process({**q, "yes_asks": [[46, 1]]})
    with pytest.raises(ValueError, match="advance"):
        broker.process(quote("same-observation"))
    with pytest.raises(ValueError, match="metadata"):
        broker.process(quote("q2", 2, market_type="games"))


def test_out_of_order_rejected_but_old_duplicate_can_resume(broker):
    broker.process(quote())
    event = order(second=5)
    broker.process(event)
    with pytest.raises(ValueError, match="chronological"):
        broker.process(order("earlier", second=4))
    assert broker.process(quote())["status"] == "observed"


@pytest.mark.parametrize("changes, reason", [({"available": False}, "market_unavailable"),
    ({"market_type": "games"}, "market_type_disabled")])
def test_unavailable_or_disabled_markets_never_create_orders(broker, changes, reason):
    broker.process(quote(**changes))
    assert broker.process(forecast())["reason"] == reason
    assert broker.process(order("o1", 2))["reason"] == reason
    assert broker.report()["orders"] == []


def test_market_becoming_unavailable_releases_reservations(broker):
    broker.process(quote())
    broker.process(order())
    assert broker.process(quote("q2", 2, available=False))["fills"] == []
    assert assert_accounting(broker)["reserved_cents"] == 0


def test_stale_market_blocks_forecast_and_order(broker):
    broker.process(quote())
    stale_at = "2026-09-04T12:02:00Z"
    assert broker.process(forecast(at=stale_at))["reason"] == "stale_quote"
    assert broker.process(order(at=stale_at))["reason"] == "stale_quote"


def test_close_expires_orders_without_late_fill(broker):
    broker.process(quote(close_at="2026-09-04T12:00:03Z"))
    broker.process(order())
    assert broker.process(quote("q3", 3, close_at="2026-09-04T12:00:03Z"))["fills"] == []
    assert broker.report()["reserved_cents"] == 0
    with pytest.raises(ValueError, match="Forecast must precede"):
        broker.process(forecast("late", 4))


def test_early_settlement_rejected(broker):
    broker.process(quote())
    with pytest.raises(ValueError, match="before market close"):
        broker.process(settlement(at="2026-09-04T12:00:01Z"))


def test_cash_reservations_are_atomic_across_connections(tmp_path):
    config = PaperConfig(initial_cash_cents=100, max_exposure_cents=1000)
    broker = PaperBroker(str(tmp_path / "cash.db"), config)
    broker.process(quote())
    def submit(number):
        return PaperBroker(broker.db_path).process(order(f"o{number}", quantity=2))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, [1, 2]))
    assert sorted(r["status"] for r in results) == ["rejected", "resting"]
    assert assert_accounting(broker)["available_cash_cents"] == 4


@pytest.mark.parametrize("overrides, reason", [({"max_exposure_cents": 400}, "exposure_limit"),
    ({"max_contracts_per_order": 5}, "per_order_limit"),
    ({"initial_cash_cents": 400}, "insufficient_cash")])
def test_submission_limits(tmp_path, overrides, reason):
    broker = PaperBroker(str(tmp_path / "risk.db"), replace(PaperConfig(), **overrides))
    broker.process(quote())
    assert broker.process(order())["reason"] == reason
    assert_accounting(broker)


@pytest.mark.parametrize("overrides, reason", [({"max_positions": 1}, "position_limit"),
    ({"max_event_positions": 1}, "event_position_limit")])
def test_market_and_explicit_event_limits(tmp_path, overrides, reason):
    broker = PaperBroker(str(tmp_path / "risk.db"), replace(PaperConfig(), **overrides))
    broker.process(quote())
    broker.process(quote("other", ticker="TEMP2"))
    broker.process(order())
    assert broker.process(order("o2", 2, ticker="TEMP2"))["reason"] == reason


def test_halt_persists_and_cancels_all_reservations(broker):
    broker.process(quote())
    broker.process(order())
    broker.process({"event_id": "h", "type": "halt", "at": "2026-09-04T12:00:02Z"})
    reopened = PaperBroker(broker.db_path)
    assert reopened.process(quote("q3", 3))["fills"] == []
    assert reopened.process(order("o2", 4))["reason"] == "halted"
    assert assert_accounting(reopened)["reserved_cents"] == 0


def test_daily_realized_loss_halts_at_boundary(tmp_path):
    broker = PaperBroker(str(tmp_path / "risk.db"), PaperConfig(max_daily_loss_cents=48))
    broker.process(quote())
    broker.process(order(quantity=1))
    broker.process(quote("q2", 2))
    broker.process(settlement(result="no"))
    assert broker.report()["realized_pnl_cents"] == -48
    assert PaperBroker(broker.db_path).report()["halted"]


def test_forecast_scores_include_skips_and_deduplicate_markets(broker):
    broker.process(quote())
    # No edge, but this is still a valid out-of-sample forecast to score.
    assert broker.process(forecast(yes_probability=0.425))["reason"] == "insufficient_net_edge"
    broker.process(forecast("p2", 2, yes_probability=0.9))
    broker.process(settlement())
    report = broker.report()
    assert report["prediction_count"] == 2
    assert report["scores"][0]["n_markets"] == 1
    assert report["scores"][0]["brier_improvement"] == pytest.approx(0)
    assert report["scores"][0]["model_brier"] == pytest.approx((1-0.425)**2)
    assert report["realized_pnl_cents"] == 0  # no quote after order


def test_forecast_no_side_sizing_and_model_probability_stays_yes(broker):
    broker.process(quote())
    broker.process(forecast(yes_probability=0.1))
    assert broker.report()["orders"][0]["side"] == "no"
    broker.process(quote("q2", 2))
    broker.process(settlement(result="no"))
    assert broker.report()["scores"][0]["model_brier"] == pytest.approx(0.01)


@pytest.mark.parametrize("prob", [True, -0.1, 1.1, float("nan"), float("inf")])
def test_invalid_probabilities_not_recorded(broker, prob):
    broker.process(quote())
    with pytest.raises(ValueError):
        broker.process(forecast(yes_probability=prob))
    assert broker.report()["prediction_count"] == 0


def test_forward_rejects_backdating(tmp_path):
    now = datetime(2026, 9, 4, 12, 0, 30, tzinfo=timezone.utc)
    broker = PaperBroker(str(tmp_path / "forward.db"), PaperConfig(run_kind="forward"), clock=lambda: now)
    broker.process(quote())
    with pytest.raises(ValueError, match="stale or in the future"):
        broker.process(order(at="2026-09-04T12:01:00Z"))


def test_experiment_config_immutable_and_existing_ledger_refused(tmp_path, broker):
    with pytest.raises(ValueError, match="settings differ"):
        PaperBroker(broker.db_path, PaperConfig(initial_cash_cents=50))
    path = tmp_path / "real.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE trades(id INTEGER)")
    with pytest.raises(ValueError, match="non-paper database"):
        PaperBroker(str(path))
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [("trades",)]


def test_replay_example_is_restart_safe(tmp_path):
    broker = PaperBroker(str(tmp_path / "example.db"), PaperConfig(run_kind="synthetic"))
    first = replay(broker, "examples/paper_events.jsonl")
    second = replay(PaperBroker(broker.db_path), "examples/paper_events.jsonl")
    assert first == second
    assert first["report"]["realized_pnl_cents"] == 419
    assert first["report"]["run_kind"] == "synthetic"


def test_cli_reports_errors_and_wont_overwrite_database(tmp_path, capsys):
    path = str(tmp_path / "paper.db")
    assert main(["--db", path, "report"]) == 2
    assert main(["--db", path, "init"]) == 0
    assert main(["--db", path, "report", "--output", path]) == 2
    assert PaperBroker(path).report()["cash_cents"] == 100000
    output = str(tmp_path / "report.json")
    assert main(["--db", path, "report", "--output", output]) == 0
    assert json.loads(open(output).read())["mode"] == "paper"


def test_flat_fee_model_is_the_default_and_unchanged():
    assert PaperConfig().fee_model == "flat"


def test_kalshi_fee_model_charges_the_price_dependent_formula(tmp_path):
    from src.paper.fees import trading_fee_cents

    config = PaperConfig(fee_model="kalshi", fee_type="quadratic", fee_multiplier=1)
    broker = PaperBroker(str(tmp_path / "kalshi_fees.db"), config)
    broker.process(quote())
    broker.process(order())  # side=yes, limit_cents=46, quantity=10
    result = broker.process(quote("q2", 2, yes_asks=[[44, 3], [45, 5]]))
    fills = result["fills"]
    # Fill prices are ask + slippage_cents (1): 44->45 (count 3), 45->46 (count 5).
    assert [(f["price_cents"], f["quantity"]) for f in fills] == [(45, 3), (46, 5)]
    expected = [trading_fee_cents(45, 3, fee_type="quadratic", fee_multiplier=1, is_maker=False),
                trading_fee_cents(46, 5, fee_type="quadratic", fee_multiplier=1, is_maker=False)]
    assert [f["fee_cents"] for f in fills] == expected
    # Neither price-dependent fee equals what the flat 2c/contract model would have charged,
    # proving the kalshi model is actually wired in rather than silently falling back.
    assert expected != [3 * 2, 5 * 2]
    report = broker.report()
    assert report["fees_paid_cents"] == sum(expected)
    assert report["cash_cents"] == 100_000 - (45 * 3 + expected[0]) - (46 * 5 + expected[1])
    assert report["config"]["fee_model"] == "kalshi"


def test_kalshi_fee_model_reserves_the_real_fee_before_any_fill(tmp_path):
    from src.paper.fees import trading_fee_cents

    config = PaperConfig(fee_model="kalshi", fee_type="quadratic", fee_multiplier=1)
    broker = PaperBroker(str(tmp_path / "kalshi_reserve.db"), config)
    broker.process(quote())
    broker.process(order())  # limit_cents=46, quantity=10, unfilled (resting)
    # Worst case: each contract filled separately and rounded up individually.
    fee = 10 * trading_fee_cents(46, 1, fee_type="quadratic", fee_multiplier=1, is_maker=False)
    assert broker.report()["reserved_cents"] == 46 * 10 + fee


def test_kalshi_fee_reserve_bounds_every_possible_fill(tmp_path):
    # A limit above 50 can fill lower, where the quadratic fee is larger; the
    # reservation must cover any fill price at or below the limit and any split.
    from src.paper.fees import trading_fee_cents

    broker = PaperBroker(str(tmp_path / "bound.db"), PaperConfig(fee_model="kalshi"))
    def fee(price, count):
        return trading_fee_cents(price, count, fee_type="quadratic", fee_multiplier=1, is_maker=False)
    for limit in range(1, 100):
        for count in (1, 3, 10, 100):
            reserve = broker._fee_reserve_cents(limit, count)
            for price in range(1, limit + 1):
                assert reserve >= count * fee(price, 1) >= fee(price, count)


def test_no_network_or_credentials_needed(tmp_path, monkeypatch):
    import socket
    def fail(*args, **kwargs):
        pytest.fail("Paper broker tried to access the network")
    monkeypatch.setattr(socket, "socket", fail)
    monkeypatch.delenv("KALSHI_API_KEY_ID", raising=False)
    monkeypatch.delenv("KALSHI_PRIVATE_KEY_PATH", raising=False)
    broker = PaperBroker(str(tmp_path / "isolated.db"), PaperConfig(run_kind="synthetic"))
    assert replay(broker, "examples/paper_events.jsonl")["report"]["cash_cents"] == 100419
