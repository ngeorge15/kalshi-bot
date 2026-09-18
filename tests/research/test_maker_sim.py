"""Unit tests for src/research/maker_sim.py — resting sell-YES offers on weather brackets.

No real network calls: `fetch_event_at_decision`, `fetch_ohlc_candles`, and
`get_historical_cutoff` are monkeypatched at the `src.research.maker_sim` module
level (the names that module imported into its own namespace).
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from src.data.kalshi_history import PriceAtInstant
from src.paper import protocol as protocol_mod
from src.paper.fees import trading_fee_cents
from src.research import maker_sim as ms


# ---------------------------------------------------------------------------
# offer_price_cents
# ---------------------------------------------------------------------------

class TestOfferPriceCents:
    def test_ask_mode_returns_ask_unchanged(self):
        assert ms.offer_price_cents(10, "ask") == 10

    def test_ask_minus_one_undercuts(self):
        assert ms.offer_price_cents(10, "ask-1") == 9

    def test_ask_plus_one_overcuts(self):
        assert ms.offer_price_cents(10, "ask+1") == 11

    def test_out_of_range_low_is_none(self):
        assert ms.offer_price_cents(1, "ask-1") is None  # would be 0

    def test_out_of_range_high_is_none(self):
        assert ms.offer_price_cents(99, "ask+1") is None  # would be 100

    def test_boundary_values_are_tradeable(self):
        assert ms.offer_price_cents(2, "ask-1") == 1
        assert ms.offer_price_cents(98, "ask+1") == 99

    def test_unknown_mode_raises(self):
        with pytest.raises(ValueError):
            ms.offer_price_cents(10, "bogus")


# ---------------------------------------------------------------------------
# _queue_fill_decision: deterministic per-ticker, not per-run
# ---------------------------------------------------------------------------

class TestQueueFillDecision:
    def test_fraction_zero_always_false(self):
        assert ms._queue_fill_decision("ANY-TICKER", 0.0) is False

    def test_fraction_one_always_true(self):
        assert ms._queue_fill_decision("ANY-TICKER", 1.0) is True

    def test_deterministic_across_calls(self):
        result1 = ms._queue_fill_decision("KXHIGHNY-25JUL16-B70.5", 0.5)
        result2 = ms._queue_fill_decision("KXHIGHNY-25JUL16-B70.5", 0.5)
        assert result1 == result2

    def test_distribution_roughly_matches_fraction(self):
        # Not a statistical proof -- just a sanity check that varying the ticker
        # produces a mix of True/False roughly matching the requested fraction,
        # rather than e.g. always the same answer regardless of ticker.
        tickers = [f"KXHIGHNY-25JAN{i:02d}-B70.5" for i in range(1, 29)]
        fills = [ms._queue_fill_decision(t, 0.5) for t in tickers]
        assert 0 < sum(fills) < len(fills)


# ---------------------------------------------------------------------------
# simulate_fill: the fill rule itself
# ---------------------------------------------------------------------------

class TestSimulateFill:
    T = datetime(2025, 1, 1, 5, 0, tzinfo=timezone.utc).timestamp()

    def _candle(self, minutes_after_t, bid_high):
        return {"end_period_ts": self.T + minutes_after_t * 60, "yes_bid_high_cents": bid_high,
                "yes_bid_close_cents": bid_high}

    def test_never_fills_from_a_candle_at_or_before_t(self):
        candles = [
            {"end_period_ts": self.T - 60, "yes_bid_high_cents": 99, "yes_bid_close_cents": 99},
            {"end_period_ts": self.T, "yes_bid_high_cents": 99, "yes_bid_close_cents": 99},
        ]
        result = ms.simulate_fill("T1", offer_cents=5, candles=candles, decision_ts=self.T, queue_fill_fraction=1.0)
        assert result.filled is False
        assert result.n_candles_checked == 0

    def test_strict_cross_always_fills_regardless_of_queue_fraction(self):
        candles = [self._candle(60, 8)]  # bid_high 8 > offer 5
        result = ms.simulate_fill("T1", offer_cents=5, candles=candles, decision_ts=self.T, queue_fill_fraction=0.0)
        assert result.filled is True
        assert result.fill_reason == "strict_cross"

    def test_below_offer_never_fills(self):
        candles = [self._candle(60, 3), self._candle(120, 4)]
        result = ms.simulate_fill("T1", offer_cents=5, candles=candles, decision_ts=self.T, queue_fill_fraction=1.0)
        assert result.filled is False
        assert result.max_bid_high_cents == 4

    def test_exact_touch_fills_only_when_queue_fraction_is_one(self):
        candles = [self._candle(60, 5)]  # exact touch of offer=5
        filled = ms.simulate_fill("T1", offer_cents=5, candles=candles, decision_ts=self.T, queue_fill_fraction=1.0)
        not_filled = ms.simulate_fill("T1", offer_cents=5, candles=candles, decision_ts=self.T, queue_fill_fraction=0.0)
        assert filled.filled is True
        assert filled.fill_reason == "queue_touch"
        assert not_filled.filled is False

    def test_touch_that_loses_the_queue_draw_can_still_fill_later_via_strict_cross(self):
        candles = [self._candle(60, 5), self._candle(120, 9)]
        result = ms.simulate_fill("T1", offer_cents=5, candles=candles, decision_ts=self.T, queue_fill_fraction=0.0)
        assert result.filled is True
        assert result.fill_reason == "strict_cross"
        assert result.fill_end_period_ts == self.T + 120 * 60

    def test_candles_examined_out_of_order_are_still_walked_chronologically(self):
        candles = [self._candle(120, 9), self._candle(60, 3)]
        result = ms.simulate_fill("T1", offer_cents=5, candles=candles, decision_ts=self.T, queue_fill_fraction=1.0)
        assert result.filled is True
        assert result.fill_end_period_ts == self.T + 120 * 60

    def test_none_bid_high_is_skipped_not_treated_as_zero(self):
        candles = [
            {"end_period_ts": self.T + 60, "yes_bid_high_cents": None, "yes_bid_close_cents": None},
            self._candle(120, 9),
        ]
        result = ms.simulate_fill("T1", offer_cents=5, candles=candles, decision_ts=self.T, queue_fill_fraction=1.0)
        assert result.filled is True
        assert result.n_candles_checked == 2


# ---------------------------------------------------------------------------
# settle_short_yes_pnl_cents: NO-equivalent settlement
# ---------------------------------------------------------------------------

class TestSettleShortYesPnlCents:
    def test_win_when_result_is_no(self):
        # Sold YES at 5c (== bought NO at 95c); result "no" pays 100 on NO.
        pnl = ms.settle_short_yes_pnl_cents(offer_cents=5, fee_cents=1, contracts=10, result="no")
        assert pnl == 100 * 10 - 95 * 10 - 1

    def test_loss_when_result_is_yes(self):
        pnl = ms.settle_short_yes_pnl_cents(offer_cents=5, fee_cents=1, contracts=10, result="yes")
        assert pnl == 0 - 95 * 10 - 1

    def test_fee_charged_once_not_per_contract(self):
        pnl_1 = ms.settle_short_yes_pnl_cents(offer_cents=50, fee_cents=175, contracts=100, result="no")
        assert pnl_1 == 100 * 100 - 50 * 100 - 175


# ---------------------------------------------------------------------------
# fill_vs_unfilled_comparison: the deliverable
# ---------------------------------------------------------------------------

class TestFillVsUnfilledComparison:
    def test_empty_groups_report_none(self):
        result = ms.fill_vs_unfilled_comparison([])
        assert result["filled"]["n"] == 0
        assert result["filled"]["yes_rate"] is None
        assert result["unfilled"]["n"] == 0

    def test_adverse_selection_visible_as_higher_yes_rate_when_filled(self):
        offers = (
            [{"filled": True, "result": "yes", "offer_price_cents": 10}] * 8
            + [{"filled": True, "result": "no", "offer_price_cents": 10}] * 2
            + [{"filled": False, "result": "yes", "offer_price_cents": 10}] * 2
            + [{"filled": False, "result": "no", "offer_price_cents": 10}] * 8
        )
        result = ms.fill_vs_unfilled_comparison(offers, n_resamples=200, seed=0)
        assert result["filled"]["n"] == 10
        assert result["unfilled"]["n"] == 10
        assert result["filled"]["yes_rate"] == pytest.approx(0.8)
        assert result["unfilled"]["yes_rate"] == pytest.approx(0.2)
        # Filled settles worse (more often YES) than unfilled -- adverse selection.
        assert result["filled"]["yes_rate"] > result["unfilled"]["yes_rate"]

    def test_hypothetical_pnl_uses_offer_price_ignoring_fees(self):
        offers = [{"filled": True, "result": "no", "offer_price_cents": 10}]
        result = ms.fill_vs_unfilled_comparison(offers)
        # NO-equivalent cost = 90c; result "no" -> pays 100 -> pnl = 100 - 90 = 10.
        assert result["filled"]["mean_hypothetical_pnl_per_contract"] == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# _select_offers_for_event: cheapest-ask-first, capped
# ---------------------------------------------------------------------------

class TestSelectOffersForEvent:
    def test_cheapest_ask_selected_first(self):
        candidates = [
            {"offer_price_cents": 30, "ticker": "B"},
            {"offer_price_cents": 5, "ticker": "A"},
            {"offer_price_cents": 15, "ticker": "C"},
        ]
        chosen = ms._select_offers_for_event(candidates, 1)
        assert [c["ticker"] for c in chosen] == ["A"]

    def test_cap_of_two_returns_two_cheapest(self):
        candidates = [
            {"offer_price_cents": 30, "ticker": "B"},
            {"offer_price_cents": 5, "ticker": "A"},
            {"offer_price_cents": 15, "ticker": "C"},
        ]
        chosen = ms._select_offers_for_event(candidates, 2)
        assert [c["ticker"] for c in chosen] == ["A", "C"]

    def test_ties_broken_by_ticker(self):
        candidates = [
            {"offer_price_cents": 10, "ticker": "Z"},
            {"offer_price_cents": 10, "ticker": "A"},
        ]
        chosen = ms._select_offers_for_event(candidates, 1)
        assert chosen[0]["ticker"] == "A"


# ---------------------------------------------------------------------------
# _price_band
# ---------------------------------------------------------------------------

class TestPriceBand:
    def test_bands_are_half_open_low_inclusive(self):
        assert ms._price_band(2) == "2-5c"
        assert ms._price_band(4) == "2-5c"
        assert ms._price_band(5) == "5-10c"
        assert ms._price_band(9) == "5-10c"
        assert ms._price_band(10) == "10-20c"
        assert ms._price_band(19) == "10-20c"
        assert ms._price_band(20) == "20-35c"
        assert ms._price_band(34) == "20-35c"

    def test_outside_all_bands_is_other(self):
        assert ms._price_band(1) == "other"
        assert ms._price_band(50) == "other"


# ---------------------------------------------------------------------------
# Guard: 2026 held out
# ---------------------------------------------------------------------------

def _declare_valid_protocol(**overrides):
    kwargs = dict(
        hypothesis="maker_sim: resting sell-YES offers on Kalshi weather brackets survive fills",
        primary_metric="event_clustered_pnl_per_event",
        secondary_metrics=(),
        min_events=10,
        decision_threshold=0.0,
        stopping_rule="stop after the fixed 2024-12-01..2025-12-31 evaluation window",
        run_kind="replay",
        declared_by="test-suite",
        declared_at="2025-06-01T00:00:00Z",
    )
    kwargs.update(overrides)
    return protocol_mod.declare(**kwargs)


class TestEnforcePeriodGuard:
    def test_no_guard_needed_within_hard_cutoff(self):
        assert ms._enforce_period_guard(ms.HARD_CUTOFF, None) is None

    def test_past_cutoff_without_protocol_raises(self):
        with pytest.raises(ValueError, match="predeclared"):
            ms._enforce_period_guard(ms.HARD_CUTOFF + timedelta(days=1), None)

    def test_valid_protocol_past_cutoff_is_accepted(self, tmp_path):
        protocol = _declare_valid_protocol()
        path = tmp_path / "protocol.json"
        protocol_mod.save(protocol, path)
        result = ms._enforce_period_guard(ms.HARD_CUTOFF + timedelta(days=10), path)
        assert result is not None
        assert result.content_hash == protocol.content_hash

    def test_tampered_protocol_file_is_rejected(self, tmp_path):
        protocol = _declare_valid_protocol()
        path = tmp_path / "protocol.json"
        protocol_mod.save(protocol, path)
        raw = json.loads(path.read_text())
        raw["decision_threshold"] = 999.0
        path.write_text(json.dumps(raw))
        with pytest.raises(ValueError, match="failed verification"):
            ms._enforce_period_guard(ms.HARD_CUTOFF + timedelta(days=10), path)

    def test_wrong_run_kind_rejected(self, tmp_path):
        protocol = _declare_valid_protocol(run_kind="synthetic")
        path = tmp_path / "protocol.json"
        protocol_mod.save(protocol, path)
        with pytest.raises(ValueError, match="run_kind"):
            ms._enforce_period_guard(ms.HARD_CUTOFF + timedelta(days=10), path)

    def test_missing_marker_rejected(self, tmp_path):
        protocol = _declare_valid_protocol(hypothesis="unrelated experiment")
        path = tmp_path / "protocol.json"
        protocol_mod.save(protocol, path)
        with pytest.raises(ValueError, match="marker"):
            ms._enforce_period_guard(ms.HARD_CUTOFF + timedelta(days=10), path)

    def test_missing_protocol_file_raises(self, tmp_path):
        with pytest.raises(ValueError):
            ms._enforce_period_guard(ms.HARD_CUTOFF + timedelta(days=10), tmp_path / "nope.json")


# ---------------------------------------------------------------------------
# run_maker_sim: end-to-end with fully mocked fetchers
# ---------------------------------------------------------------------------

class TestRunMakerSimEndToEnd:
    STATION = "KNYC"
    SERIES = "KXHIGHNY"
    EVAL_DATE = date(2025, 1, 1)

    def _price(self, bid, ask):
        return PriceAtInstant(
            status="ok", yes_bid_cents=bid, yes_ask_cents=ask,
            candle_end_utc="2025-01-01T04:59:00Z", age_hours=0.0, volume=10.0, open_interest=10.0,
        )

    def _event_ticker(self):
        return "KXHIGHNY-25JAN01"

    def _patch_cutoff(self, monkeypatch):
        monkeypatch.setattr(
            ms, "get_historical_cutoff",
            lambda *a, **k: {"market_settled_ts": datetime(2025, 6, 1, tzinfo=timezone.utc)},
        )

    def _patch_event(self, monkeypatch, rows):
        def fake_fetch_event(series, target_date, session=None, **kwargs):
            assert series == self.SERIES
            assert target_date == self.EVAL_DATE
            return rows
        monkeypatch.setattr(ms, "fetch_event_at_decision", fake_fetch_event)

    def _make_row(self, ticker, event_ticker, bid, ask, result, close_time="2025-01-02T05:00:00Z"):
        return {
            "ticker": ticker, "event_ticker": event_ticker, "station": self.STATION,
            "date_lst": "2025-01-01", "lower_bound_f": None, "upper_bound_f": None,
            "result": result, "settlement_source": "nws_cli", "close_time": close_time,
            "status": "finalized", "decision_time_utc": "2025-01-01T04:59:00Z",
            "price": self._price(bid, ask),
        }

    def test_cheapest_bracket_selected_and_fee_charged_once(self, monkeypatch):
        self._patch_cutoff(monkeypatch)
        event_ticker = self._event_ticker()
        rows = [
            self._make_row(f"{event_ticker}-CHEAP", event_ticker, bid=2, ask=4, result="no"),
            self._make_row(f"{event_ticker}-EXPENSIVE", event_ticker, bid=40, ask=45, result="no"),
        ]
        self._patch_event(monkeypatch, rows)

        seen_tickers = []

        def fake_ohlc(series, ticker, station, target_date, start_ts, end_ts, period_interval, session=None, cutoff=None):
            seen_tickers.append(ticker)
            # bid_high strictly exceeds the offer -> guaranteed fill.
            return [{"end_period_ts": end_ts, "yes_bid_high_cents": 99, "yes_bid_close_cents": 99}]

        monkeypatch.setattr(ms, "fetch_ohlc_candles", fake_ohlc)

        report = ms.run_maker_sim(
            [self.STATION], self.EVAL_DATE, self.EVAL_DATE, offer_mode="ask", contracts=10,
        )
        assert seen_tickers == [f"{event_ticker}-CHEAP"]
        assert report["n_offers_posted"] == 1
        assert report["n_fills"] == 1
        trade = report["offers"][0]
        assert trade["offer_price_cents"] == 4
        expected_fee = trading_fee_cents(96, 10, fee_type="quadratic", fee_multiplier=1.0, is_maker=True)
        assert trade["fee_cents"] == expected_fee
        # result "no" -> win: 100*10 - 96*10 - fee
        assert trade["pnl_cents"] == pytest.approx(100 * 10 - 96 * 10 - expected_fee)
        assert report["total_pnl_cents"] == trade["pnl_cents"]
        assert report["fill_rate"] == 1.0
        assert report["hit_rate"] == 1.0

    def test_unfilled_offer_has_zero_pnl_and_is_recorded(self, monkeypatch):
        self._patch_cutoff(monkeypatch)
        event_ticker = self._event_ticker()
        rows = [self._make_row(f"{event_ticker}-B", event_ticker, bid=2, ask=4, result="yes")]
        self._patch_event(monkeypatch, rows)

        def fake_ohlc(series, ticker, station, target_date, start_ts, end_ts, period_interval, session=None, cutoff=None):
            return [{"end_period_ts": end_ts, "yes_bid_high_cents": 1, "yes_bid_close_cents": 1}]

        monkeypatch.setattr(ms, "fetch_ohlc_candles", fake_ohlc)

        report = ms.run_maker_sim([self.STATION], self.EVAL_DATE, self.EVAL_DATE, offer_mode="ask")
        assert report["n_offers_posted"] == 1
        assert report["n_fills"] == 0
        assert report["fill_rate"] == 0.0
        assert report["total_pnl_cents"] == 0.0
        assert report["offers"][0]["filled"] is False
        assert report["offers"][0]["pnl_cents"] == 0.0

    def test_no_market_data_is_skipped(self, monkeypatch):
        self._patch_cutoff(monkeypatch)
        monkeypatch.setattr(ms, "fetch_event_at_decision", lambda *a, **k: [])
        report = ms.run_maker_sim([self.STATION], self.EVAL_DATE, self.EVAL_DATE)
        assert report["n_offers_posted"] == 0
        assert report["skip_reasons"]["no_market_data"] == 1

    def test_non_ok_price_status_is_skipped(self, monkeypatch):
        self._patch_cutoff(monkeypatch)
        event_ticker = self._event_ticker()
        stale_price = PriceAtInstant(status="stale", candle_end_utc="x", age_hours=10.0)
        rows = [{
            "ticker": f"{event_ticker}-B", "event_ticker": event_ticker, "station": self.STATION,
            "date_lst": "2025-01-01", "lower_bound_f": None, "upper_bound_f": None,
            "result": "no", "settlement_source": "nws_cli", "close_time": "2025-01-02T05:00:00Z",
            "status": "finalized", "decision_time_utc": "2025-01-01T04:59:00Z", "price": stale_price,
        }]
        self._patch_event(monkeypatch, rows)
        report = ms.run_maker_sim([self.STATION], self.EVAL_DATE, self.EVAL_DATE)
        assert report["n_offers_posted"] == 0
        assert report["skip_reasons"]["price_stale"] == 1

    def test_unsettled_result_is_skipped(self, monkeypatch):
        self._patch_cutoff(monkeypatch)
        event_ticker = self._event_ticker()
        rows = [self._make_row(f"{event_ticker}-B", event_ticker, bid=2, ask=4, result=None)]
        self._patch_event(monkeypatch, rows)
        report = ms.run_maker_sim([self.STATION], self.EVAL_DATE, self.EVAL_DATE)
        assert report["n_offers_posted"] == 0
        assert report["skip_reasons"]["unsettled_result"] == 1

    def test_untradeable_offer_price_is_skipped(self, monkeypatch):
        self._patch_cutoff(monkeypatch)
        event_ticker = self._event_ticker()
        # ask=99, offer_mode="ask+1" -> 100, untradeable.
        rows = [self._make_row(f"{event_ticker}-B", event_ticker, bid=98, ask=99, result="no")]
        self._patch_event(monkeypatch, rows)
        report = ms.run_maker_sim([self.STATION], self.EVAL_DATE, self.EVAL_DATE, offer_mode="ask+1")
        assert report["n_offers_posted"] == 0
        assert report["skip_reasons"]["untradeable_offer_price"] == 1

    def test_start_after_end_raises(self):
        with pytest.raises(ValueError):
            ms.run_maker_sim([self.STATION], date(2025, 1, 2), date(2025, 1, 1))

    def test_unknown_offer_mode_raises(self):
        with pytest.raises(ValueError):
            ms.run_maker_sim([self.STATION], self.EVAL_DATE, self.EVAL_DATE, offer_mode="bogus")

    def test_eval_past_hard_cutoff_without_protocol_raises(self, monkeypatch):
        self._patch_cutoff(monkeypatch)
        with pytest.raises(ValueError, match="predeclared"):
            ms.run_maker_sim(
                [self.STATION], ms.HARD_CUTOFF + timedelta(days=1), ms.HARD_CUTOFF + timedelta(days=1),
            )

    def test_report_is_json_serializable_with_allow_nan_false(self, monkeypatch):
        self._patch_cutoff(monkeypatch)
        event_ticker = self._event_ticker()
        rows = [self._make_row(f"{event_ticker}-B", event_ticker, bid=2, ask=4, result="no")]
        self._patch_event(monkeypatch, rows)
        monkeypatch.setattr(
            ms, "fetch_ohlc_candles",
            lambda *a, **k: [{"end_period_ts": 0, "yes_bid_high_cents": 99, "yes_bid_close_cents": 99}],
        )
        report = ms.run_maker_sim([self.STATION], self.EVAL_DATE, self.EVAL_DATE)
        text = json.dumps(report, allow_nan=False, default=str)
        assert isinstance(text, str)

    def test_max_offers_per_event_posts_more_than_one(self, monkeypatch):
        self._patch_cutoff(monkeypatch)
        event_ticker = self._event_ticker()
        rows = [
            self._make_row(f"{event_ticker}-A", event_ticker, bid=2, ask=4, result="no"),
            self._make_row(f"{event_ticker}-B", event_ticker, bid=10, ask=12, result="no"),
            self._make_row(f"{event_ticker}-C", event_ticker, bid=30, ask=32, result="yes"),
        ]
        self._patch_event(monkeypatch, rows)
        monkeypatch.setattr(
            ms, "fetch_ohlc_candles",
            lambda *a, **k: [{"end_period_ts": 0, "yes_bid_high_cents": 1, "yes_bid_close_cents": 1}],
        )
        report = ms.run_maker_sim([self.STATION], self.EVAL_DATE, self.EVAL_DATE, max_offers_per_event=2)
        assert report["n_offers_posted"] == 2
        posted_tickers = {o["ticker"] for o in report["offers"]}
        assert posted_tickers == {f"{event_ticker}-A", f"{event_ticker}-B"}
