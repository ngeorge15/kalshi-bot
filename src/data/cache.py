"""File-based TTL cache for external API responses.

Provides simple get/set/stale-fallback operations backed by JSON files on disk.
Each cached item stores the value alongside a ``cached_at`` Unix timestamp so
TTL expiry can be evaluated at read time without touching file mtimes.

Directory layout::

    data/cache/{namespace}/{md5_of_params}.json

Usage::

    from src.data.cache import cache_get, cache_set, cache_get_stale

    # Store a response
    cache_set("nws_forecast", {"city": "NYC", "date": "2024-01-15"}, forecast_dict)

    # Retrieve (returns None if expired or missing)
    result = cache_get("nws_forecast", {"city": "NYC", "date": "2024-01-15"})

    # Retrieve stale (returns last known value even if TTL exceeded — useful as
    # fallback when the upstream API is unavailable)
    result = cache_get_stale("nws_forecast", {"city": "NYC", "date": "2024-01-15"})
"""

import hashlib
import json
import logging
import os
import tempfile
import time
from pathlib import Path

from src.config import config

logger = logging.getLogger(__name__)

# Default root directory for all cached files.
# Tests monkeypatch this attribute to redirect to a tmp_path.
CACHE_DIR = Path("data/cache")

# Fallback TTL map used when config is unavailable (e.g., missing env vars).
_DEFAULT_TTL: dict[str, int] = {
    "nws_forecast": 3600,
    "player_stats": 21600,
    "team_stats": 86400,
    "schedule": 86400,
    "historical": 604800,
}


def _get_default_ttl(namespace: str) -> int:
    """Return the TTL (seconds) for *namespace* from config, or a hardcoded fallback.

    Args:
        namespace: Cache namespace key (e.g. ``"nws_forecast"``).

    Returns:
        TTL in seconds.  Falls back to 3 600 s (1 h) if namespace is unknown.
    """
    if config is not None:
        ttl_map: dict = config.cache.get("ttl_seconds", {})
        if namespace in ttl_map:
            return int(ttl_map[namespace])
    # Fallback to hardcoded defaults
    return _DEFAULT_TTL.get(namespace, 3600)


def _cache_key(namespace: str, params: dict) -> Path:
    """Return the ``Path`` for the cache file corresponding to *namespace*+*params*.

    The filename is the MD5 hex digest of the JSON-serialised *params* (keys
    sorted for determinism).  Nested namespaces (``"a/b/c"``) are turned into
    nested subdirectories.

    Args:
        namespace: Logical grouping for cached items (e.g. ``"player_stats"``).
        params: Arbitrary dict of query parameters identifying the item.

    Returns:
        Absolute ``Path`` object pointing at the ``.json`` cache file.
    """
    params_hash = hashlib.md5(
        json.dumps(params, sort_keys=True).encode()
    ).hexdigest()
    return CACHE_DIR / namespace / f"{params_hash}.json"


def cache_get(
    namespace: str,
    params: dict,
    ttl_seconds: int | None = None,
) -> dict | None:
    """Return the cached value for *namespace*+*params* if it is still fresh.

    Args:
        namespace: Cache namespace.
        params: Query parameters that identify the cached item.
        ttl_seconds: Override the default TTL for this namespace.  If ``None``
            the default TTL from ``_get_default_ttl`` is used.

    Returns:
        The cached ``dict`` value, or ``None`` if the entry does not exist or
        has expired.
    """
    if ttl_seconds is None:
        ttl_seconds = _get_default_ttl(namespace)

    key = _cache_key(namespace, params)
    if not key.exists():
        logger.debug("Cache miss (no file): %s", key)
        return None

    try:
        data = json.loads(key.read_text())
        cached_at: float = data["cached_at"]
        age = time.time() - cached_at
        if age > ttl_seconds:
            logger.debug("Cache miss (expired, age=%.0fs ttl=%ss): %s", age, ttl_seconds, key)
            return None
        logger.debug("Cache hit (age=%.0fs): %s", age, key)
        return data["value"]
    except (KeyError, json.JSONDecodeError, OSError) as exc:
        logger.warning("Cache read error for %s: %s", key, exc)
        return None


def cache_get_stale(namespace: str, params: dict) -> dict | None:
    """Return the cached value for *namespace*+*params* regardless of TTL.

    This is used as a graceful fallback when an upstream API call fails —
    returning stale data is better than returning nothing.

    Args:
        namespace: Cache namespace.
        params: Query parameters that identify the cached item.

    Returns:
        The cached ``dict`` value (possibly expired), or ``None`` if no file
        exists at all.
    """
    key = _cache_key(namespace, params)
    if not key.exists():
        logger.debug("Stale cache miss (no file): %s", key)
        return None

    try:
        data = json.loads(key.read_text())
        logger.debug("Stale cache hit: %s", key)
        return data["value"]
    except (KeyError, json.JSONDecodeError, OSError) as exc:
        logger.warning("Stale cache read error for %s: %s", key, exc)
        return None


def cache_set(namespace: str, params: dict, value: dict) -> None:
    """Write *value* to the cache under *namespace*+*params*.

    Creates the necessary directory tree if it does not already exist.  The
    written JSON object has the form::

        {"cached_at": <unix_timestamp_float>, "value": <value>}

    Args:
        namespace: Cache namespace.
        params: Query parameters that identify the cached item.
        value: The data to cache.  Must be JSON-serialisable.
    """
    key = _cache_key(namespace, params)
    key.parent.mkdir(parents=True, exist_ok=True)
    payload = {"cached_at": time.time(), "value": value}
    # Written via a temp file in the same directory and then renamed, because
    # os.replace is atomic on POSIX: a reader either sees the whole previous
    # file or the whole new one, never a half-written mixture. Writing in
    # place is not safe here -- two backtests scanning overlapping date
    # ranges hit the same key concurrently and interleave their writes, which
    # shows up later as `json.JSONDecodeError: Extra data` on read, and an
    # interrupted run leaves a truncated file behind forever. Both were
    # observed before this was made atomic.
    tmp_fd, tmp_name = tempfile.mkstemp(dir=str(key.parent), suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w") as handle:
            json.dump(payload, handle)
        os.replace(tmp_name, key)
    except BaseException:
        # Leave no partial file behind on failure, including on KeyboardInterrupt.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    logger.debug("Cache set: %s", key)
