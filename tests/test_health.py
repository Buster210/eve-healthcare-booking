from collections.abc import Iterator
from time import perf_counter
from typing import Annotated, cast

import pytest
from fastapi import Body, Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app import main
from app.errors import AppError, Conflict, InvalidState, NotFound
from app.schemas.common import Page, pagination_params


def test_health(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.fixture
def ready_engine(db: Session, monkeypatch: pytest.MonkeyPatch) -> Iterator[Engine]:
    bind = cast(Engine, db.get_bind())
    engine = create_engine(bind.url, pool_size=1, max_overflow=0, pool_timeout=1)
    monkeypatch.setattr(main, "readiness_engine", engine)
    yield engine
    engine.dispose()


def test_ready(client: TestClient, ready_engine: Engine) -> None:
    response = client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_database_down_keeps_liveness_available(
    client: TestClient, ready_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail() -> None:
        raise OperationalError("SELECT 1", {}, Exception("private connection details"))

    monkeypatch.setattr(ready_engine, "connect", fail)
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DATABASE_UNAVAILABLE"
    assert "private connection details" not in response.text
    assert response.headers["x-request-id"]
    assert client.get("/health").json() == {"status": "ok"}


def test_ready_statement_timeout(client: TestClient, ready_engine: Engine) -> None:
    def delay_probe(conn, cursor, statement, parameters, context, executemany):
        if statement == "SELECT 1":
            statement = "SELECT pg_sleep(5)"
        return statement, parameters

    event.listen(ready_engine, "before_cursor_execute", delay_probe, retval=True)
    started = perf_counter()
    response = client.get("/health/ready")
    elapsed = perf_counter() - started
    event.remove(ready_engine, "before_cursor_execute", delay_probe)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DATABASE_UNAVAILABLE"
    assert elapsed < 4
    assert client.get("/health/ready").status_code == 200
    with ready_engine.connect() as connection:
        assert connection.scalar(text("SHOW statement_timeout")) == "0"


def test_ready_pool_checkout_timeout(client: TestClient, ready_engine: Engine) -> None:
    with ready_engine.connect():
        started = perf_counter()
        response = client.get("/health/ready")
        assert perf_counter() - started < 3
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DATABASE_UNAVAILABLE"
    assert client.get("/health/ready").status_code == 200


def test_not_found(client: TestClient) -> None:
    response = client.get("/missing")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_method_not_allowed(client: TestClient) -> None:
    response = client.post("/health")
    assert response.status_code == 405
    assert response.json()["error"]["code"] == "METHOD_NOT_ALLOWED"
    assert response.headers["allow"] == "GET"


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (NotFound("No such centre"), 404, "NOT_FOUND"),
        (
            Conflict("Already booked", code="DUPLICATE_BOOKING"),
            409,
            "DUPLICATE_BOOKING",
        ),
        (InvalidState(), 409, "INVALID_STATE"),
        (AppError(409, "CUSTOM", "Conflict", {"id": 3}), 409, "CUSTOM"),
        (HTTPException(403, "Forbidden"), 403, "FORBIDDEN"),
        (RuntimeError("private internal data"), 500, "INTERNAL_ERROR"),
    ],
)
def test_uniform_errors(
    client: TestClient, error: Exception, status: int, code: str
) -> None:
    @cast(FastAPI, client.app).get("/_error")
    def fail() -> None:
        raise error

    response = client.get("/_error")
    assert response.status_code == status
    body = response.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == code
    assert "message" in body["error"]
    assert "private internal data" not in response.text
    if isinstance(error, AppError) and error.details is not None:
        assert body["error"]["details"] == error.details


def test_validation_does_not_echo_input(client: TestClient) -> None:
    @cast(FastAPI, client.app).post("/_validate")
    def validate(value: Annotated[int, Body()]) -> dict[str, int]:
        return {"value": value}

    response = client.post("/_validate", json="sensitive-input")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert response.json()["error"]["details"]
    assert "sensitive-input" not in response.text


@pytest.mark.parametrize(
    ("query", "status"),
    [
        ("", 200),
        ("?limit=1&offset=0", 200),
        ("?limit=100&offset=5", 200),
        ("?limit=0", 422),
        ("?limit=101", 422),
        ("?offset=-1", 422),
    ],
)
def test_pagination(client: TestClient, query: str, status: int) -> None:
    @cast(FastAPI, client.app).get("/_page")
    def page(
        params: Annotated[tuple[int, int], Depends(pagination_params)],
    ) -> Page[int]:
        limit, offset = params
        return Page[int](items=[1, 2], total=2, limit=limit, offset=offset)

    response = client.get(f"/_page{query}")
    assert response.status_code == status
    if status == 200:
        body = response.json()
        assert body["items"] == [1, 2]
        assert body["total"] == 2
        if not query:
            assert body["limit"] == 20
            assert body["offset"] == 0
    else:
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"
