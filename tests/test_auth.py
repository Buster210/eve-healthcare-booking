from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import cast

import jwt
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import ratelimit
from app.config import get_settings
from app.models import User
from app.security import require_admin, verify_password


def test_signup_login_and_me(client: TestClient, db: Session) -> None:
    response = client.post(
        "/auth/signup",
        json={
            "email": "Person@Example.com",
            "password": "a-good-password",
            "full_name": "A Person",
            "is_admin": True,
        },
    )
    assert response.status_code == 201
    user = response.json()
    assert user["email"] == "person@example.com"
    assert user["full_name"] == "A Person"
    assert user["is_admin"] is False
    assert "password_hash" not in user
    stored = db.scalar(select(User).where(User.id == user["id"]))
    assert stored is not None
    assert stored.password_hash != "a-good-password"
    assert verify_password("a-good-password", stored.password_hash)

    response = client.post(
        "/auth/login",
        json={"email": "PERSON@example.com", "password": "a-good-password"},
    )
    assert response.status_code == 200
    token = response.json()
    assert token["token_type"] == "bearer"
    assert token["expires_in"] == get_settings().jwt_expire_minutes * 60
    response = client.get(
        "/auth/me", headers={"Authorization": f"Bearer {token['access_token']}"}
    )
    assert response.status_code == 200
    assert response.json() == user


def test_duplicate_email_is_case_insensitive(client: TestClient) -> None:
    payload = {
        "email": "person@example.com",
        "password": "a-good-password",
        "full_name": "A Person",
    }
    assert client.post("/auth/signup", json=payload).status_code == 201
    payload["email"] = "PERSON@example.com"
    response = client.post("/auth/signup", json=payload)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CONFLICT"


def test_bad_password_and_unknown_email_have_same_error(client: TestClient) -> None:
    assert (
        client.post(
            "/auth/signup",
            json={
                "email": "person@example.com",
                "password": "a-good-password",
                "full_name": "A Person",
            },
        ).status_code
        == 201
    )
    wrong = client.post(
        "/auth/login", json={"email": "person@example.com", "password": "wrong"}
    )
    unknown = client.post(
        "/auth/login", json={"email": "unknown@example.com", "password": "wrong"}
    )
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()


@pytest.mark.parametrize(
    "field,value",
    [
        ("email", "invalid"),
        ("password", "short"),
        ("password", "x" * 129),
        ("full_name", ""),
        ("full_name", "x" * 101),
    ],
)
def test_signup_validates_fields(client: TestClient, field: str, value: str) -> None:
    payload = {
        "email": "person@example.com",
        "password": "a-good-password",
        "full_name": "A Person",
    }
    payload[field] = value
    response = client.post("/auth/signup", json=payload)
    assert response.status_code == 422
    assert "error" in response.json()


def test_me_rejects_missing_and_garbage_tokens(client: TestClient) -> None:
    for headers in ({}, {"Authorization": "Bearer garbage"}):
        response = client.get("/auth/me", headers=headers)
        assert response.status_code == 401
        assert "error" in response.json()


def test_me_rejects_expired_token(
    client: TestClient, make_user: Callable[..., User]
) -> None:
    user = make_user()
    token = jwt.encode(
        {"sub": str(user.id), "exp": datetime.now(UTC) - timedelta(seconds=1)},
        get_settings().jwt_secret.get_secret_value(),
        algorithm="HS256",
    )
    response = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
    assert "error" in response.json()


def test_admin_guard_rejects_anonymous_and_regular_users(
    client: TestClient,
    make_user: Callable[..., User],
    auth_headers: Callable[[User], dict[str, str]],
) -> None:
    @cast(FastAPI, client.app).get("/_auth_test/admin")
    def admin_only(user: User = Depends(require_admin)) -> dict[str, int]:
        return {"id": user.id}

    assert client.get("/_auth_test/admin").status_code == 401
    user = make_user()
    assert (
        client.get("/_auth_test/admin", headers=auth_headers(user)).status_code == 403
    )
    admin = make_user(is_admin=True)
    response = client.get("/_auth_test/admin", headers=auth_headers(admin))
    assert response.status_code == 200
    assert response.json() == {"id": admin.id}


@pytest.mark.parametrize("endpoint,status_code", [("login", 401), ("signup", 201)])
def test_auth_throttles_after_ten_requests_and_resets_at_window_expiry(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
    status_code: int,
) -> None:
    monkeypatch.setattr(ratelimit, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(ratelimit, "monotonic", lambda: 100.0)
    payload = {
        "email": "person@example.com",
        "password": "a-good-password",
        "full_name": "A Person",
    }
    for index in range(10):
        payload["email"] = f"person-{index}@example.com"
        assert client.post(f"/auth/{endpoint}", json=payload).status_code == status_code

    payload["email"] = "person-10@example.com"
    response = client.post(f"/auth/{endpoint}", json=payload)
    assert response.status_code == 429
    assert response.json() == {
        "error": {
            "code": "RATE_LIMITED",
            "message": "Too many requests",
            "details": {"retry_after": 60},
        }
    }
    other_endpoint = "signup" if endpoint == "login" else "login"
    assert client.post(f"/auth/{other_endpoint}", json={}).status_code == 422
    assert client.get("/auth/me").status_code == 401

    monkeypatch.setattr(ratelimit, "monotonic", lambda: 160.0)
    assert client.post(f"/auth/{endpoint}", json=payload).status_code == status_code
