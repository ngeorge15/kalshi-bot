"""Unit tests for src/data/cache.py

Tests cover: write/read/TTL expiry/stale fallback/directory creation/key collisions.
All tests use monkeypatched CACHE_DIR so no real filesystem is touched outside tmp_path.
"""

import json
import time
import pytest

from src.data import cache as cache_module
from src.data.cache import cache_get, cache_set, cache_get_stale


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

NAMESPACE = "test_ns"
PARAMS = {"league": "NBA", "season": "2024"}
VALUE = {"games": [{"id": 1, "home": "LAL", "away": "GSW"}]}


@pytest.fixture(autouse=True)
def isolate_cache_dir(tmp_path, monkeypatch):
    """Redirect every test to use a fresh tmp directory as CACHE_DIR."""
    monkeypatch.setattr(cache_module, "CACHE_DIR", tmp_path / "cache")


# ---------------------------------------------------------------------------
# 1. cache_set writes JSON file with "cached_at" and "value"
# ---------------------------------------------------------------------------

def test_cache_set_writes_json_file(tmp_path):
    cache_set(NAMESPACE, PARAMS, VALUE)
    key = cache_module._cache_key(NAMESPACE, PARAMS)
    assert key.exists(), "cache file should be created by cache_set"
    data = json.loads(key.read_text())
    assert "cached_at" in data
    assert "value" in data
    assert data["value"] == VALUE


# ---------------------------------------------------------------------------
# 2. cache_get returns value when within TTL
# ---------------------------------------------------------------------------

def test_cache_get_returns_value_within_ttl():
    cache_set(NAMESPACE, PARAMS, VALUE)
    result = cache_get(NAMESPACE, PARAMS, ttl_seconds=3600)
    assert result == VALUE


# ---------------------------------------------------------------------------
# 3. cache_get returns None when TTL expired
# ---------------------------------------------------------------------------

def test_cache_get_returns_none_after_ttl_expired(monkeypatch):
    cache_set(NAMESPACE, PARAMS, VALUE)
    # Advance time beyond TTL
    original_time = time.time()
    monkeypatch.setattr(cache_module.time, "time", lambda: original_time + 7200)
    result = cache_get(NAMESPACE, PARAMS, ttl_seconds=3600)
    assert result is None


# ---------------------------------------------------------------------------
# 4. cache_get_stale returns expired value regardless of TTL
# ---------------------------------------------------------------------------

def test_cache_get_stale_returns_expired_value(monkeypatch):
    cache_set(NAMESPACE, PARAMS, VALUE)
    # Advance time way beyond any TTL
    original_time = time.time()
    monkeypatch.setattr(cache_module.time, "time", lambda: original_time + 9999999)
    result = cache_get_stale(NAMESPACE, PARAMS)
    assert result == VALUE


# ---------------------------------------------------------------------------
# 5. cache_get_stale returns None when no cached file exists
# ---------------------------------------------------------------------------

def test_cache_get_stale_returns_none_when_no_file():
    result = cache_get_stale(NAMESPACE, {"nonexistent": True})
    assert result is None


# ---------------------------------------------------------------------------
# 6. cache_set creates nested directories automatically
# ---------------------------------------------------------------------------

def test_cache_set_creates_nested_directories(tmp_path):
    nested_ns = "a/b/c"
    cache_set(nested_ns, PARAMS, VALUE)
    key = cache_module._cache_key(nested_ns, PARAMS)
    assert key.exists(), "nested namespace directories should be created automatically"


# ---------------------------------------------------------------------------
# 7. Cache key generation is deterministic
# ---------------------------------------------------------------------------

def test_cache_key_is_deterministic():
    key1 = cache_module._cache_key(NAMESPACE, PARAMS)
    key2 = cache_module._cache_key(NAMESPACE, PARAMS)
    assert key1 == key2


# ---------------------------------------------------------------------------
# 8. Different params produce different cache keys (no collision)
# ---------------------------------------------------------------------------

def test_different_params_produce_different_keys():
    key1 = cache_module._cache_key(NAMESPACE, {"team": "LAL"})
    key2 = cache_module._cache_key(NAMESPACE, {"team": "GSW"})
    assert key1 != key2
