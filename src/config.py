"""Centralized configuration module for the Kalshi trading bot.

Loads credentials from .env and trading parameters from trading_config.json.
Exposes a module-level singleton ``config`` for use throughout the application.

Usage:
    from src.config import config, Config

    # Use the singleton (will be None if env vars are missing at import time)
    print(config.base_url)

    # Or instantiate directly for testing (monkeypatch env vars first)
    cfg = Config()
"""

import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=False)

logger = logging.getLogger(__name__)

# Root of the project (parent of src/)
_PROJECT_ROOT = Path(__file__).parent.parent


class Config:
    """Configuration singleton for the Kalshi trading bot.

    Reads credentials from environment variables and trading parameters
    from ``trading_config.json`` at the project root.

    Args:
        env_override: If provided, overrides the KALSHI_ENV environment
            variable.  Useful in tests: ``Config(env_override="demo")``.

    Raises:
        KeyError: If ``KALSHI_API_KEY_ID`` or ``KALSHI_PRIVATE_KEY_PATH``
            are not set in the environment.
        FileNotFoundError: If ``trading_config.json`` is not found.
    """

    DEMO_BASE_URL = "https://demo-api.kalshi.co/trade-api/v2"
    PROD_BASE_URL = "https://trading-api.kalshi.com/trade-api/v2"

    def __init__(self, env_override: str | None = None) -> None:
        # Required credentials — raises KeyError if missing (per R1.10)
        self.api_key_id: str = os.environ["KALSHI_API_KEY_ID"]
        self.private_key_path: str = os.path.expanduser(os.environ["KALSHI_PRIVATE_KEY_PATH"])

        # Environment: demo or production
        self.env: str = env_override if env_override is not None else os.getenv("KALSHI_ENV", "demo")
        if self.env not in {"demo", "production"}:
            raise ValueError("KALSHI_ENV must be demo or production")
        self.base_url: str = (
            self.DEMO_BASE_URL if self.env == "demo" else self.PROD_BASE_URL
        )

        # Load trading parameters from JSON config
        config_path = _PROJECT_ROOT / "trading_config.json"
        with open(config_path) as f:
            self._trading: dict = json.load(f)

        self.risk: dict = self._trading["risk"]
        self.markets: dict = self._trading["markets"]
        self.cache: dict = self._trading.get("cache", {})

        logger.debug(
            "Config loaded: env=%s, base_url=%s, key_id=%s",
            self.env,
            self.base_url,
            self.api_key_id,
        )


def _load_config() -> "Config | None":
    """Attempt to create the module-level Config singleton.

    Returns None if required environment variables are not set (e.g., during
    testing before monkeypatching).  Downstream code that uses the singleton
    should guard: ``assert config is not None``.
    """
    try:
        return Config()
    except KeyError as exc:
        logger.debug("Config singleton not created: missing env var %s", exc)
        return None


config: Config | None = _load_config()
