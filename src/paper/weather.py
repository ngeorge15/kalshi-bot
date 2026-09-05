"""Explicit, uncalibrated weather baseline for paper experiments.

Consumes a saved hourly forecast snapshot. Requires a complete local-standard
calendar day, with no default temperature and no hidden network/cache fallback.
Contract bounds must be supplied from reviewed settlement rules.
"""
from datetime import date, datetime, time, timedelta, timezone
import math
from statistics import NormalDist


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Weather timestamps must include a timezone")
    return parsed.astimezone(timezone.utc)


def predict_hourly_high(snapshot: dict, spec: dict, now: datetime,
                        sigma_f: float, max_age_seconds: int) -> dict:
    """Estimate P(lower <= high < upper) from a complete hourly forecast day.

    `spec` supplies date, fixed utc_offset_hours, and continuous lower/upper
    Fahrenheit bounds (None for an open tail). For an integer-rounded 70–72 F
    contract, reviewed bounds might be 69.5 and 72.5; this function never infers
    rounding or settlement rules from a ticker or title.
    """
    issued = _timestamp(snapshot["issued_at"])
    if not 0 <= (now - issued).total_seconds() <= max_age_seconds:
        raise ValueError("Weather forecast is stale or issued in the future")
    if not isinstance(snapshot.get("source"), str) or not snapshot["source"].strip():
        raise ValueError("Weather snapshot requires a source identifier")
    offset = spec["utc_offset_hours"]
    if type(offset) is not int or not -12 <= offset <= 14:
        raise ValueError("Invalid local standard UTC offset")
    target = date.fromisoformat(spec["date"])
    start = datetime.combine(target, time(), timezone(timedelta(hours=offset))).astimezone(timezone.utc)
    if now >= start:
        raise ValueError("Baseline requires a forecast made before the target day starts")
    if not math.isfinite(sigma_f) or sigma_f <= 0:
        raise ValueError("sigma_f must be finite and positive")
    lower, upper = spec.get("lower_bound_f"), spec.get("upper_bound_f")
    for bound in (lower, upper):
        if bound is not None and (isinstance(bound, bool) or not isinstance(bound, (int, float)) or not math.isfinite(bound)):
            raise ValueError("Temperature bounds must be finite numbers or null")
    if (lower is None and upper is None) or (lower is not None and upper is not None and lower >= upper):
        raise ValueError("Specify an ordered interval or one open tail")
    needed = {start + timedelta(hours=hour) for hour in range(24)}
    temperatures = {}
    for period in snapshot["periods"]:
        period_start = _timestamp(period["startTime"])
        period_end = _timestamp(period["endTime"])
        if period_start not in needed:
            continue
        if period_end-period_start != timedelta(hours=1) or period_start in temperatures:
            raise ValueError("Weather periods must be distinct one-hour intervals")
        temperature = period["temperature"]
        if isinstance(temperature, bool) or not isinstance(temperature, (int, float)) or not math.isfinite(temperature):
            raise ValueError("Missing or invalid hourly temperature")
        unit = period["temperatureUnit"]
        if unit not in {"F", "C"}:
            raise ValueError("Temperature unit must be F or C")
        temperatures[period_start] = temperature if unit == "F" else temperature*9/5+32
    if set(temperatures) != needed:
        raise ValueError(f"Incomplete forecast day: {len(temperatures)}/24 hours")
    mean = max(temperatures.values())
    normal = NormalDist(mu=mean, sigma=sigma_f)
    probability = (normal.cdf(upper) if upper is not None else 1.0) - (normal.cdf(lower) if lower is not None else 0.0)
    return {"yes_probability": probability, "mean_high_f": mean, "sigma_f": sigma_f,
            "source": snapshot["source"], "issued_at": issued.isoformat(), "hours": 24,
            "model_name": "weather_hourly_normal_baseline", "model_version": "1",
            "assumption": "Uncalibrated Normal uncertainty around maximum hourly forecast; not a fitted daily-high error distribution."}
