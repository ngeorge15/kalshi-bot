"""Regression coverage for audit findings; all exchange calls are mocked."""
from unittest.mock import Mock

import pytest

from src.kalshi.client import KalshiClient


@pytest.fixture
def client(test_private_key_path):
    return KalshiClient("https://example.test", "test", test_private_key_path)


@pytest.mark.parametrize("env", ["", "prod", "Demo", "typo"])
def test_invalid_environment_rejected(mock_config_env, env):
    from src.config import Config
    with pytest.raises(ValueError, match="KALSHI_ENV"):
        Config(env_override=env)


@pytest.mark.parametrize("method,key", [("get_orders", "orders"), ("get_positions", "market_positions")])
def test_complete_portfolio_pagination(client, method, key):
    params_seen = []
    pages = iter([{key: [{"id": 1}], "cursor": "next"}, {key: [{"id": 2}]}])
    def get(path, params):
        params_seen.append(dict(params))
        return next(pages)
    client._get = get
    assert getattr(client, method)(status="resting") == [{"id": 1}, {"id": 2}]
    assert "cursor" not in params_seen[0]
    assert params_seen[1]["cursor"] == "next"
    assert len(params_seen[1]) == 2  # original filter preserved


def test_repeated_cursor_fails_instead_of_looping(client):
    client._get = Mock(return_value={"orders": [], "cursor": "same"})
    with pytest.raises(ValueError, match="Repeated pagination"):
        client.get_orders()
    assert client._get.call_count == 2


@pytest.mark.parametrize("helper,method,kwargs", [("_get", "get", {}), ("_post", "post", {"json": {}}), ("_delete", "delete", {})])
def test_http_timeouts(client, helper, method, kwargs):
    client._session = Mock()
    getattr(client, helper)("/test", **kwargs)
    assert getattr(client._session, method).call_args.kwargs["timeout"] == (5, 30)


def test_post_not_automatically_replayed(client):
    retry = client._session.get_adapter("https://").max_retries
    assert not retry.is_retry("POST", 503)
    assert retry.is_retry("GET", 503)


@pytest.mark.parametrize("overrides", [{"side": "invalid"}, {"action": "invalid"}, {"count": 0}, {"count": True}, {"count": 1.5}, {"price_cents": 100}, {"price_cents": 0}])
def test_invalid_orders_never_sent(client, overrides):
    client._post = Mock()
    args = dict(ticker="TEST", side="yes", action="buy", price_cents=50, count=1)
    args.update(overrides)
    with pytest.raises(ValueError):
        client.place_limit_order(**args)
    client._post.assert_not_called()


def test_orders_have_unique_client_ids(client):
    client._post = Mock(return_value={"order": {"order_id": "id"}})
    for _ in range(2):
        client.place_limit_order("TEST", "yes", "buy", 50, 1)
    ids = [call.kwargs["json"]["client_order_id"] for call in client._post.call_args_list]
    assert all(ids) and ids[0] != ids[1]
