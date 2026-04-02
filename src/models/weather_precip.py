"""Precipitation threshold probability model.

Combines NWS probability of precipitation (PoP) with NOAA historical base
rates using lead-time weighting (R5.5).

Usage::

    from src.models.weather_precip import weather_precip_model
    prob = weather_precip_model.predict("KNYC", 0.1, "2026-04-01")
"""

import logging
import re
from typing import Optional

from src.data.weather.station_map import STATIONS
from src.db.database import Database

logger = logging.getLogger(__name__)


class WeatherPrecipModel:
    """Precipitation threshold model: P(precip > threshold_in).

    Combines NWS PoP with historical climatological base rates from NOAA.
    Lead-time weighting per R5.5: closer forecasts weighted more heavily.

    adjusted_prob = alpha * nws_pop + (1 - alpha) * historical_rate
    alpha = max(0.3, 1.0 - days_until / 7.0)

    Args:
        db_path: Path to SQLite database with NOAA historical data.
    """

    def __init__(self, db_path: str = "data/kalshi_bot.db") -> None:
        self.db = Database(db_path=db_path)
        self._historical_rates: dict[tuple[str, int, float], float] = {}

    def _get_historical_rate(
        self, station: str, month: int, threshold_in: float,
    ) -> float:
        """Compute historical P(precip > threshold_in) for station × month.

        Caches results so repeated calls for the same station/month/threshold
        don't re-query the DB.

        Args:
            station: Station code.
            month: Integer month (1–12).
            threshold_in: Precipitation threshold in inches.

        Returns:
            Historical exceedance rate (0.0–1.0), or 0.3 default.
        """
        cache_key = (station, month, threshold_in)
        if cache_key in self._historical_rates:
            return self._historical_rates[cache_key]

        rows = self.db.fetchall(
            "SELECT prcp_in FROM noaa_daily_weather "
            "WHERE station_code = ? "
            "AND CAST(strftime('%%m', date) AS INTEGER) = ? "
            "AND prcp_in IS NOT NULL",
            (station, month),
        )

        if not rows:
            self._historical_rates[cache_key] = 0.3
        else:
            precip_vals = [r["prcp_in"] for r in rows]
            rate = sum(1 for v in precip_vals if v > threshold_in) / len(precip_vals)
            self._historical_rates[cache_key] = rate

        return self._historical_rates[cache_key]

    def _get_nws_pop(self, station: str, forecast_date: str) -> float:
        """Get NWS probability of precipitation for forecast_date.

        Parses PoP from detailed forecast text or probabilityOfPrecipitation
        field.

        Args:
            station: Station code.
            forecast_date: ISO date string.

        Returns:
            PoP as float in [0.0, 1.0], or 0.3 fallback.
        """
        from src.data.weather.nws import get_nws_forecast

        station_info = STATIONS[station]
        try:
            periods = get_nws_forecast(station_info["lat"], station_info["lon"])
        except Exception as exc:
            logger.warning("NWS forecast fetch failed for %s: %s", station, exc)
            return 0.3

        for period in periods:
            start = period.get("startTime", "")
            if forecast_date in start:
                # Check structured field first
                pop = period.get("probabilityOfPrecipitation", {})
                if isinstance(pop, dict) and pop.get("value") is not None:
                    return float(pop["value"]) / 100.0

                # Parse from detailed forecast text
                detail = period.get("detailedForecast", "")
                match = re.search(
                    r"chance of precipitation is (\d+)%", detail, re.IGNORECASE,
                )
                if match:
                    return float(match.group(1)) / 100.0

        return 0.3  # fallback

    def predict(
        self,
        station: str,
        threshold_in: float,
        forecast_date: str,
        days_until: int = 1,
    ) -> float:
        """Return P(daily_precip > threshold_in) for a given station and date.

        Args:
            station: Station code (e.g. ``'KNYC'``).
            threshold_in: Precipitation threshold in inches (e.g. 0.1).
            forecast_date: ISO date string ``'YYYY-MM-DD'``.
            days_until: Days until the forecast date (for lead-time weighting).

        Returns:
            Float in ``[0.0, 1.0]``.
        """
        month = int(forecast_date[5:7])
        historical_rate = self._get_historical_rate(station, month, threshold_in)
        nws_pop = self._get_nws_pop(station, forecast_date)

        # R5.5: Lead-time weighting
        alpha = max(0.3, 1.0 - days_until / 7.0)
        combined = alpha * nws_pop + (1.0 - alpha) * historical_rate

        return max(0.0, min(1.0, combined))


# Module-level singleton per D-14
weather_precip_model = WeatherPrecipModel()
