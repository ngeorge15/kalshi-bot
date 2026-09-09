"""Research mode: collect model/market/outcome triples without trading.

Proving predictive edge needs a model probability, a contemporaneous market
probability, and a settled outcome. None of the three requires permission to
trade -- reading a public order book is not trading. These tests pin that
research mode collects where trading mode correctly refuses, and that it never
places a position.

All market and forecast data comes from an injected fake reader; no network.
"""
from datetime import date, datetime, time, timedelta, timezone

import pytest

from src.paper.broker import PaperBroker
from src.paper.config import PaperConfig
from src.paper.observe import observe_once


def now_utc():
    """Forward mode checks event arrival against real time, so tests use it."""
    return datetime.now(timezone.utc).replace(microsecond=0)


# The strict baseline requires a complete forecast for a local day that has not
# started yet, so the fixtures target tomorrow at a fixed -5h local offset.
UTC_OFFSET_HOURS = -5


def target_date(base=None):
    return ((base or now_utc()) + timedelta(days=1)).date()


def local_day_start(base=None):
    """UTC instant at which the target local day begins."""
    return datetime.combine(target_date(base), time(),
                            timezone(timedelta(hours=UTC_OFFSET_HOURS))).astimezone(timezone.utc)


NOW = now_utc()


class FakeReader:
    """Serves canned market, orderbook and NWS payloads by URL substring."""

    def __init__(self, status="active", result=None, base=None):
        self.status = status
        self.result = result
        self.base = base or now_utc()
        self.closed = False

    def get(self, url):
        if url.endswith("/orderbook"):
            return {"orderbook_fp": {"yes_dollars": [["0.40", "20"]],
                                     "no_dollars": [["0.55", "20"]]}}
        if "/points/" in url:
            return {"properties": {"forecastHourly": "https://api.weather.gov/gridpoints/OKX/1,1/forecast/hourly"}}
        if "/forecast/hourly" in url:
            start = local_day_start(self.base)
            periods = [{"startTime": (start + timedelta(hours=h)).isoformat(),
                        "endTime": (start + timedelta(hours=h + 1)).isoformat(),
                        "temperature": 71, "temperatureUnit": "F"} for h in range(24)]
            return {"properties": {"updateTime": self.base.isoformat(), "periods": periods}}
        return {"market": {"ticker": "TEMP-NYC", "status": self.status,
                           "result": self.result,
                           "close_time": (local_day_start(self.base) + timedelta(hours=30)).isoformat()}}

    def close(self):
        self.closed = True


def entry(available=False, base=None):
    """A watchlist entry; ineligible by default, as a real unreviewed one is."""
    return {
        "ticker": "TEMP-NYC", "market_type": "temperature",
        "event_key": "NYC-2026-09-08", "latitude": 40.7794, "longitude": -73.9692,
        "rules_source": "https://example.invalid/rules",
        "weather_spec": {"date": target_date(base).isoformat(),
                         "utc_offset_hours": UTC_OFFSET_HOURS,
                         "lower_bound_f": 69.5, "upper_bound_f": 72.5},
        "eligibility": {"available": available, "source": "reviewed",
                        "checked_at": ((base or now_utc()) - timedelta(days=1)).isoformat(),
                        "expires_at": ((base or now_utc()) + timedelta(days=1)).isoformat()},
    }


def make(tmp_path, research_only, name="r.db"):
    return PaperBroker(str(tmp_path / name),
                       PaperConfig(run_kind="forward", research_only=research_only))


class TestConfig:
    def test_defaults_off(self):
        assert PaperConfig().research_only is False

    def test_rejects_non_bool(self):
        with pytest.raises(ValueError, match="research_only must be a bool"):
            PaperConfig(research_only="yes")

    def test_persists_into_the_experiment(self, tmp_path):
        broker = make(tmp_path, True)
        assert PaperBroker(broker.db_path).config.research_only is True


class TestCollectsWhereTradingRefuses:
    def test_trading_mode_skips_an_ineligible_market(self, tmp_path):
        broker = make(tmp_path, False)
        results = observe_once(broker, [entry(available=False)],
                               reader=FakeReader(), clock=now_utc)
        assert results[0]["status"] == "skipped"
        assert results[0]["reason"] == "eligibility_missing_or_expired"
        assert broker.report()["prediction_count"] == 0

    def test_research_mode_records_the_same_market(self, tmp_path):
        broker = make(tmp_path, True)
        results = observe_once(broker, [entry(available=False)],
                               reader=FakeReader(), clock=now_utc)
        assert results[0]["status"] == "observed"
        assert broker.report()["prediction_count"] == 1

    def test_research_rows_record_that_it_was_not_tradeable(self, tmp_path):
        broker = make(tmp_path, True)
        results = observe_once(broker, [entry(available=False)],
                               reader=FakeReader(), clock=now_utc)
        assert results[0]["tradeable"] is False

    def test_eligible_markets_are_marked_tradeable(self, tmp_path):
        broker = make(tmp_path, True)
        results = observe_once(broker, [entry(available=True)],
                               reader=FakeReader(), clock=now_utc)
        assert results[0]["tradeable"] is True


class TestNeverTrades:
    def test_no_orders_are_placed(self, tmp_path):
        broker = make(tmp_path, True)
        observe_once(broker, [entry(available=True)], reader=FakeReader(), clock=now_utc)
        report = broker.report()
        assert report["orders"] == []
        assert report["fills"] == []

    def test_cash_is_untouched(self, tmp_path):
        broker = make(tmp_path, True)
        before = broker.report()["cash_cents"]
        observe_once(broker, [entry(available=True)], reader=FakeReader(), clock=now_utc)
        assert broker.report()["cash_cents"] == before

    def test_forecast_is_recorded_as_research_only(self, tmp_path):
        broker = make(tmp_path, True)
        observe_once(broker, [entry(available=True)], reader=FakeReader(), clock=now_utc)
        rows = broker.report()
        assert rows["prediction_count"] == 1
        assert rows["reserved_cents"] == 0


class TestResearchTripleIsUsable:
    """Once settled, the three values must yield a paired score.

    Driven through replay mode with explicit timestamps. Forward mode anchors
    event arrival to real wall-clock time by design, so a test cannot advance
    past a market close there; the forward-specific behaviour (recording despite
    ineligibility, never trading) is covered above.
    """

    BASE = datetime(2026, 9, 4, 12, 0, 0, tzinfo=timezone.utc)

    def _settled(self, tmp_path, result="yes"):
        broker = PaperBroker(str(tmp_path / "replay.db"),
                             PaperConfig(run_kind="replay", research_only=True))
        stamp = self.BASE.isoformat()
        broker.process({"event_id": "q1", "type": "quote", "at": stamp, "observed_at": stamp,
                        "ticker": "TEMP-NYC", "market_type": "temperature",
                        "event_key": "NYC-2026-09-04", "available": True,
                        "close_at": (self.BASE + timedelta(hours=2)).isoformat(),
                        "yes_asks": [[45, 20]], "no_asks": [[60, 20]]})
        broker.process({"event_id": "p1", "type": "forecast",
                        "at": (self.BASE + timedelta(seconds=1)).isoformat(),
                        "ticker": "TEMP-NYC", "yes_probability": 0.7,
                        "model_name": "weather_hourly_normal_baseline", "model_version": "1"})
        broker.process({"event_id": "s1", "type": "settlement",
                        "at": (self.BASE + timedelta(hours=3)).isoformat(),
                        "ticker": "TEMP-NYC", "result": result})
        return broker

    def test_research_mode_places_no_order_in_replay_either(self, tmp_path):
        broker = self._settled(tmp_path)
        assert broker.report()["orders"] == []

    def test_market_probability_is_captured(self, tmp_path):
        """A paired score needs the market's probability, not only ours."""
        broker = self._settled(tmp_path)
        score = broker.report()["scores"][0]
        assert score["market_brier"] is not None
        assert score["model_brier"] is not None

    def test_outcome_comes_from_the_venue_not_our_rule_reading(self, tmp_path):
        """The settlement label is reported by the venue, so interpreting
        contract rules is not on the critical path for outcome labels."""
        broker = self._settled(tmp_path, result="no")
        assert broker.market_state("TEMP-NYC")["result"] == "no"

    def test_paired_scores_are_computable(self, tmp_path):
        broker = self._settled(tmp_path)
        scores = broker.report()["scores"]
        assert scores, "a settled research observation should produce a paired score"
        assert scores[0]["n_markets"] == 1
        assert "event_clustered" in scores[0]

    def test_clustered_block_present_for_research_data(self, tmp_path):
        broker = self._settled(tmp_path)
        clustered = broker.report()["scores"][0]["event_clustered"]
        assert clustered["n_events"] == 1
        assert clustered["ci_low"] is None  # one event cannot support an interval

    def test_model_beats_market_is_measurable(self, tmp_path):
        """p=0.7 on a YES settlement should score better than a ~0.42 market."""
        broker = self._settled(tmp_path, result="yes")
        score = broker.report()["scores"][0]
        assert score["model_brier"] < score["market_brier"]
        assert score["brier_improvement"] > 0
