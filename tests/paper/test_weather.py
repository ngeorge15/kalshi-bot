"""No fallback weather forecasts, explicit units, offsets, and contract bounds."""
from datetime import datetime, timedelta, timezone
from statistics import NormalDist

import pytest

from src.paper.broker import PaperBroker
from src.paper.config import PaperConfig
from src.paper.weather import predict_hourly_high


NOW = datetime(2026, 9, 4, 12, tzinfo=timezone.utc)
SPEC = {"date": "2026-09-05", "utc_offset_hours": -6,
        "lower_bound_f": 69.5, "upper_bound_f": 72.5}


def snapshot():
    # NWS-like strings carry daylight offsets; the target calendar window uses
    # a fixed standard offset. The first included hour is 01:00 daylight time.
    start = datetime(2026, 9, 5, 1, tzinfo=timezone(timedelta(hours=-5)))
    periods = [{"startTime": (start+timedelta(hours=h)).isoformat(),
                "endTime": (start+timedelta(hours=h+1)).isoformat(),
                "temperature": 65 if h < 23 else 71, "temperatureUnit": "F"}
               for h in range(24)]
    return {"source": "saved-nws-hourly-fixture", "issued_at": "2026-09-04T11:00:00Z", "periods": periods}


def predict(data=None, spec=None, **kwargs):
    return predict_hourly_high(data or snapshot(), spec or SPEC, kwargs.get("now", NOW), 5, 21600)


def test_full_local_standard_day_includes_last_hour():
    result = predict()
    assert result["mean_high_f"] == 71
    assert result["hours"] == 24
    expected = NormalDist(71, 5).cdf(72.5)-NormalDist(71, 5).cdf(69.5)
    assert result["yes_probability"] == pytest.approx(expected)


def test_celsius_conversion():
    data = snapshot()
    for period in data["periods"]:
        period.update(temperature=20, temperatureUnit="C")
    assert predict(data)["mean_high_f"] == 68


@pytest.mark.parametrize("change, message", [
    (lambda data: data["periods"].pop(), "Incomplete"),
    (lambda data: data["periods"].append(data["periods"][0]), "distinct"),
    (lambda data: data["periods"][0].update(temperature=None), "invalid"),
    (lambda data: data["periods"][0].update(temperatureUnit="K"), "unit"),
    (lambda data: data.update(issued_at="2026-09-03T12:00:00Z"), "stale"),
    (lambda data: data.update(issued_at="2026-09-04T12:01:00Z"), "future"),
])
def test_invalid_or_incomplete_snapshot_fails(change, message):
    data = snapshot()
    change(data)
    with pytest.raises(ValueError, match=message):
        predict(data)


def test_cannot_predict_day_after_it_has_started():
    with pytest.raises(ValueError, match="before the target day"):
        predict(spec={**SPEC, "date": "2026-09-04"})


def test_open_tails_and_complement():
    lower = predict(spec={**SPEC, "lower_bound_f": None})["yes_probability"]
    upper = predict(spec={**SPEC, "lower_bound_f": 72.5, "upper_bound_f": None})["yes_probability"]
    assert lower+upper == pytest.approx(1)


def test_weather_event_records_input_and_model_version(tmp_path):
    broker = PaperBroker(str(tmp_path / "weather.db"))
    broker.process({"event_id": "q", "type": "quote", "at": NOW.isoformat(), "observed_at": NOW.isoformat(),
                    "ticker": "T", "market_type": "temperature", "event_key": "NYC-2026-09-05",
                    "close_at": "2026-09-06T12:00:00Z", "available": True,
                    "yes_asks": [[10, 10]], "no_asks": [[95, 10]], "weather_spec": SPEC})
    event = {"event_id": "w", "type": "weather_forecast", "at": (NOW+timedelta(seconds=1)).isoformat(),
             "ticker": "T", "snapshot": snapshot()}
    result = broker.process(event)
    assert result["prediction"]["model_name"] == "weather_hourly_normal_baseline"
    assert result["status"] == "resting"
    assert broker.report()["prediction_count"] == 1
    assert PaperBroker(broker.db_path).process(event) == result


def test_forward_market_scope_is_weather_only():
    with pytest.raises(ValueError, match="weather only"):
        PaperConfig(run_kind="forward", allowed_market_types=("games",))
    assert PaperConfig(run_kind="replay", allowed_market_types=("games",)).allowed_market_types == ("games",)
