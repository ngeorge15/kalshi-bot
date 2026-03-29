"""RSA-PSS request signing for the Kalshi v2 REST API.

Implements ``KalshiAuth`` as a ``requests.auth.AuthBase`` subclass so it
composes cleanly into a ``requests.Session``.  Per design decision D-03,
the private key is loaded once at initialization time and cached in memory.

Usage:
    from src.kalshi.auth import KalshiAuth, dollars_to_cents

    auth = KalshiAuth(key_id=config.api_key_id,
                      private_key_path=config.private_key_path)
    session = requests.Session()
    session.auth = auth
    resp = session.get("https://demo-api.kalshi.co/trade-api/v2/markets")
"""

import base64
import logging
import time
from decimal import Decimal

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from requests.auth import AuthBase

logger = logging.getLogger(__name__)


class KalshiAuth(AuthBase):
    """Signs every outgoing request with the Kalshi RSA-PSS scheme.

    The signing string is: ``{timestamp_ms}{METHOD}{path}`` where:
    - ``timestamp_ms`` is Unix epoch in milliseconds (integer string)
    - ``METHOD`` is the HTTP verb in uppercase (e.g., ``GET``)
    - ``path`` is the URL path **without** query string

    Three headers are added to the request:
    - ``KALSHI-ACCESS-KEY`` — the API key ID
    - ``KALSHI-ACCESS-TIMESTAMP`` — the timestamp in milliseconds (string)
    - ``KALSHI-ACCESS-SIGNATURE`` — base64-encoded RSA-PSS signature

    Args:
        key_id: Kalshi API key identifier.
        private_key_path: Path to the RSA private key file in PEM format.
    """

    def __init__(self, key_id: str, private_key_path: str) -> None:
        self.key_id = key_id
        # D-03: Load private key once, cache in memory
        with open(private_key_path, "rb") as f:
            self.private_key = serialization.load_pem_private_key(
                f.read(), password=None
            )
        logger.debug("KalshiAuth initialized for key_id=%s", key_id)

    def __call__(self, r):
        """Sign the prepared request and attach auth headers.

        Args:
            r: A ``requests.PreparedRequest`` object.

        Returns:
            The same ``PreparedRequest`` with auth headers attached.
        """
        # D-04: Unix milliseconds as integer string
        timestamp_ms = str(int(time.time() * 1000))

        # CRITICAL: strip query string before signing
        path = r.path_url.split("?")[0]

        # Signing string: timestamp + METHOD + path (no query string)
        msg = f"{timestamp_ms}{r.method}{path}".encode("utf-8")

        # RSA-PSS with SHA-256
        sig = self.private_key.sign(
            msg,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH,
            ),
            hashes.SHA256(),
        )

        r.headers["KALSHI-ACCESS-KEY"] = self.key_id
        r.headers["KALSHI-ACCESS-SIGNATURE"] = base64.b64encode(sig).decode("utf-8")
        r.headers["KALSHI-ACCESS-TIMESTAMP"] = timestamp_ms

        return r


def dollars_to_cents(dollars_str: str) -> int:
    """Convert a fixed-point dollar string to integer cents.

    Uses ``Decimal`` for exact arithmetic — avoids floating-point rounding
    errors when parsing Kalshi ``_dollars`` response fields.

    Args:
        dollars_str: A fixed-point string such as ``"0.6500"`` or ``"1.0000"``.

    Returns:
        Integer cents (e.g., ``"0.6500"`` → 65).

    Examples:
        >>> dollars_to_cents("0.6500")
        65
        >>> dollars_to_cents("0.0100")
        1
        >>> dollars_to_cents("1.0000")
        100
    """
    return int(Decimal(dollars_str) * 100)
