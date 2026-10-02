from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated
from unittest.mock import MagicMock, Mock

import conftest
import jwt
import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.config import get_settings
from app.errors import register_error_handlers
from app.models import User
from app.security import (
    create_access_token,
    get_current_user,
    hash_password,
    require_admin,
    verify_password,
)


@pytest.fixture
def secured_client(client: TestClient) -> TestClient:
    @client.app.get("/_me")
    def me(user: Annotated[User, Depends(get_current_user)]) -> dict[str, int]:
        return {"id": user.id}

    @client.app.get("/_admin")
    def admin(user: Annotated[User, Depends(require_admin)]) -> dict[str, int]:
        return {"id": user.id}

    return client


def test_password_hashing() -> None:
    password = "a-long-test-password"
    encoded = hash_password(password)
    assert encoded.startswith("$argon2id$")
    assert encoded != password
    assert verify_password(password, encoded)
    assert not verify_password("wrong-password", encoded)
    assert hash_password(password) != encoded


def test_token_round_trip(
    secured_client: TestClient,
    make_user: Callable[..., User],
    auth_headers: Callable[[User], dict[str, str]],
) -> None:
    user = make_user()
    token = create_access_token(user.id)
    claims = jwt.decode(
        token, get_settings().jwt_secret.get_secret_value(), algorithms=["HS256"]
    )
    assert claims["sub"] == str(user.id)
    assert claims["exp"] - claims["iat"] == get_settings().jwt_expire_minutes * 60
    response = secured_client.get("/_me", headers=auth_headers(user))
    assert response.status_code == 200
    assert response.json() == {"id": user.id}


@pytest.mark.parametrize(
    "headers", [{}, {"Authorization": "Basic abc"}, {"Authorization": "Bearer garbage"}]
)
def test_missing_or_garbage_token(
    secured_client: TestClient, headers: dict[str, str]
) -> None:
    response = secured_client.get("/_me", headers=headers)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "case",
    [
        "expired",
        "wrong_key",
        "wrong_algorithm",
        "missing_exp",
        "invalid_exp",
        "invalid_subject",
        "huge_subject",
        "missing_user",
    ],
)
def test_invalid_tokens(
    secured_client: TestClient, make_user: Callable[..., User], case: str
) -> None:
    user = make_user()
    now = datetime.now(UTC)
    claims: dict[str, object] = {"sub": str(user.id), "exp": now + timedelta(minutes=5)}
    key = get_settings().jwt_secret.get_secret_value()
    algorithm = "HS256"
    if case == "expired":
        claims["exp"] = now - timedelta(minutes=1)
    elif case == "wrong_key":
        key = "wrong-signing-key-at-least-32-characters"
    elif case == "wrong_algorithm":
        algorithm = "HS512"
        key = key.ljust(64, "x")
    elif case == "missing_exp":
        del claims["exp"]
    elif case == "invalid_exp":
        claims["exp"] = float("inf")
    elif case == "invalid_subject":
        claims["sub"] = "not-an-id"
    elif case == "huge_subject":
        claims["sub"] = str(2**100)
    elif case == "missing_user":
        claims["sub"] = "999999"
    token = jwt.encode(claims, key, algorithm=algorithm)
    response = secured_client.get("/_me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


def test_admin_guard(
    secured_client: TestClient,
    make_user: Callable[..., User],
    auth_headers: Callable[[User], dict[str, str]],
) -> None:
    user = make_user()
    admin = make_user(is_admin=True)
    anonymous_response = secured_client.get("/_admin")
    assert anonymous_response.status_code == 401
    assert anonymous_response.json()["error"]["code"] == "UNAUTHORIZED"
    user_response = secured_client.get("/_admin", headers=auth_headers(user))
    assert user_response.status_code == 403
    assert user_response.json()["error"]["code"] == "FORBIDDEN"
    admin_response = secured_client.get("/_admin", headers=auth_headers(admin))
    assert admin_response.status_code == 200
    assert admin_response.json() == {"id": admin.id}


@pytest.mark.parametrize("status", [500, 503, 599])
def test_http_server_errors_hide_details(status: int) -> None:
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/_error")
    def fail() -> None:
        raise HTTPException(
            status, "private internal data", headers={"Retry-After": "5"}
        )

    with TestClient(app) as client:
        response = client.get("/_error")
    assert response.status_code == status
    assert response.json() == {
        "error": {"code": "INTERNAL_ERROR", "message": "Internal server error"}
    }
    assert response.headers["retry-after"] == "5"


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql+psycopg://localhost/eve",
        "postgresql+psycopg://localhost/eve_test_c?dbname=eve",
        "postgresql+psycopg://localhost/eve_test_c?db%6eame=eve",
        "postgresql+psycopg://localhost/eve_test_c?dbname=eve_test&dbname=eve",
        "postgresql+psycopg://localhost/eve_test_c?service=production",
        "sqlite:///eve_test",
        "invalid-url",
    ],
)
def test_database_url_guard_rejects_overrides_before_connecting(
    monkeypatch: pytest.MonkeyPatch, database_url: str
) -> None:
    monkeypatch.setenv("TEST_DATABASE_URL", database_url)
    create_engine = Mock()
    upgrade = Mock()
    monkeypatch.setattr(conftest, "create_engine", create_engine)
    monkeypatch.setattr(conftest.command, "upgrade", upgrade)
    with pytest.raises(pytest.fail.Exception):
        next(conftest.test_engine.__wrapped__())
    create_engine.assert_not_called()
    upgrade.assert_not_called()


@pytest.mark.parametrize("database", ["eve", "production", None])
def test_connected_database_guard_runs_before_migration(
    monkeypatch: pytest.MonkeyPatch, database: str | None
) -> None:
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql+psycopg://localhost/eve_test")
    engine = MagicMock()
    connection = engine.connect.return_value.__enter__.return_value
    connection.scalar.return_value = database
    monkeypatch.setattr(conftest, "create_engine", Mock(return_value=engine))
    upgrade = Mock()
    monkeypatch.setattr(conftest.command, "upgrade", upgrade)
    with pytest.raises(pytest.fail.Exception, match="Connected database"):
        next(conftest.test_engine.__wrapped__())
    assert str(connection.scalar.call_args.args[0]) == "SELECT current_database()"
    upgrade.assert_not_called()
    engine.dispose.assert_called_once()


def test_connected_database_guard_runs_before_cleanup() -> None:
    engine = MagicMock()
    connection = engine.begin.return_value.__enter__.return_value
    connection.scalar.return_value = "eve"
    with pytest.raises(pytest.fail.Exception, match="Connected database"):
        next(conftest.db.__wrapped__(engine))
    assert str(connection.scalar.call_args.args[0]) == "SELECT current_database()"
    connection.execute.assert_not_called()
