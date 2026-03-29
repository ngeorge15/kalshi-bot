"""Unit and integration test stubs for the Kalshi API client.

All unit tests use mocks (no network required).  Integration tests are
gated behind KALSHI_INTEGRATION=true and target the Kalshi demo API.

Run unit tests only (fast, CI-safe):
    pytest tests/test_kalshi_client.py -v -m "not integration"

Run integration tests (requires demo credentials + KALSHI_INTEGRATION=true):
    KALSHI_INTEGRATION=true pytest tests/test_kalshi_client.py -v -m integration
"""

import pytest


# ---------------------------------------------------------------------------
# Unit tests — no marker, no network
# ---------------------------------------------------------------------------


def test_auth_signature():
    """KalshiAuth produces valid base64 RSA-PSS signature for timestamp+method+path."""
    pass


def test_auth_headers():
    """KalshiAuth sets KALSHI-ACCESS-KEY, KALSHI-ACCESS-SIGNATURE, KALSHI-ACCESS-TIMESTAMP headers."""
    pass


def test_auth_strips_query_string():
    """KalshiAuth signs path without query string (path only, not ?foo=bar)."""
    pass


def test_base_url_demo():
    """Config uses https://demo-api.kalshi.co/trade-api/v2 when KALSHI_ENV=demo."""
    pass


def test_base_url_prod():
    """Config uses https://trading-api.kalshi.com/trade-api/v2 when KALSHI_ENV=production."""
    pass


def test_market_discovery_pagination():
    """get_markets_by_series follows cursor pagination across multiple pages."""
    pass


def test_get_event():
    """get_event returns event dict with markets list."""
    pass


def test_orderbook_parse():
    """get_orderbook parses _dollars format into cents tuples."""
    pass


def test_place_order_payload():
    """place_limit_order sends correct JSON payload with ticker, side, price, count."""
    pass


def test_cancel_order():
    """cancel_order calls DELETE /portfolio/orders/{order_id}."""
    pass


def test_get_balance():
    """get_balance returns balance_cents and portfolio_value_cents."""
    pass


def test_get_positions():
    """get_positions returns list of position dicts."""
    pass


def test_get_fills():
    """get_fills returns list of fill dicts with _dollars fields."""
    pass


def test_retry_config():
    """Session has Retry adapter mounted with total=3, backoff_factor=1."""
    pass


def test_missing_env_var():
    """Config raises error when KALSHI_API_KEY_ID is missing."""
    pass


def test_dollars_to_cents():
    """dollars_to_cents('0.6500') returns 65."""
    pass


# ---------------------------------------------------------------------------
# Integration tests — require KALSHI_INTEGRATION=true and demo credentials
# ---------------------------------------------------------------------------


class TestIntegration:
    """Integration tests that hit the live Kalshi demo API.

    All tests in this class are marked with the ``integration`` marker and
    will be skipped unless KALSHI_INTEGRATION=true is set in the environment.
    Use the ``skip_without_integration`` fixture to enforce this at runtime.
    """

    pytestmark = pytest.mark.integration

    def test_integration_auth(self, skip_without_integration):
        """Authenticate to demo API and get exchange status."""
        pass

    def test_integration_discover_nba_games(self, skip_without_integration):
        """Discover NBA game markets via series_ticker=KXNBAGAME."""
        pass

    def test_integration_orderbook(self, skip_without_integration):
        """Pull orderbook for a real market ticker."""
        pass

    def test_integration_place_cancel(self, skip_without_integration):
        """Place a limit order at extreme price and cancel it."""
        pass

    def test_integration_get_balance(self, skip_without_integration):
        """Get demo account balance."""
        pass

    def test_integration_get_positions(self, skip_without_integration):
        """Get demo account positions."""
        pass
