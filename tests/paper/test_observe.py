"""Read-only collector contracts and fail-closed watchlist behavior."""
from datetime import timedelta
from unittest.mock import Mock

import pytest
import requests

from src.paper.broker import PaperBroker
from src.paper.config import PaperConfig
from src.paper.observe import MARKET_BASE, PublicData, asks_from_orderbook, observe_once
from tests.paper.test_weather import NOW, SPEC, snapshot


def watch():
    return {"ticker": "TEMP", "market_type": "temperature", "event_key": "NYC-2026-09-05",
            "weather_spec": SPEC, "latitude": 40.0, "longitude": -74.0,
            "rules_source": "fixture-reviewed-contract",
            "eligibility": {"available": True, "checked_at": (NOW-timedelta(hours=1)).isoformat(),
                            "expires_at": (NOW+timedelta(days=3)).isoformat(), "source": "fixture-account-review"}}


class Reader:
    def __init__(self):
        self.calls = []
        self.settled = False

    def get(self, url):
        self.calls.append(url)
        if url.endswith("/orderbook"):
            return {"orderbook_fp": {"yes_dollars": [["0.0500", "20.00"]], "no_dollars": [["0.9000", "5.00"]]}}
        if "/markets/" in url:
            return {"market": {"ticker": "TEMP", "status": "settled" if self.settled else "active",
                               "close_time": "2026-09-06T12:00:00Z", "result": "yes"}}
        if "/points/" in url:
            return {"properties": {"forecastHourly": "https://api.weather.gov/gridpoints/TEST/1,1/forecast/hourly"}}
        return {"properties": {"updateTime": snapshot()["issued_at"], "periods": snapshot()["periods"]}}


def test_rounds_fixed_point_prices_adversely_and_floors_size():
    result = asks_from_orderbook({"orderbook_fp": {"yes_dollars": [["0.555", "3.9"]], "no_dollars": [["0.40", "2.99"]]}})
    assert result == {"yes_asks": [[60, 2]], "no_asks": [[45, 3]]}


def test_forward_observation_forecasts_once_then_fills_and_settles(tmp_path):
    current = [NOW]
    broker = PaperBroker(str(tmp_path / "f.db"), PaperConfig(run_kind="forward"), clock=lambda: current[0])
    reader = Reader()
    first = observe_once(broker, [watch()], reader, clock=lambda: current[0])
    assert first[0]["forecast"]["status"] == "resting"
    assert broker.report()["fills"] == []
    assert len(reader.calls) == 4
    current[0] += timedelta(seconds=1)
    second = observe_once(broker, [watch()], reader, clock=lambda: current[0])
    assert sum(f["quantity"] for f in second[0]["fills"]) == 5
    assert "forecast" not in second[0]
    assert len(reader.calls) == 6
    assert broker.report()["prediction_count"] == 1
    reader.settled = True
    current[0] = NOW+timedelta(days=2)
    last = observe_once(broker, [watch()], reader, clock=lambda: current[0])
    assert last[0]["status"] == "settled"
    assert broker.report()["realized_pnl_cents"] == 435


@pytest.mark.parametrize("change", [{"available": False}, {"expires_at": NOW.isoformat()}])
def test_unavailable_watchlist_never_calls_network(tmp_path, change):
    broker = PaperBroker(str(tmp_path / "f.db"), PaperConfig(run_kind="forward"), clock=lambda: NOW)
    entry = watch()
    entry["eligibility"].update(change)
    reader = Mock()
    result = observe_once(broker, [entry], reader, clock=lambda: NOW)
    assert result[0]["status"] == "skipped"
    reader.get.assert_not_called()


def test_restricted_type_never_calls_network(tmp_path):
    broker = PaperBroker(str(tmp_path / "f.db"), PaperConfig(run_kind="forward"), clock=lambda: NOW)
    reader = Mock()
    result = observe_once(broker, [{**watch(), "market_type": "games"}], reader, clock=lambda: NOW)
    assert result[0]["status"] == "error"
    reader.get.assert_not_called()


def test_access_failure_reported_without_fallback(tmp_path):
    broker = PaperBroker(str(tmp_path / "f.db"), PaperConfig(run_kind="forward"), clock=lambda: NOW)
    reader = Mock()
    reader.get.side_effect = requests.HTTPError("403 blocked")
    result = observe_once(broker, [watch()], reader, clock=lambda: NOW)
    assert result[0]["reason"] == "403 blocked"
    assert reader.get.call_count == 1
    assert broker.report()["orders"] == []


def test_eligibility_expiry_cancels_pending_paper_orders(tmp_path):
    current = [NOW]
    broker = PaperBroker(str(tmp_path / "f.db"), PaperConfig(run_kind="forward"), clock=lambda: current[0])
    observe_once(broker, [watch()], Reader(), clock=lambda: current[0])
    assert broker.report()["reserved_cents"] > 0
    current[0] += timedelta(seconds=1)
    entry = watch()
    entry["eligibility"]["expires_at"] = current[0].isoformat()
    reader = Mock()
    observe_once(broker, [entry], reader, clock=lambda: current[0])
    assert broker.report()["reserved_cents"] == 0
    reader.get.assert_not_called()


def test_public_reader_only_uses_get_and_does_not_load_auth():
    reader = PublicData()
    assert not reader.session.trust_env
    assert reader.session.auth is None
    session = Mock()
    session.get.return_value.status_code = 200
    reader.session.close()
    reader.session = session
    reader.get(MARKET_BASE + "/markets/TEST")
    session.get.assert_called_once_with(MARKET_BASE + "/markets/TEST", timeout=(5, 30), allow_redirects=False)
    session.post.assert_not_called()
    with pytest.raises(ValueError, match="Unexpected"):
        reader.get("https://unexpected.example/forecast")
    session.get.return_value.status_code = 302
    with pytest.raises(ValueError, match="without redirects"):
        reader.get(MARKET_BASE + "/markets/TEST")
