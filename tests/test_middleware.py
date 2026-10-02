import json
import logging
from collections.abc import Callable
from typing import cast
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.logging import JsonFormatter, request_id
from app.main import create_app
from app.models import User


@pytest.mark.parametrize(("path", "status"), [("/health", 200), ("/missing", 404)])
def test_request_id_round_trip(client: TestClient, path: str, status: int) -> None:
    response = client.get(path, headers={"X-Request-ID": "client-request-123"})
    assert response.status_code == status
    assert response.headers["x-request-id"] == "client-request-123"


def test_generated_request_ids_are_unique(client: TestClient) -> None:
    first = client.get("/health").headers["x-request-id"]
    second = client.get("/health").headers["x-request-id"]
    assert UUID(first).version == 4
    assert UUID(second).version == 4
    assert first != second
    assert request_id.get() is None


@pytest.mark.parametrize("identifier", ["A._-09", "x" * 64])
def test_request_id_allowlist_boundary(client: TestClient, identifier: str) -> None:
    response = client.get("/health", headers={"X-Request-ID": identifier})
    assert response.headers["x-request-id"] == identifier


@pytest.mark.parametrize(
    "identifier",
    ["", "x" * 65, "x" * 131072, "has space", "\t", "\r\n", "\x00", "\x7f"],
)
def test_invalid_request_ids_are_replaced(
    client: TestClient, caplog: pytest.LogCaptureFixture, identifier: str
) -> None:
    caplog.handler.setFormatter(JsonFormatter())
    with caplog.at_level(logging.INFO, logger="app"):
        response = client.get("/health", headers={"X-Request-ID": identifier})
    generated = response.headers["x-request-id"]
    assert UUID(generated).version == 4
    rows = [json.loads(line) for line in caplog.text.splitlines()]
    assert rows[0]["request_id"] == generated


def test_middleware_and_exception_handlers_can_be_registered_before_start() -> None:
    application = create_app()
    assert application.middleware_stack is None
    application.add_middleware(TrustedHostMiddleware, allowed_hosts=["testserver"])

    @application.exception_handler(ValueError)
    async def handle_value_error(request: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse({"handled": True}, status_code=418)

    @application.get("/_late_handler")
    def fail() -> None:
        raise ValueError("failure")

    with TestClient(application, raise_server_exceptions=False) as test_client:
        response = test_client.get("/_late_handler")
    assert response.status_code == 418
    assert response.json() == {"handled": True}
    assert response.headers["x-request-id"]


def test_access_and_application_logs_share_request_id(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    @cast(FastAPI, client.app).get("/_logged")
    def logged() -> dict[str, str]:
        logging.getLogger("app.test").info("application_event")
        return {"status": "ok"}

    caplog.handler.setFormatter(JsonFormatter())
    with caplog.at_level(logging.INFO, logger="app"):
        response = client.get(
            "/_logged?private=value", headers={"X-Request-ID": "log-id"}
        )
    rows = [json.loads(line) for line in caplog.text.splitlines()]
    events = [row for row in rows if row["logger"].startswith("app.")]
    assert response.status_code == 200
    assert len(events) == 2
    assert {event["request_id"] for event in events} == {"log-id"}
    access = [row for row in events if row["message"] == "request_completed"]
    assert len(access) == 1
    assert access[0]["method"] == "GET"
    assert access[0]["path"] == "/_logged"
    assert access[0]["status"] == 200
    assert access[0]["duration_ms"] >= 0
    assert access[0]["user_id"] is None


def test_unhandled_errors_keep_request_id_and_one_access_log(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    @cast(FastAPI, client.app).get("/_broken")
    def broken() -> None:
        raise RuntimeError("failure")

    caplog.handler.setFormatter(JsonFormatter())
    with caplog.at_level(logging.INFO, logger="app"):
        response = client.get("/_broken", headers={"X-Request-ID": "error-id"})
    assert response.status_code == 500
    assert response.headers["x-request-id"] == "error-id"
    events = [
        row
        for line in caplog.text.splitlines()
        if (row := json.loads(line))["logger"].startswith("app.")
    ]
    assert {event["request_id"] for event in events} == {"error-id"}
    access = [row for row in events if row["message"] == "request_completed"]
    assert len(access) == 1
    assert access[0]["status"] == 500
    unhandled = [row for row in events if row["message"] == "unhandled_exception"]
    assert len(unhandled) == 1
    assert "RuntimeError: failure" in unhandled[0]["exc_info"]


def test_access_log_records_authenticated_user_without_leaking_to_next_request(
    client: TestClient,
    make_user: Callable[..., User],
    auth_headers: Callable[[User], dict[str, str]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    user = make_user()
    with caplog.at_level(logging.INFO, logger="app"):
        assert client.get("/auth/me", headers=auth_headers(user)).status_code == 200
        assert client.get("/health").status_code == 200
    access = [record for record in caplog.records if record.msg == "request_completed"]
    assert len(access) == 2
    assert access[0].__dict__["user_id"] == user.id
    assert access[1].__dict__["user_id"] is None


def test_logs_outside_requests_have_null_request_id() -> None:
    record = logging.LogRecord("app.test", logging.INFO, "", 0, "event", (), None)
    record.centre_id, record.test_id = 12, 34
    data = json.loads(JsonFormatter().format(record))
    assert data["request_id"] is None
    assert (data["centre_id"], data["test_id"]) == (12, 34)


def test_exception_logs_include_exception_and_stack_info(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.handler.setFormatter(JsonFormatter())
    with caplog.at_level(logging.ERROR, logger="app.test"):
        try:
            raise ValueError("payment failed")
        except ValueError:
            logging.getLogger("app.test").exception("webhook failed", stack_info=True)

    data = json.loads(caplog.text)
    assert "ValueError: payment failed" in data["exc_info"]
    assert data["stack_info"].startswith("Stack (most recent call last):")
