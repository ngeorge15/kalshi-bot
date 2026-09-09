"""One-pass, read-only weather observation using an explicitly reviewed watchlist."""
from datetime import datetime, timezone
import math
from urllib.parse import urlparse
from uuid import uuid4

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from src.paper.broker import PaperBroker, label, utc
from src.paper.venue import DEFAULT_VENUE, KALSHI_MARKET_BASE, asks_from_orderbook  # noqa: F401


# Retained for callers and tests that referenced it before venues were split out.
MARKET_BASE = KALSHI_MARKET_BASE
NWS_BASE = "https://api.weather.gov"
NWS_HOST = "api.weather.gov"


class PublicData:
    """GET-only public data reader. No auth, credentials, or order methods."""

    def __init__(self, venue=None) -> None:
        # Allowed hosts come from the venue plus the weather source, so adding a
        # venue does not mean editing a hardcoded allowlist here.
        self.venue = venue or DEFAULT_VENUE
        self.allowed_hosts = frozenset(self.venue.hosts) | {NWS_HOST}
        self.session = requests.Session()
        self.session.trust_env = False  # never load .netrc credentials
        self.session.headers.update({"User-Agent": "kalshi-bot-paper-research/1.0", "Accept": "application/json"})
        self.session.mount("https://", HTTPAdapter(max_retries=Retry(
            total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"], raise_on_status=False)))

    def get(self, url: str) -> dict:
        """Fetch a public JSON resource on the two approved data hosts."""
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.netloc not in self.allowed_hosts:
            raise ValueError("Unexpected public-data URL")
        response = self.session.get(url, timeout=(5, 30), allow_redirects=False)
        response.raise_for_status()
        if response.status_code != 200:
            raise ValueError("Public data must return HTTP 200 without redirects")
        return response.json()

    def close(self) -> None:
        """Close pooled connections."""
        self.session.close()


def observe_once(broker: PaperBroker, watchlist: list[dict], reader=None, clock=None,
                 venue=None) -> list[dict]:
    """Observe reviewed temperature markets, paper fill, forecast once, and settle.

    Caller-supplied eligibility is not inferred from public market visibility.
    Access errors are reported; the collector does not attempt another host/auth.
    Run this command repeatedly to collect subsequent quotes and outcomes.
    """
    if broker.config.run_kind != "forward":
        raise ValueError("observe requires a separate forward paper experiment")
    if not isinstance(watchlist, list):
        raise ValueError("Watchlist must be a JSON list")
    venue = venue or DEFAULT_VENUE
    owns_reader = reader is None
    reader = reader or PublicData(venue=venue)
    clock = clock or (lambda: datetime.now(timezone.utc))
    results = []
    try:
        for entry in watchlist:
            ticker = entry.get("ticker") if isinstance(entry, dict) else None
            try:
                ticker = label(ticker, "ticker")
                now = clock()
                eligibility = entry["eligibility"]
                ineligible = (eligibility.get("available") is not True
                              or not utc(eligibility["checked_at"]) <= now < utc(eligibility["expires_at"]))
                # Research mode reads and records regardless of tradeability: a
                # model probability, a public market probability and a reported
                # outcome are all obtainable without permission to trade. Trading
                # modes still refuse, because visible does not mean tradeable.
                if ineligible and not broker.config.research_only:
                    if broker.market_state(ticker):
                        broker.process({"event_id": str(uuid4()), "type": "unavailable", "at": now.isoformat(),
                                        "ticker": ticker, "reason": "eligibility_missing_or_expired"})
                    results.append({"ticker": ticker, "status": "skipped", "reason": "eligibility_missing_or_expired"})
                    continue
                if not broker.config.research_only:
                    label(eligibility.get("source"), "eligibility source")
                label(entry.get("rules_source"), "reviewed contract rules source")
                if entry["market_type"] != "temperature" or entry["market_type"] not in broker.config.allowed_market_types:
                    raise ValueError("Automatic observation currently supports enabled temperature markets only")
                spec = entry["weather_spec"]
                market = venue.parse_market(reader.get(venue.market_url(ticker)))
                if market["ticker"] != ticker:
                    raise ValueError("Market response ticker mismatch")
                state = broker.market_state(ticker)
                if market["status"] == "settled":
                    if state and state["result"] is None:
                        settled = broker.process({"event_id": str(uuid4()), "type": "settlement", "at": clock().isoformat(),
                                                  "ticker": ticker, "result": market["result"], "source_market": market})
                        results.append({"ticker": ticker, "status": "settled", **settled})
                    else:
                        results.append({"ticker": ticker, "status": "skipped", "reason": "already_settled_or_unobserved"})
                    continue
                if state and state["result"]:
                    raise ValueError("Public market state conflicts with a recorded settlement")
                observed = clock()
                book = reader.get(venue.orderbook_url(ticker))
                received = clock()
                if received >= utc(eligibility["expires_at"]) and not broker.config.research_only:
                    if state:
                        broker.process({"event_id": str(uuid4()), "type": "unavailable", "at": received.isoformat(),
                                        "ticker": ticker, "reason": "eligibility_expired_during_fetch"})
                    results.append({"ticker": ticker, "status": "skipped", "reason": "eligibility_expired_during_fetch"})
                    continue
                result = broker.process({"event_id": str(uuid4()), "type": "quote", "at": received.isoformat(),
                    "observed_at": observed.isoformat(), "ticker": ticker, "market_type": entry["market_type"],
                    "event_key": entry["event_key"], "close_at": market["close_time"],
                    "available": market["is_open"], "weather_spec": spec,
                    "eligibility": eligibility, "rules_source": entry["rules_source"],
                    "source_market": market["raw"], "source_orderbook": book,
                    **venue.parse_orderbook(book)})
                result = {"ticker": ticker, "tradeable": not ineligible, **result}
                results.append(result)
                if not market["is_open"] or received >= utc(market["close_time"]):
                    continue
                if broker.has_prediction(ticker, "weather_hourly_normal_baseline", "1"):
                    continue
                lat, lon = entry["latitude"], entry["longitude"]
                if (isinstance(lat, bool) or isinstance(lon, bool) or not math.isfinite(lat)
                    or not math.isfinite(lon) or not -90 <= lat <= 90 or not -180 <= lon <= 180):
                    raise ValueError("Invalid forecast coordinates")
                point = reader.get(f"{NWS_BASE}/points/{lat},{lon}")
                hourly_url = point["properties"]["forecastHourly"]
                properties = reader.get(hourly_url)["properties"]
                issued_at = properties.get("updateTime") or properties["generatedAt"]
                snapshot = {"source": hourly_url, "issued_at": issued_at, "periods": properties["periods"],
                            "raw_properties": properties}
                predicted = broker.process({"event_id": str(uuid4()), "type": "weather_forecast", "at": clock().isoformat(),
                                            "ticker": ticker, "snapshot": snapshot})
                result["forecast"] = predicted
            except (ValueError, KeyError, TypeError, requests.RequestException) as exc:
                results.append({"ticker": ticker, "status": "error", "reason": str(exc)})
    finally:
        if owns_reader:
            reader.close()
    return results
