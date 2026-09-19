"""Unit tests for src/data/cache.py

Tests cover: write/read/TTL expiry/stale fallback/directory creation/key collisions.
All tests use monkeypatched CACHE_DIR so no real filesystem is touched outside tmp_path.
"""

import json
import threading
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


class TestCacheSetIsAtomic:
    """cache_set must never leave a reader a half-written file.

    Two backtests scanning overlapping date ranges hit the same cache key at
    the same time; an in-place write interleaves and the next read fails with
    `Extra data`. An interrupted run leaves a truncated file behind forever.
    Both were observed in this repo before the write was made atomic.
    """

    def test_no_temp_files_are_left_behind(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cache_module, "CACHE_DIR", tmp_path)
        cache_module.cache_set("ns", {"a": 1}, {"v": "x"})
        leftovers = list(tmp_path.rglob("*.tmp"))
        assert leftovers == []

    def test_a_failed_write_leaves_the_previous_value_intact(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cache_module, "CACHE_DIR", tmp_path)
        cache_module.cache_set("ns", {"a": 1}, {"v": "good"})

        class Unserialisable:
            pass

        with pytest.raises(TypeError):
            cache_module.cache_set("ns", {"a": 1}, {"v": Unserialisable()})

        assert cache_module.cache_get("ns", {"a": 1}, ttl_seconds=3600) == {"v": "good"}
        assert list(tmp_path.rglob("*.tmp")) == []

    def test_concurrent_writers_never_produce_an_unparseable_file(
        self, tmp_path, monkeypatch
    ):
        """The actual observed corruption: `json.JSONDecodeError: Extra data`.

        Path.write_text truncates, so overwriting with a shorter payload is
        safe on its own -- the corruption came from two processes writing the
        same key at once and interleaving. Each thread here writes a payload
        of a different length to the same key many times; with a non-atomic
        write a reader eventually sees one payload's head followed by
        another's tail, or an empty file mid-truncate. Reproduced against the
        old implementation before this fix: a reader hit JSONDecodeError
        within a few hundred reads. The backtest logs showed the same class
        of failure as `Extra data`.
        """
        monkeypatch.setattr(cache_module, "CACHE_DIR", tmp_path)
        params = {"a": 1}
        payloads = [{"v": str(n) * (1000 * n)} for n in range(1, 6)]
        errors: list[Exception] = []

        def writer(payload):
            try:
                for _ in range(40):
                    cache_module.cache_set("ns", params, payload)
            except Exception as exc:  # pragma: no cover - surfaced via errors
                errors.append(exc)

        def reader():
            try:
                for _ in range(200):
                    got = cache_module.cache_get("ns", params, ttl_seconds=3600)
                    if got is not None:
                        assert got in payloads
            except Exception as exc:  # pragma: no cover - surfaced via errors
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(p,)) for p in payloads]
        threads.append(threading.Thread(target=reader))
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        assert list(tmp_path.rglob("*.tmp")) == []
        final = cache_module.cache_get("ns", params, ttl_seconds=3600)
        assert final in payloads
