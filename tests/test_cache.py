import os
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from redis import Redis
from redis.exceptions import ConnectionError, TimeoutError

from app import cache
from app.models import User
from app.routers import centres, tests
from app.schemas.catalogue import CentreResponse
from app.schemas.common import Page


def test_cache_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cache, "get_settings", lambda: SimpleNamespace(redis_url=None))
    client = Mock()
    monkeypatch.setattr(cache, "_client", client)
    assert cache.key("centres", 20, 0, None) is None
    assert cache.get_json(None, Page[CentreResponse]) is None
    cache.set_json(None, Page[CentreResponse](items=[], total=0, limit=20, offset=0))
    cache.invalidate()
    client.assert_not_called()


@pytest.mark.parametrize("error", [ConnectionError, TimeoutError])
def test_cache_errors_fall_through(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error: type[Exception],
) -> None:
    redis = Mock()
    redis.get.side_effect = error("unavailable")
    redis.set.side_effect = error("unavailable")
    redis.incr.side_effect = error("unavailable")
    monkeypatch.setattr(cache, "_redis", lambda: redis)
    cache._warn_once.cache_clear()
    assert cache.key("centres", 20, 0, None) is None
    assert cache.get_json("catalogue:0:test", Page[CentreResponse]) is None
    cache.set_json(
        "catalogue:0:test", Page[CentreResponse](items=[], total=0, limit=20, offset=0)
    )
    cache.invalidate()
    assert sum("Catalogue cache unavailable" in r.message for r in caplog.records) == 1


def test_cache_down_requests_use_database(
    client: TestClient,
    make_user: Callable[..., User],
    auth_headers: Callable[[User], dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = Mock()
    redis.get.side_effect = ConnectionError("unavailable")
    redis.incr.side_effect = ConnectionError("unavailable")
    monkeypatch.setattr(cache, "_redis", lambda: redis)
    headers = auth_headers(make_user(is_admin=True))
    created = client.post(
        "/centres", json={"name": "Clinic", "location": "City"}, headers=headers
    )
    assert created.status_code == 201
    assert client.get("/centres").json()["total"] == 1
    assert client.get(f"/centres/{created.json()['id']}").status_code == 200
    assert (
        client.post("/tests", json={"name": "CBC"}, headers=headers).status_code == 201
    )
    assert client.get("/tests").json()["total"] == 1


def test_invalid_cached_response_falls_through(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = Mock()
    redis.get.return_value = '{"items": "invalid"}'
    monkeypatch.setattr(cache, "_redis", lambda: redis)
    assert cache.get_json("catalogue:0:test", Page[CentreResponse]) is None


@pytest.fixture
def catalogue_redis(monkeypatch: pytest.MonkeyPatch) -> Iterator[Redis]:
    url = os.environ.get("TEST_REDIS_URL")
    if not url:
        pytest.skip("Set TEST_REDIS_URL for Redis integration tests")
    monkeypatch.setattr(cache, "get_settings", lambda: SimpleNamespace(redis_url=url))
    redis = cache._client(url)
    redis.ping()
    cache.invalidate()
    yield redis
    cache.invalidate()


def test_cache_hits_and_write_invalidation(
    client: TestClient,
    make_user: Callable[..., User],
    auth_headers: Callable[[User], dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
    catalogue_redis: Redis,
) -> None:
    headers = auth_headers(make_user(is_admin=True))
    assert client.get("/centres").json()["total"] == 0
    assert client.get("/tests").json()["total"] == 0
    version = catalogue_redis.get(cache.VERSION_KEY)
    centre = client.post(
        "/centres", json={"name": "Clinic", "location": "City"}, headers=headers
    ).json()
    assert catalogue_redis.get(cache.VERSION_KEY) != version
    assert client.get("/centres").json()["total"] == 1
    version = catalogue_redis.get(cache.VERSION_KEY)
    test = client.post(
        "/tests", json={"name": "CBC", "description": "Blood count"}, headers=headers
    ).json()
    assert catalogue_redis.get(cache.VERSION_KEY) != version
    assert client.get("/tests").json()["total"] == 1
    paths = ["/centres", f"/centres/{centre['id']}", "/tests"]

    def warm() -> list[dict]:
        responses = [client.get(path) for path in paths]
        assert all(response.status_code == 200 for response in responses)
        return [response.json() for response in responses]

    before = warm()
    with monkeypatch.context() as patch:
        for module, name in [
            (centres, "list_centres"),
            (centres, "get_centre_detail"),
            (tests, "list_tests"),
        ]:
            patch.setattr(module, name, Mock(side_effect=AssertionError("Cache miss")))
        assert warm() == before

    mutations = [
        ("PATCH", paths[1], {"location": "New City"}, 200),
        ("PATCH", f"/tests/{test['id']}", {"description": "Updated"}, 200),
        ("PUT", f"{paths[1]}/tests/{test['id']}", {"price": "12.34"}, 200),
        ("DELETE", f"{paths[1]}/tests/{test['id']}", None, 204),
        ("DELETE", f"/tests/{test['id']}", None, 204),
        ("DELETE", paths[1], None, 204),
    ]
    for method, path, payload, status in mutations:
        version = catalogue_redis.get(cache.VERSION_KEY)
        assert (
            client.request(method, path, json=payload, headers=headers).status_code
            == status
        )
        assert catalogue_redis.get(cache.VERSION_KEY) != version
        if path == paths[1] and method == "DELETE":
            assert client.get(paths[1]).status_code == 404
            assert client.get("/centres").json()["total"] == 0
        else:
            after = warm()
            assert after != before
            before = after


def test_cache_keys_include_filters_and_pagination(
    client: TestClient,
    make_user: Callable[..., User],
    auth_headers: Callable[[User], dict[str, str]],
    catalogue_redis: Redis,
) -> None:
    headers = auth_headers(make_user(is_admin=True))
    for name in ["Alpha", "Beta"]:
        assert (
            client.post(
                "/centres", json={"name": name, "location": "City"}, headers=headers
            ).status_code
            == 201
        )
        assert (
            client.post("/tests", json={"name": name}, headers=headers).status_code
            == 201
        )
    for _ in range(2):
        assert client.get("/centres", params={"q": "Alpha"}).json()["total"] == 1
        assert client.get("/centres", params={"q": "missing"}).json()["total"] == 0
        for path in ["/centres", "/tests"]:
            assert client.get(path).json()["total"] == 2
            page = client.get(path, params={"limit": 1, "offset": 1}).json()
            assert [item["name"] for item in page["items"]] == ["Beta"]
            assert client.get(path, params={"offset": 2}).json()["items"] == []
    cache_key = cast(str, cache.key("centres", 20, 0, "Alpha"))
    assert 0 < catalogue_redis.ttl(cache_key) <= cache.TTL


def test_invalidation_during_read_does_not_repopulate_current_version(
    catalogue_redis: Redis,
) -> None:
    old_key = cache.key("centres", 20, 0, None)
    cache.invalidate()
    cache.set_json(old_key, Page[CentreResponse](items=[], total=0, limit=20, offset=0))
    new_key = cache.key("centres", 20, 0, None)
    assert new_key != old_key
    assert cache.get_json(new_key, Page[CentreResponse]) is None
