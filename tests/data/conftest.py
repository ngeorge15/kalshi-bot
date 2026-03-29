"""Shared pytest fixtures for the data pipeline tests."""

import os
import pytest


@pytest.fixture
def cache_dir(tmp_path):
    """Provide a temporary cache directory for cache tests."""
    return tmp_path / "cache"


@pytest.fixture
def skip_without_integration():
    """Skip unless KALSHI_INTEGRATION=true is set."""
    if os.environ.get("KALSHI_INTEGRATION", "").lower() != "true":
        pytest.skip("Set KALSHI_INTEGRATION=true to run")
