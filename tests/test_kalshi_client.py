"""Unit and integration test stubs for the Kalshi API client.

All unit tests use mocks (no network required).  Integration tests are
gated behind KALSHI_INTEGRATION=true and target the Kalshi demo API.

Run unit tests only (fast, CI-safe):
    pytest tests/test_kalshi_client.py -v -m "not integration"

Run integration tests (requires demo credentials + KALSHI_INTEGRATION=true):
    KALSHI_INTEGRATION=true pytest tests/test_kalshi_client.py -v -m integration
"""

import base64
import os
from unittest.mock import patch

import pytest
import requests


# ---------------------------------------------------------------------------
# Unit tests — no marker, no network
# ---------------------------------------------------------------------------


def test_auth_signature(test_private_key_path):
    """KalshiAuth produces valid base64 RSA-PSS signature for timestamp+method+path."""
    from src.kalshi.auth import KalshiAuth

    auth = KalshiAuth(key_id="test-key", private_key_path=test_private_key_path)
    prepared = requests.Request("GET", "https://example.com/trade-api/v2/markets").prepare()
    auth(prepared)
    sig = prepared.headers["KALSHI-ACCESS-SIGNATURE"]
    assert sig, "Signature must not be empty"
    # Must be valid base64
    base64.b64decode(sig)


def test_auth_headers(test_private_key_path):
    """KalshiAuth sets KALSHI-ACCESS-KEY, KALSHI-ACCESS-SIGNATURE, KALSHI-ACCESS-TIMESTAMP headers."""
    from src.kalshi.auth import KalshiAuth

    auth = KalshiAuth(key_id="test-key", private_key_path=test_private_key_path)
    prepared = requests.Request("GET", "https://example.com/trade-api/v2/markets").prepare()
    auth(prepared)
    assert "KALSHI-ACCESS-KEY" in prepared.headers
    assert "KALSHI-ACCESS-SIGNATURE" in prepared.headers
    assert "KALSHI-ACCESS-TIMESTAMP" in prepared.headers


def test_auth_strips_query_string(test_private_key_path):
    """KalshiAuth signs path without query string (path only, not ?foo=bar)."""
    from src.kalshi.auth import KalshiAuth
    import src.kalshi.auth as auth_module

    auth = KalshiAuth(key_id="test-key", private_key_path=test_private_key_path)
    prepared = requests.Request(
        "GET", "https://example.com/trade-api/v2/markets?status=open&limit=100"
    ).prepare()

    signed_messages = []
    original_b64encode = base64.b64encode

    # Intercept base64.b64encode at module level to capture the raw bytes passed to sign.
    # We do this by patching the encode function in the auth module's namespace and
    # recording what was signed via a separate sign intercept on the auth module.
    # Simplest reliable approach: patch `time.time` and independently reconstruct msg.
    with patch.object(auth_module.time, "time", return_value=1700000000.0):
        auth(prepared)

    # The signing string would be: "1700000000000GET/trade-api/v2/markets"
    # Verify the header was set (auth ran without error)
    assert "KALSHI-ACCESS-SIGNATURE" in prepared.headers

    # Also verify the signature is verifiable with the expected message (no query string)
    from cryptography.hazmat.primitives.asymmetric import padding as _padding
    from cryptography.hazmat.primitives import hashes as _hashes
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    import time as _time

    with open(test_private_key_path, "rb") as f:
        private_key = load_pem_private_key(f.read(), password=None)
    public_key = private_key.public_key()

    expected_msg_no_qs = b"1700000000000GET/trade-api/v2/markets"
    wrong_msg_with_qs = b"1700000000000GET/trade-api/v2/markets?status=open&limit=100"

    sig_bytes = base64.b64decode(prepared.headers["KALSHI-ACCESS-SIGNATURE"])

    # Verify signature is valid for path WITHOUT query string
    try:
        public_key.verify(
            sig_bytes,
            expected_msg_no_qs,
            _padding.PSS(
                mgf=_padding.MGF1(_hashes.SHA256()),
                salt_length=_padding.PSS.MAX_LENGTH,
            ),
            _hashes.SHA256(),
        )
        valid_without_qs = True
    except Exception:
        valid_without_qs = False

    assert valid_without_qs, "Signature must be valid for path without query string"


def test_base_url_demo(monkeypatch, test_private_key_path):
    """Config uses https://demo-api.kalshi.co/trade-api/v2 when KALSHI_ENV=demo."""
    monkeypatch.setenv("KALSHI_API_KEY_ID", "test-key-id")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", test_private_key_path)
    from src.config import Config
    cfg = Config(env_override="demo")
    assert cfg.base_url == "https://demo-api.kalshi.co/trade-api/v2"


def test_base_url_prod(monkeypatch, test_private_key_path):
    """Config uses https://trading-api.kalshi.com/trade-api/v2 when KALSHI_ENV=production."""
    monkeypatch.setenv("KALSHI_API_KEY_ID", "test-key-id")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", test_private_key_path)
    from src.config import Config
    cfg = Config(env_override="production")
    assert cfg.base_url == "https://trading-api.kalshi.com/trade-api/v2"


def test_missing_env_var(monkeypatch, test_private_key_path):
    """Config raises error when KALSHI_API_KEY_ID is missing."""
    # Remove the key from the environment. load_dotenv() only runs at module
    # import time (not at Config() instantiation time), so we can safely
    # instantiate Config() and it will hit the missing key via os.environ[].
    monkeypatch.delenv("KALSHI_API_KEY_ID", raising=False)
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", test_private_key_path)
    monkeypatch.setenv("KALSHI_ENV", "demo")
    from src.config import Config
    with pytest.raises((KeyError, ValueError)):
        Config()


def test_dollars_to_cents():
    """dollars_to_cents('0.6500') returns 65."""
    from src.kalshi.auth import dollars_to_cents

    assert dollars_to_cents("0.6500") == 65
    assert dollars_to_cents("0.0100") == 1
    assert dollars_to_cents("1.0000") == 100


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
