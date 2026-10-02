import logging

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

logger = logging.getLogger(__name__)


class AppError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: object | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


class NotFound(AppError):
    def __init__(self, message: str = "Resource not found") -> None:
        super().__init__(404, "NOT_FOUND", message)


class Conflict(AppError):
    def __init__(
        self,
        message: str = "Resource already exists",
        code: str = "CONFLICT",
        details: object | None = None,
    ) -> None:
        super().__init__(409, code, message, details)


class Unauthorized(AppError):
    def __init__(self, message: str = "Invalid or missing credentials") -> None:
        super().__init__(401, "UNAUTHORIZED", message)


class Forbidden(AppError):
    def __init__(self, message: str = "Admin access required") -> None:
        super().__init__(403, "FORBIDDEN", message)


class InvalidState(AppError):
    def __init__(
        self, message: str = "Operation not allowed in the current state"
    ) -> None:
        super().__init__(409, "INVALID_STATE", message)


def error_response(
    status_code: int,
    code: str,
    message: str,
    details: object | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    error: dict[str, object] = {"code": code, "message": message}
    if details is not None:
        error["details"] = jsonable_encoder(details)
    if status_code == 401:
        headers = {**(headers or {}), "WWW-Authenticate": "Bearer"}
    return JSONResponse({"error": error}, status_code=status_code, headers=headers)


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        return error_response(exc.status_code, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        details = [
            {"location": error["loc"], "message": error["msg"], "type": error["type"]}
            for error in exc.errors()
        ]
        return error_response(
            422, "VALIDATION_ERROR", "Request validation failed", details
        )

    @app.exception_handler(HTTPException)
    def handle_http_error(request: Request, exc: HTTPException) -> JSONResponse:
        if exc.status_code >= 500:
            return error_response(
                exc.status_code,
                "INTERNAL_ERROR",
                "Internal server error",
                headers=exc.headers,
            )
        codes = {
            401: "UNAUTHORIZED",
            403: "FORBIDDEN",
            404: "NOT_FOUND",
            405: "METHOD_NOT_ALLOWED",
        }
        return error_response(
            exc.status_code,
            codes.get(exc.status_code, "HTTP_ERROR"),
            str(exc.detail),
            headers=exc.headers,
        )

    @app.exception_handler(Exception)
    def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        logger.error("unhandled_exception")
        return error_response(500, "INTERNAL_ERROR", "Internal server error")
