import logging
from datetime import timedelta
from typing import cast

from celery import Celery  # type: ignore[import-untyped]
from celery.signals import beat_init, worker_init  # type: ignore[import-untyped]
from celery.signals import setup_logging as celery_setup_logging
from sqlalchemy.exc import OperationalError

from app.config import get_settings
from app.db import SessionLocal
from app.logging import setup_logging
from app.services import bookings

logger = logging.getLogger(__name__)

app = Celery("eve", broker=get_settings().redis_url)
app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    beat_schedule={
        "expire-stale-bookings": {
            "task": "app.worker.expire_stale_bookings",
            "schedule": 300.0,
        },
        "retry-failed-webhooks": {
            "task": "app.worker.retry_failed_webhooks",
            "schedule": 60.0,
        },
    },
)


def _require_redis_url(sender=None, **kwargs) -> None:
    if not get_settings().redis_url:
        raise SystemExit(
            "REDIS_URL is not set; configure REDIS_URL to run the Celery worker/beat"
        )


worker_init.connect(_require_redis_url)
beat_init.connect(_require_redis_url)


def _setup_celery_logging(sender=None, **kwargs) -> None:
    setup_logging(get_settings().log_level)


celery_setup_logging.connect(_setup_celery_logging)


@app.task(autoretry_for=(OperationalError,), retry_backoff=True, max_retries=5)
def expire_stale_bookings() -> int:
    count = 0
    last_id: int | None = 0
    with SessionLocal() as db:
        try:
            while True:
                cancelled, last_id = bookings.expire_stale_bookings(
                    db, timedelta(minutes=30), after_id=cast(int, last_id)
                )
                if last_id is None:
                    break
                db.commit()
                count += cancelled
        except Exception:
            db.rollback()
            raise
    logger.info("Expired %s stale bookings", count)
    return count


@app.task(autoretry_for=(OperationalError,), retry_backoff=True, max_retries=5)
def retry_failed_webhooks() -> int:
    from app.services.payments import retry_failed_webhook_event

    count = 0
    last_id = 0
    with SessionLocal() as db:
        try:
            while True:
                last_id, result = retry_failed_webhook_event(db, last_id)
                if result == "empty":
                    break
                db.commit()
                count += result == "processed"
        except Exception:
            db.rollback()
            raise
    logger.info("Retried %s failed webhooks", count)
    return count
