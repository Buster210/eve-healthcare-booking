import logging
import re
from time import perf_counter
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.db import get_db
from app.logging import request_id
from app.models import User
from app.security import bearer, get_current_user

logger = logging.getLogger(__name__)


def record_current_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    db: Annotated[Session, Depends(get_db)],
) -> User:
    user = get_current_user(credentials, db)
    request.state.user_id = user.id
    return user


class RequestLoggingMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        identifier = Headers(scope=scope).get("x-request-id", "")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", identifier):
            identifier = uuid4().hex
        token = request_id.set(identifier)
        started = perf_counter()
        status = 500

        async def send_response(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message)["X-Request-ID"] = identifier
            await send(message)

        try:
            await self.app(scope, receive, send_response)
        finally:
            try:
                logger.info(
                    "request_completed",
                    extra={
                        "method": scope["method"],
                        "path": scope["path"],
                        "status": status,
                        "duration_ms": round((perf_counter() - started) * 1000, 3),
                        "user_id": scope.get("state", {}).get("user_id"),
                    },
                )
            finally:
                request_id.reset(token)
