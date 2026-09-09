"""Venue seam: collection must work against a venue with a different API shape.

No Polymarket adapter is shipped, because writing one from memory rather than
from verified responses would produce something that looks finished and fails on
first contact. Instead these tests prove the seam is real by driving the whole
collection path with an invented venue whose URLs, payload envelope and price
convention differ from Kalshi's in every respect. If that works, a real second
adapter is an afternoon's work against real responses.
"""
from datetime import datetime, time, timedelta, timezone

import pytest

from src.paper.broker import PaperBroker
from src.paper.config import PaperConfig
from src.paper.observe import PublicData, observe_once
from src.paper.venue import DEFAULT_VENUE, KalshiVenue, Venue, asks_from_orderbook


def now_utc():
    return datetime.now(timezone.utc).replace(microsecond=0)


UTC_OFFSET_HOURS = -5


def local_day_start(base=None):
    target = ((base or now_utc()) + timedelta(days=1)).date()
    return datetime.combine(target, time(),
                            timezone(timedelta(hours=UTC_OFFSET_HOURS))).astimezone(timezone.utc)


class OtherVenue:
    """A deliberately un-Kalshi-like venue.

    Different host, different URL layout, a flat payload with no envelope,
    different status vocabulary, and a book quoted directly as probabilities
    rather than complementary fixed-point bids.
    """

    name = "othervenue"
    hosts = frozenset({"api.othervenue.test"})

    def market_url(self, ticker):
        return f"https://api.othervenue.test/v3/contract?id={ticker}"

    def orderbook_url(self, ticker):
        return f"https://api.othervenue.test/v3/contract/{ticker}/depth"

    def parse_market(self, payload):
        state = payload["state"]
        return {"ticker": payload["id"], "status": "settled" if state == "resolved" else state,
                "result": payload.get("resolution"), "close_time": payload["expiry"],
                "is_open": state == "trading", "raw": payload}

    def parse_orderbook(self, payload):
        # Quoted as probabilities; convert to integer-cent asks on both sides.
        yes = int(round(payload["yes_prob"] * 100))
        return {"yes_asks": [[yes, payload["size"]]],
                "no_asks": [[100 - yes + 5, payload["size"]]]}


class OtherReader:
    """Serves OtherVenue-shaped payloads plus NWS forecasts."""

    def __init__(self, state="trading", resolution=None):
        self.state = state
        self.resolution = resolution
        self.base = now_utc()
        self.urls = []

    def get(self, url):
        self.urls.append(url)
        if "/depth" in url:
            return {"yes_prob": 0.44, "size": 20}
        if "/points/" in url:
            return {"properties": {"forecastHourly":
                                   "https://api.weather.gov/gridpoints/OKX/1,1/forecast/hourly"}}
        if "/forecast/hourly" in url:
            start = local_day_start(self.base)
            periods = [{"startTime": (start + timedelta(hours=h)).isoformat(),
                        "endTime": (start + timedelta(hours=h + 1)).isoformat(),
                        "temperature": 71, "temperatureUnit": "F"} for h in range(24)]
            return {"properties": {"updateTime": self.base.isoformat(), "periods": periods}}
        return {"id": "OTHER-NYC", "state": self.state, "resolution": self.resolution,
                "expiry": (local_day_start(self.base) + timedelta(hours=30)).isoformat()}

    def close(self):
        pass


def entry():
    base = now_utc()
    target = (base + timedelta(days=1)).date()
    return {
        "ticker": "OTHER-NYC", "market_type": "temperature",
        "event_key": f"NYC-{target.isoformat()}", "latitude": 40.7794, "longitude": -73.9692,
        "rules_source": "https://example.invalid/rules",
        "weather_spec": {"date": target.isoformat(), "utc_offset_hours": UTC_OFFSET_HOURS,
                         "lower_bound_f": 69.5, "upper_bound_f": 72.5},
        "eligibility": {"available": False, "source": "reviewed",
                        "checked_at": (base - timedelta(days=1)).isoformat(),
                        "expires_at": (base + timedelta(days=1)).isoformat()},
    }


class TestKalshiAdapter:
    def test_satisfies_the_protocol(self):
        assert isinstance(KalshiVenue(), Venue)

    def test_default_venue_is_kalshi(self):
        assert DEFAULT_VENUE.name == "kalshi"

    def test_market_url_escapes_the_ticker(self):
        assert "A%2FB" in KalshiVenue().market_url("A/B")

    def test_orderbook_url_extends_market_url(self):
        venue = KalshiVenue()
        assert venue.orderbook_url("T") == venue.market_url("T") + "/orderbook"

    def test_parse_market_normalises_status(self):
        payload = {"market": {"ticker": "T", "status": "active", "result": None,
                              "close_time": "2026-09-09T00:00:00Z"}}
        parsed = KalshiVenue().parse_market(payload)
        assert parsed["is_open"] is True and parsed["result"] is None

    def test_parse_market_reports_settlement(self):
        payload = {"market": {"ticker": "T", "status": "settled", "result": "yes",
                              "close_time": "2026-09-09T00:00:00Z"}}
        parsed = KalshiVenue().parse_market(payload)
        assert parsed["is_open"] is False and parsed["result"] == "yes"

    def test_empty_result_string_becomes_none(self):
        payload = {"market": {"ticker": "T", "status": "active", "result": "",
                              "close_time": "2026-09-09T00:00:00Z"}}
        assert KalshiVenue().parse_market(payload)["result"] is None

    def test_orderbook_is_the_complement_of_the_opposite_bid(self):
        book = {"orderbook_fp": {"yes_dollars": [["0.60", "5"]], "no_dollars": [["0.35", "7"]]}}
        parsed = KalshiVenue().parse_orderbook(book)
        assert parsed["yes_asks"] == [[65, 7]]   # 1 - 0.35
        assert parsed["no_asks"] == [[40, 5]]    # 1 - 0.60

    def test_rounding_never_flatters_the_fill(self):
        """Prices round up, quantities round down."""
        parsed = asks_from_orderbook(
            {"orderbook_fp": {"yes_dollars": [], "no_dollars": [["0.404", "3.9"]]}})
        assert parsed["yes_asks"] == [[60, 3]]

    def test_malformed_level_raises(self):
        with pytest.raises(ValueError, match="Invalid fixed-point book level"):
            asks_from_orderbook({"orderbook_fp": {"yes_dollars": [["nope", "1"]], "no_dollars": []}})


class TestAllowedHostsFollowTheVenue:
    def test_kalshi_hosts_accepted(self):
        reader = PublicData()
        assert "external-api.kalshi.com" in reader.allowed_hosts
        assert "api.weather.gov" in reader.allowed_hosts

    def test_other_venue_host_accepted_and_kalshi_not(self):
        reader = PublicData(venue=OtherVenue())
        assert "api.othervenue.test" in reader.allowed_hosts
        assert "external-api.kalshi.com" not in reader.allowed_hosts

    def test_weather_host_always_allowed(self):
        assert "api.weather.gov" in PublicData(venue=OtherVenue()).allowed_hosts

    def test_unknown_host_rejected(self):
        with pytest.raises(ValueError, match="Unexpected public-data URL"):
            PublicData().get("https://evil.test/markets/T")

    def test_plain_http_rejected(self):
        with pytest.raises(ValueError, match="Unexpected public-data URL"):
            PublicData().get("http://external-api.kalshi.com/x")


class TestCollectionThroughAnotherVenue:
    """The seam is only real if the whole path runs on a foreign API shape."""

    def _broker(self, tmp_path):
        return PaperBroker(str(tmp_path / "other.db"),
                           PaperConfig(run_kind="forward", research_only=True))

    def test_observes_and_records(self, tmp_path):
        broker = self._broker(tmp_path)
        results = observe_once(broker, [entry()], reader=OtherReader(),
                               clock=now_utc, venue=OtherVenue())
        assert results[0]["status"] == "observed"
        assert broker.report()["prediction_count"] == 1

    def test_uses_the_foreign_url_layout(self, tmp_path):
        reader = OtherReader()
        observe_once(self._broker(tmp_path), [entry()], reader=reader,
                     clock=now_utc, venue=OtherVenue())
        assert any("/v3/contract?id=OTHER-NYC" in u for u in reader.urls)
        assert any("/v3/contract/OTHER-NYC/depth" in u for u in reader.urls)
        assert not any("kalshi" in u for u in reader.urls)

    def test_foreign_price_convention_reaches_the_book(self, tmp_path):
        broker = self._broker(tmp_path)
        observe_once(broker, [entry()], reader=OtherReader(),
                     clock=now_utc, venue=OtherVenue())
        state = broker.market_state("OTHER-NYC")
        assert state is not None
        import json
        quote = json.loads(state["quote_json"])
        assert quote["yes_asks"] == [[44, 20]]

    def test_closed_market_is_not_treated_as_open(self, tmp_path):
        broker = self._broker(tmp_path)
        results = observe_once(broker, [entry()], reader=OtherReader(state="paused"),
                               clock=now_utc, venue=OtherVenue())
        assert results[0]["status"] == "observed"
        assert broker.report()["prediction_count"] == 0  # no forecast on a closed market
