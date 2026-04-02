"""Temperature bracket probability model using NWS ensemble + bias correction.

Workflow:
    1. fit_bias_correction() — learn additive offsets from NOAA historical data
    2. predict_brackets(station, forecast_date, brackets) — return per-bracket probs
    3. predict_threshold(station, threshold_f, forecast_date) — return P(high > X)

All predictions use Gaussian fit centered on bias-corrected NWS point forecast (D-11).
Bias correction: per station × month (D-10, D-12). 12-month granularity.
DST handling: NWS hourly forecast filtered to local standard time window (R5.7).

Usage::

    from src.models.weather_temp import weather_temp_model
    weather_temp_model.fit_bias_correction()
    probs = weather_temp_model.predict_brackets("KNYC", "2026-04-01")
"""

import logging
import re
import statistics
from typing import Optional

from scipy import stats

from src.data.weather.station_map import STATIONS
from src.db.database import Database
from src.models.exceptions import InsufficientDataError

logger = logging.getLogger(__name__)

# Standard timezone offsets (LST — no DST adjustment per R5.7)
_TZ_OFFSETS = {
    "America/New_York": -5,
    "America/Chicago": -6,
}


class WeatherTempModel:
    """Temperature bracket probability model using NWS ensemble + bias correction.

    Combines NWS point forecast with learned additive bias corrections and a
    Gaussian fit to produce per-bracket probabilities for Kalshi temperature
    markets.

    Args:
        db_path: Path to SQLite database with NOAA historical data.
    """

    def __init__(self, db_path: str = "data/kalshi_bot.db") -> None:
        self.db = Database(db_path=db_path)
        # bias_offsets[station_code][month_int] = (mean_offset_f, sigma_f)
        self.bias_offsets: dict[str, dict[int, tuple[float, float]]] = {}
        self._is_fitted = False

    def fit_bias_correction(self, min_observations: int = 30) -> dict:
        """Learn additive bias corrections from NOAA historical data.

        Computes per station × month sigma (forecast uncertainty proxy)
        from NOAA tmax variance.  Offset defaults to 0.0 until NWS forecast
        history accumulates for comparison.

        Args:
            min_observations: Minimum NOAA obs per station to use real sigma.

        Returns:
            Dict mapping station_code -> {month: (offset_f, sigma_f)}.
        """
        result: dict[str, dict[int, tuple[float, float]]] = {}

        for station_code in STATIONS:
            rows = self.db.fetchall(
                "SELECT date, tmax_f FROM noaa_daily_weather "
                "WHERE station_code = ? AND tmax_f IS NOT NULL ORDER BY date ASC",
                (station_code,),
            )

            if len(rows) < min_observations:
                logger.warning(
                    "Station %s has %d NOAA records (need %d); using default sigma=5.0°F",
                    station_code, len(rows), min_observations,
                )
                result[station_code] = {m: (0.0, 5.0) for m in range(1, 13)}
                continue

            # Group by month
            by_month: dict[int, list[float]] = {m: [] for m in range(1, 13)}
            for row in rows:
                month = int(row["date"][5:7])
                by_month[month].append(row["tmax_f"])

            station_offsets: dict[int, tuple[float, float]] = {}
            for month, temps in by_month.items():
                if len(temps) < 5:
                    station_offsets[month] = (0.0, 5.0)
                else:
                    sigma = statistics.stdev(temps)
                    station_offsets[month] = (0.0, sigma)

            result[station_code] = station_offsets
            avg_sigma = sum(v[1] for v in station_offsets.values()) / 12
            logger.info(
                "Fitted bias correction for %s: avg sigma=%.1f°F",
                station_code, avg_sigma,
            )

        self.bias_offsets = result
        self._is_fitted = True
        return result

    def _get_nws_point_forecast(self, station: str, forecast_date: str) -> float:
        """Extract point forecast high temperature from NWS hourly data.

        Uses local standard time (LST) 6am–6pm window per R5.7.

        Args:
            station: Station code (e.g. ``'KNYC'``).
            forecast_date: ISO date string ``'YYYY-MM-DD'``.

        Returns:
            Forecast high temperature in °F.
        """
        from src.data.weather.nws import get_nws_hourly

        station_info = STATIONS[station]
        hourly = get_nws_hourly(station_info["lat"], station_info["lon"])

        # R5.7: LST window for daily high
        offset_hours = _TZ_OFFSETS.get(
            station_info.get("timezone", "America/New_York"), -5,
        )
        window_start_utc = 6 - offset_hours   # 6am LST in UTC
        window_end_utc = 18 - offset_hours     # 6pm LST in UTC

        temps_in_window: list[float] = []
        for period in hourly:
            start_time = period.get("startTime", "")
            if not start_time.startswith(forecast_date):
                continue
            try:
                hour_utc = int(start_time[11:13])
            except (ValueError, IndexError):
                continue
            if window_start_utc <= hour_utc < window_end_utc:
                temp = period.get("temperature")
                if temp is not None:
                    temps_in_window.append(float(temp))

        if temps_in_window:
            return max(temps_in_window)

        # Fallback: any temperature on that date
        for period in hourly:
            if period.get("startTime", "").startswith(forecast_date):
                t = period.get("temperature")
                if t is not None:
                    return float(t)

        return 55.0  # last-resort default

    def predict_brackets(
        self,
        station: str,
        forecast_date: str,
        brackets: Optional[list[tuple]] = None,
    ) -> dict[str, float]:
        """Return probability distribution over temperature brackets.

        Args:
            station: Station code (e.g. ``'KNYC'``).
            forecast_date: ISO date string ``'YYYY-MM-DD'``.
            brackets: List of ``(low, high)`` tuples.  ``None`` = open-ended.
                Default 6-bracket structure.

        Returns:
            Dict mapping bracket label to probability.  Values sum ≈ 1.0.
        """
        if not self._is_fitted:
            self.fit_bias_correction()

        if brackets is None:
            brackets = [
                (None, 45), (45, 50), (50, 55),
                (55, 60), (60, 65), (65, None),
            ]

        nws_forecast = self._get_nws_point_forecast(station, forecast_date)
        month = int(forecast_date[5:7])
        offset, sigma = self.bias_offsets.get(station, {}).get(
            month, (0.0, 5.0),
        )
        mu = nws_forecast + offset  # D-10 bias correction

        result: dict[str, float] = {}
        for lo, hi in brackets:
            if lo is None and hi is not None:
                label = f"<{hi}"
                prob = float(stats.norm.cdf(hi, loc=mu, scale=sigma))
            elif lo is not None and hi is None:
                label = f">={lo}"
                prob = float(1.0 - stats.norm.cdf(lo, loc=mu, scale=sigma))
            elif lo is not None and hi is not None:
                label = f"{lo}-{hi - 1}"
                prob = float(
                    stats.norm.cdf(hi, loc=mu, scale=sigma)
                    - stats.norm.cdf(lo, loc=mu, scale=sigma)
                )
            else:
                label = "all"
                prob = 1.0
            result[label] = max(0.0, min(1.0, prob))

        # Normalize to sum = 1.0
        total = sum(result.values())
        if total > 0:
            result = {k: v / total for k, v in result.items()}

        return result

    def predict_threshold(
        self,
        station: str,
        threshold_f: float,
        forecast_date: str,
    ) -> float:
        """Return P(daily_high > threshold_f) for single-threshold markets.

        Args:
            station: Station code.
            threshold_f: Temperature threshold in °F.
            forecast_date: ISO date string.

        Returns:
            Float in ``[0.0, 1.0]``.
        """
        if not self._is_fitted:
            self.fit_bias_correction()

        nws_forecast = self._get_nws_point_forecast(station, forecast_date)
        month = int(forecast_date[5:7])
        offset, sigma = self.bias_offsets.get(station, {}).get(
            month, (0.0, 5.0),
        )
        mu = nws_forecast + offset
        prob = float(1.0 - stats.norm.cdf(threshold_f, loc=mu, scale=sigma))
        return max(0.0, min(1.0, prob))


# Module-level singleton per D-14
weather_temp_model = WeatherTempModel()
