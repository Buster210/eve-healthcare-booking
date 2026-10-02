import logging

from fastapi import FastAPI
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from starlette.types import ASGIApp

from app.config import get_settings
from app.errors import AppError, register_error_handlers
from app.logging import setup_logging
from app.middleware import RequestLoggingMiddleware, record_current_user
from app.routers import auth, bookings, centres, payments, tests
from app.security import get_current_user

logger = logging.getLogger(__name__)
readiness_engine = create_engine(
    get_settings().database_url,
    pool_size=1,
    max_overflow=0,
    pool_timeout=1,
    pool_pre_ping=True,
    connect_args={"connect_timeout": 2},
)


class LoggedFastAPI(FastAPI):
    def build_middleware_stack(self) -> ASGIApp:
        return RequestLoggingMiddleware(super().build_middleware_stack())


def create_app() -> FastAPI:
    setup_logging(get_settings().log_level)
    application = LoggedFastAPI(title="EVE Healthcare Booking", version="0.1.0")
    register_error_handlers(application)
    application.dependency_overrides[get_current_user] = record_current_user
    for router in (
        auth.router,
        centres.router,
        tests.router,
        bookings.router,
        payments.router,
    ):
        application.include_router(router)

    @application.get("/health", tags=["health"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/health/ready", tags=["health"])
    def ready() -> dict[str, str]:
        try:
            with readiness_engine.connect() as connection:
                connection.execute(text("SET LOCAL statement_timeout = '2s'"))
                connection.execute(text("SELECT 1"))
        except SQLAlchemyError as exc:
            logger.warning("database_unavailable")
            raise AppError(503, "DATABASE_UNAVAILABLE", "Database unavailable") from exc
        return {"status": "ok"}

    return application


app = create_app()
