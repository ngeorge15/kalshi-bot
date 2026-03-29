"""Shared pytest fixtures for the kalshi-bot test suite."""

import os
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization


@pytest.fixture(scope="session")
def test_private_key_path(tmp_path_factory):
    """Generate a fresh 2048-bit RSA key and return the path to the PEM file.

    Scoped to session so we generate the key once and reuse it across all
    tests that need a valid RSA private key without hitting the live API.
    """
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )
    pem_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    key_dir = tmp_path_factory.mktemp("keys")
    key_file = key_dir / "test_private_key.pem"
    key_file.write_bytes(pem_bytes)
    return str(key_file)


@pytest.fixture(scope="function")
def mock_config_env(monkeypatch, test_private_key_path):
    """Monkeypatch environment variables required by Config/KalshiAuth.

    Sets KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY_PATH (pointing to the
    session-scoped test key), and KALSHI_ENV so tests don't need a real
    .env file present.
    """
    monkeypatch.setenv("KALSHI_API_KEY_ID", "test-key-id")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", test_private_key_path)
    monkeypatch.setenv("KALSHI_ENV", "demo")


@pytest.fixture
def skip_without_integration():
    """Skip the calling test unless KALSHI_INTEGRATION=true is set.

    Usage: include this fixture in any test that makes live network calls
    to the Kalshi demo API so the default test run stays fast and offline.
    """
    if os.environ.get("KALSHI_INTEGRATION", "").lower() != "true":
        pytest.skip("Set KALSHI_INTEGRATION=true to run")
