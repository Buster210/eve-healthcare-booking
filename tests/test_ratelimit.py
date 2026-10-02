from collections.abc import Iterator

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from app import ratelimit
from app.errors import register_error_handlers


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    ratelimit.reset_rate_limits()
    monkeypatch.setattr(ratelimit, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(ratelimit, "monotonic", lambda: 100.0)
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/first", dependencies=[Depends(ratelimit.rate_limit("first", 2, 10))])
    @app.get("/second", dependencies=[Depends(ratelimit.rate_limit("second", 2, 10))])
    def endpoint() -> dict[str, bool]:
        return {"ok": True}

    with TestClient(app) as test_client:
        yield test_client
    ratelimit.reset_rate_limits()


def test_allows_up_to_limit(client: TestClient) -> None:
    assert client.get("/first").status_code == 200
    assert client.get("/first").status_code == 200


def test_rejects_requests_over_limit(client: TestClient) -> None:
    client.get("/first")
    client.get("/first")
    response = client.get("/first")
    assert response.status_code == 429
    assert response.json() == {
        "error": {
            "code": "RATE_LIMITED",
            "message": "Too many requests",
            "details": {"retry_after": 10},
        }
    }


def test_window_resets_at_expiry(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client.get("/first")
    client.get("/first")
    monkeypatch.setattr(ratelimit, "monotonic", lambda: 109.2)
    response = client.get("/first")
    assert response.status_code == 429
    assert response.json()["error"]["details"]["retry_after"] == 1
    monkeypatch.setattr(ratelimit, "monotonic", lambda: 110.0)
    assert client.get("/first").status_code == 200
    assert client.get("/first").status_code == 200
    assert client.get("/first").status_code == 429


def test_different_keys_are_independent(client: TestClient) -> None:
    client.get("/first")
    client.get("/first")
    assert client.get("/first").status_code == 429
    assert client.get("/second").status_code == 200


def test_different_client_ips_are_independent(client: TestClient) -> None:
    client.get("/first")
    client.get("/first")
    assert client.get("/first").status_code == 429
    with TestClient(client.app, client=("192.0.2.1", 50000)) as other_client:
        assert other_client.get("/first").status_code == 200


def test_reset_clears_limits(client: TestClient) -> None:
    client.get("/first")
    client.get("/first")
    assert client.get("/first").status_code == 429
    ratelimit.reset_rate_limits()
    assert client.get("/first").status_code == 200


def test_disabled_limiter_allows_requests(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ratelimit, "RATE_LIMIT_ENABLED", False)
    for _ in range(3):
        assert client.get("/first").status_code == 200


@pytest.mark.parametrize(("limit", "window_seconds"), [(0, 10), (2, 0)])
def test_rejects_nonpositive_limits(limit: int, window_seconds: int) -> None:
    with pytest.raises(ValueError, match="must be positive"):
        ratelimit.rate_limit("invalid", limit, window_seconds)


def test_evicts_expired_windows_for_other_clients_and_keys(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client.get("/first")
    with TestClient(client.app, client=("192.0.2.1", 50000)) as other_client:
        other_client.get("/second")
    monkeypatch.setattr(ratelimit, "monotonic", lambda: 105.0)
    client.get("/second")
    monkeypatch.setattr(ratelimit, "monotonic", lambda: 110.0)
    client.get("/second")
    assert ratelimit._windows == {("second", "testclient"): (115.0, 2)}


def test_prunes_only_once_per_window(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client.get("/first")
    ratelimit._windows[("expired", "192.0.2.1")] = (101.0, 1)
    monkeypatch.setattr(ratelimit, "monotonic", lambda: 105.0)
    client.get("/second")
    assert ("expired", "192.0.2.1") in ratelimit._windows
    assert ratelimit._next_prune == 110.0
    monkeypatch.setattr(ratelimit, "monotonic", lambda: 110.0)
    client.get("/second")
    assert ("expired", "192.0.2.1") not in ratelimit._windows
    assert ratelimit._next_prune == 120.0


def test_short_window_resets_before_scheduled_prune(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client.get("/first")
    check = ratelimit.rate_limit("short", 1, 2)
    request = Request({"type": "http", "client": ("192.0.2.1", 50000)})
    check(request)
    monkeypatch.setattr(ratelimit, "monotonic", lambda: 102.0)
    check(request)
    assert ratelimit._windows[("short", "192.0.2.1")] == (104.0, 1)
    assert ratelimit._next_prune == 110.0


def test_identity_cap_evicts_oldest_window(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ratelimit, "MAX_IDENTITIES", 3)
    check = ratelimit.rate_limit("cap", 2, 10)
    requests = [
        Request({"type": "http", "client": (f"192.0.2.{index}", 50000)})
        for index in range(4)
    ]
    for request in requests[:3]:
        check(request)
    check(requests[0])
    assert len(ratelimit._windows) == 3
    check(requests[3])
    assert list(ratelimit._windows) == [
        ("cap", "192.0.2.1"),
        ("cap", "192.0.2.2"),
        ("cap", "192.0.2.3"),
    ]
