"""Kalshi REST API v2 client covering all CRUD operations.

Implements ``KalshiClient`` with market discovery, orderbook retrieval, order
placement, order management, and portfolio state methods.  All HTTP calls go
through a ``requests.Session`` with an ``HTTPAdapter`` retry policy so
transient failures are retried automatically (R1.9).

Usage:
    from src.kalshi.client import KalshiClient
    from src.config import config

    client = KalshiClient.from_config(config)
    markets = client.get_markets_by_series("KXNBAGAME")
"""

import logging
from typing import Optional
from uuid import uuid4

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from src.kalshi.auth import KalshiAuth, dollars_to_cents

logger = logging.getLogger(__name__)


class KalshiClient:
    """Full Kalshi REST API v2 client.

    Covers market discovery (R1.3), event discovery (R1.4), orderbook (R1.5),
    order placement (R1.6), order management (R1.7), portfolio state (R1.8),
    and resilient HTTP with retry (R1.9).

    Args:
        base_url: Kalshi API base URL (demo or production).
        key_id: Kalshi API key identifier.
        private_key_path: Path to RSA private key in PEM format.
    """

    def __init__(self, base_url: str, key_id: str, private_key_path: str) -> None:
        self.timeout = (5, 30)  # connect and read timeout, in seconds
        self.base_url = base_url
        self._auth = KalshiAuth(key_id, private_key_path)
        self._session = self._build_session()
        logger.debug("KalshiClient initialized: base_url=%s, key_id=%s", base_url, key_id)

    def _build_session(self) -> requests.Session:
        """Build a requests.Session with auth and retry adapter mounted.

        Returns:
            Configured ``requests.Session`` with ``KalshiAuth`` and a
            ``Retry`` adapter on the HTTPS prefix.
        """
        session = requests.Session()
        session.auth = self._auth

        retry = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "DELETE"],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)

        session.headers.update(
            {
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        )
        return session

    # ------------------------------------------------------------------
    # Private HTTP helpers
    # ------------------------------------------------------------------

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        """Execute a GET request.

        Args:
            path: API path (appended to base_url).
            params: Optional query parameters.

        Returns:
            Parsed JSON response as a dict.

        Raises:
            requests.HTTPError: If the response status is 4xx or 5xx.
        """
        resp = self._session.get(f"{self.base_url}{path}", params=params, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, json: Optional[dict] = None) -> dict:
        """Execute a POST request.

        Args:
            path: API path (appended to base_url).
            json: Optional request body as a dict (JSON-serialized).

        Returns:
            Parsed JSON response as a dict.

        Raises:
            requests.HTTPError: If the response status is 4xx or 5xx.
        """
        resp = self._session.post(f"{self.base_url}{path}", json=json, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def _delete(self, path: str) -> dict:
        """Execute a DELETE request.

        Args:
            path: API path (appended to base_url).

        Returns:
            Parsed JSON response as a dict.

        Raises:
            requests.HTTPError: If the response status is 4xx or 5xx.
        """
        resp = self._session.delete(f"{self.base_url}{path}", timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def _get_all_pages(self, path: str, key: str, params: dict) -> list[dict]:
        """Read complete portfolio state; reject repeated pagination cursors."""
        params = dict(params)
        items: list[dict] = []
        seen: set[str] = set()
        while True:
            response = self._get(path, params=params)
            items.extend(response.get(key, []))
            cursor = response.get("cursor")
            if not cursor:
                return items
            if cursor in seen:
                raise ValueError(f"Repeated pagination cursor for {path}")
            seen.add(cursor)
            params["cursor"] = cursor

    # ------------------------------------------------------------------
    # Market Discovery (R1.3)
    # ------------------------------------------------------------------

    def get_markets_by_series(
        self,
        series_ticker: str,
        status: str = "open",
        limit: int = 200,
    ) -> list[dict]:
        """Discover all markets belonging to a series, following cursor pagination.

        Args:
            series_ticker: Kalshi series ticker (e.g., ``"KXNBAGAME"``).
            status: Market status filter — ``"open"``, ``"closed"``, or ``"settled"``.
            limit: Max results per page (API max is typically 200).

        Returns:
            Combined list of market dicts from all pages.
        """
        markets: list[dict] = []
        params: dict = {
            "series_ticker": series_ticker,
            "status": status,
            "limit": limit,
        }
        while True:
            resp = self._get("/markets", params=params)
            markets.extend(resp.get("markets", []))
            cursor = resp.get("cursor", "")
            if not cursor:
                break
            params["cursor"] = cursor
            logger.debug(
                "get_markets_by_series: fetched %d markets so far, cursor=%s",
                len(markets),
                cursor,
            )
        logger.info(
            "get_markets_by_series: %s → %d markets total", series_ticker, len(markets)
        )
        return markets

    def get_market(self, ticker: str) -> dict:
        """Retrieve a single market by ticker.

        Args:
            ticker: Market ticker string.

        Returns:
            Market dict from the API response.
        """
        return self._get(f"/markets/{ticker}")["market"]

    # ------------------------------------------------------------------
    # Event Discovery (R1.4)
    # ------------------------------------------------------------------

    def get_event(self, event_ticker: str) -> dict:
        """Retrieve an event (series of related markets) by its ticker.

        Args:
            event_ticker: Event ticker string (e.g., ``"EVT-123"``).

        Returns:
            Event dict including the ``markets`` list.
        """
        return self._get(f"/events/{event_ticker}")["event"]

    # ------------------------------------------------------------------
    # Orderbook (R1.5)
    # ------------------------------------------------------------------

    def get_orderbook(self, ticker: str) -> dict:
        """Retrieve the orderbook for a market and parse _dollars fields to cents.

        Kalshi's ``orderbook_fp`` response uses ``yes_dollars`` and
        ``no_dollars`` fixed-point string arrays.  This method converts them
        to integer cent tuples via ``dollars_to_cents``.

        Args:
            ticker: Market ticker string.

        Returns:
            Dict with keys:
            - ``yes_bids``: list of ``(price_cents, quantity_float)`` tuples
            - ``no_bids``: list of ``(price_cents, quantity_float)`` tuples
            - ``raw``: original API response dict
        """
        resp = self._get(f"/markets/{ticker}/orderbook")
        ob = resp.get("orderbook_fp", {})
        yes_bids = [
            (dollars_to_cents(p), float(q)) for p, q in ob.get("yes_dollars", [])
        ]
        no_bids = [
            (dollars_to_cents(p), float(q)) for p, q in ob.get("no_dollars", [])
        ]
        return {"yes_bids": yes_bids, "no_bids": no_bids, "raw": resp}

    # ------------------------------------------------------------------
    # Order Placement (R1.6)
    # ------------------------------------------------------------------

    def place_limit_order(
        self,
        ticker: str,
        side: str,
        action: str,
        price_cents: int,
        count: int,
        post_only: bool = True,
    ) -> dict:
        """Place a limit (maker) order.

        Per Kalshi's API, ``yes_price`` is the number of cents for a YES
        contract.  For a NO order, ``yes_price`` must be set to
        ``100 - price_cents`` because Kalshi normalizes prices to the YES side.

        Args:
            ticker: Market ticker string.
            side: ``"yes"`` or ``"no"``.
            action: ``"buy"`` or ``"sell"``.
            price_cents: Price in cents (1-99) for the specified side.
            count: Number of contracts.
            post_only: If True, order is rejected if it would fill immediately
                (maker-only, avoids taker fees).

        Returns:
            Order dict from the API response.
        """
        if side not in {"yes", "no"} or action not in {"buy", "sell"}:
            raise ValueError("Invalid order side or action")
        if type(price_cents) is not int or not 1 <= price_cents <= 99:
            raise ValueError("price_cents must be an integer between 1 and 99")
        if type(count) is not int or count <= 0:
            raise ValueError("count must be a positive integer")
        yes_price = price_cents if side == "yes" else (100 - price_cents)
        payload = {
            "ticker": ticker,
            "client_order_id": str(uuid4()),
            "side": side,
            "action": action,
            "type": "limit",
            "yes_price": yes_price,
            "count": count,
            "post_only": post_only,
            "time_in_force": "good_till_canceled",
        }
        logger.info(
            "place_limit_order: ticker=%s side=%s action=%s price_cents=%d count=%d",
            ticker,
            side,
            action,
            price_cents,
            count,
        )
        return self._post("/portfolio/orders", json=payload)["order"]

    # ------------------------------------------------------------------
    # Order Management (R1.7)
    # ------------------------------------------------------------------

    def cancel_order(self, order_id: str) -> dict:
        """Cancel an open order by its ID.

        Args:
            order_id: The order identifier string.

        Returns:
            Response dict (includes ``reduced_by`` field).
        """
        logger.info("cancel_order: order_id=%s", order_id)
        return self._delete(f"/portfolio/orders/{order_id}")

    def get_orders(self, status: Optional[str] = None) -> list[dict]:
        """List orders, optionally filtered by status.

        Args:
            status: Optional status filter (e.g., ``"open"``, ``"filled"``).

        Returns:
            List of order dicts.
        """
        params: dict = {}
        if status:
            params["status"] = status
        return self._get_all_pages("/portfolio/orders", "orders", params)

    def get_fills(
        self,
        ticker: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict]:
        """Retrieve fill history.

        Args:
            ticker: Optional market ticker to filter fills.
            limit: Maximum number of fills to return.

        Returns:
            List of fill dicts (use ``_dollars`` fields for prices).
        """
        params: dict = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        return self._get("/portfolio/fills", params=params).get("fills", [])

    # ------------------------------------------------------------------
    # Portfolio State (R1.8)
    # ------------------------------------------------------------------

    def get_balance(self) -> dict:
        """Retrieve current account balance.

        Returns:
            Dict with:
            - ``balance_cents``: Available cash balance in cents.
            - ``portfolio_value_cents``: Total portfolio value in cents.
        """
        resp = self._get("/portfolio/balance")
        return {
            "balance_cents": resp["balance"],
            "portfolio_value_cents": resp.get("portfolio_value", 0),
        }

    def get_positions(self, status: Optional[str] = None) -> list[dict]:
        """Retrieve current market positions.

        Args:
            status: Optional settlement status filter (e.g., ``"unsettled"``).

        Returns:
            List of market position dicts.
        """
        params: dict = {}
        if status:
            params["settlement_status"] = status
        return self._get_all_pages("/portfolio/positions", "market_positions", params)

    def get_settlements(self, limit: int = 100) -> list[dict]:
        """Retrieve settlement history.

        Args:
            limit: Maximum number of settlements to return.

        Returns:
            List of settlement dicts.
        """
        return self._get(
            "/portfolio/settlements", params={"limit": limit}
        ).get("settlements", [])

    # ------------------------------------------------------------------
    # Exchange Status (utility)
    # ------------------------------------------------------------------

    def get_exchange_status(self) -> dict:
        """Retrieve the current exchange status (open/closed/maintenance).

        Returns:
            Exchange status dict.
        """
        return self._get("/exchange/status")

    # ------------------------------------------------------------------
    # Convenience constructors
    # ------------------------------------------------------------------

    @classmethod
    def from_config(cls, config) -> "KalshiClient":
        """Create a KalshiClient from a Config object.

        Args:
            config: A ``src.config.Config`` instance with ``base_url``,
                ``api_key_id``, and ``private_key_path`` attributes.

        Returns:
            Configured ``KalshiClient`` instance.
        """
        return cls(
            base_url=config.base_url,
            key_id=config.api_key_id,
            private_key_path=config.private_key_path,
        )
