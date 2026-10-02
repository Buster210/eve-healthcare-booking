import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, call
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app import worker
from app.enums import BookingStatus, PaymentStatus
from app.logging import JsonFormatter
from app.models import Booking, CentreTest, Payment, User
from app.services import bookings, payments


@pytest.fixture
def session(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    db = MagicMock()
    db.__enter__.return_value = db
    monkeypatch.setattr(worker, "SessionLocal", MagicMock(return_value=db))
    return db


@pytest.mark.parametrize("name", ["expire_stale_bookings", "retry_failed_webhooks"])
def test_task_closes_session_and_returns_service_count(
    session: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    name: str,
) -> None:
    service = MagicMock()
    if name == "expire_stale_bookings":
        service.side_effect = [(0, 2), (3, 4), (0, None)]
        monkeypatch.setattr(bookings, name, service)
    else:
        service.side_effect = [
            (1, "processed"),
            (2, "busy"),
            (3, "failed"),
            (4, "processed"),
            (5, "processed"),
            (5, "empty"),
        ]
        monkeypatch.setattr(payments, "retry_failed_webhook_event", service)
    with caplog.at_level("INFO", logger="app.worker"):
        result = getattr(worker, name).apply(throw=True)
    assert result.get() == 3
    if name == "expire_stale_bookings":
        assert service.call_args_list == [
            call(session, timedelta(minutes=30), after_id=cursor)
            for cursor in [0, 2, 4]
        ]
        assert session.commit.call_count == 2
    else:
        assert service.call_args_list == [call(session, cursor) for cursor in range(6)]
        assert session.commit.call_count == 5
    session.rollback.assert_not_called()
    session.__exit__.assert_called_once_with(None, None, None)
    assert "3" in caplog.text


@pytest.mark.parametrize("name", ["expire_stale_bookings", "retry_failed_webhooks"])
def test_task_retries_operational_errors(
    session: MagicMock, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    service = MagicMock(
        side_effect=[OperationalError("query", {}, Exception()), (1, 1), (0, None)]
    )
    if name == "expire_stale_bookings":
        monkeypatch.setattr(bookings, name, service)
    else:
        service.side_effect = [
            OperationalError("query", {}, Exception()),
            (1, "processed"),
            (1, "empty"),
        ]
        monkeypatch.setattr(payments, "retry_failed_webhook_event", service)
    task = getattr(worker, name)
    assert task.apply().get() == 1
    assert service.call_count == 3
    assert session.__exit__.call_count == 2
    session.commit.assert_called_once_with()
    session.rollback.assert_called_once_with()
    assert task.retry_backoff is True
    assert task.max_retries == 5


def test_task_stops_after_five_retries(
    session: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    error = OperationalError("query", {}, Exception())
    service = MagicMock(side_effect=error)
    monkeypatch.setattr(bookings, "expire_stale_bookings", service)
    result = worker.expire_stale_bookings.apply()
    assert result.failed()
    assert isinstance(result.result, OperationalError)
    assert service.call_count == 6
    assert session.__exit__.call_count == 6
    session.commit.assert_not_called()


def test_task_does_not_retry_other_errors(
    session: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = MagicMock(side_effect=ValueError("invalid"))
    monkeypatch.setattr(bookings, "expire_stale_bookings", service)
    result = worker.expire_stale_bookings.apply()
    assert result.failed()
    service.assert_called_once()
    session.commit.assert_not_called()
    session.__exit__.assert_called_once()


@pytest.mark.parametrize("name", ["expire_stale_bookings", "retry_failed_webhooks"])
def test_task_keeps_prior_commit_on_later_error(
    session: MagicMock, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    service = MagicMock()
    if name == "expire_stale_bookings":
        service.side_effect = [(1, 1), ValueError("invalid")]
        monkeypatch.setattr(bookings, name, service)
    else:
        service.side_effect = [(1, "processed"), ValueError("invalid")]
        monkeypatch.setattr(payments, "retry_failed_webhook_event", service)
    result = getattr(worker, name).apply()
    assert result.failed()
    assert isinstance(result.result, ValueError)
    if name == "expire_stale_bookings":
        assert service.call_args_list == [
            call(session, timedelta(minutes=30), after_id=cursor) for cursor in [0, 1]
        ]
    else:
        assert service.call_args_list == [call(session, 0), call(session, 1)]
    session.commit.assert_called_once_with()
    session.rollback.assert_called_once_with()
    session.__exit__.assert_called_once()


@pytest.mark.parametrize("booking_count", [4, 5])
def test_expiry_task_commits_each_batch_and_processes_all_stale_bookings(
    db: Session,
    make_user: Callable[..., User],
    make_offering: Callable[..., CentreTest],
    monkeypatch: pytest.MonkeyPatch,
    booking_count: int,
) -> None:
    user = make_user()
    offering = make_offering()
    now = datetime.now(UTC)
    items = [
        Booking(
            user_id=user.id,
            centre_id=offering.centre_id,
            test_id=offering.test_id,
            appointment_at=now + timedelta(days=index + 1),
            amount=offering.price,
            created_at=now - timedelta(hours=1),
        )
        for index in range(booking_count)
    ]
    db.add_all(items)
    db.flush()
    for booking, status in zip(
        items[:3],
        [PaymentStatus.PENDING, PaymentStatus.SUCCESS, PaymentStatus.FAILED],
        strict=True,
    ):
        db.add(
            Payment(
                booking_id=booking.id,
                amount=booking.amount,
                provider_ref=f"sim_{uuid4().hex}",
                status=status,
            )
        )
    db.commit()
    snapshots = []
    commit = db.commit
    expire = bookings.expire_stale_bookings
    cursors = []

    def expire_batch(db: Session, older_than: timedelta, *, after_id: int = 0):
        cursors.append(after_id)
        return expire(db, older_than, after_id=after_id, batch_size=2)

    def record_commit() -> None:
        commit()
        with Session(db.get_bind()) as other:
            snapshots.append(
                list(
                    other.scalars(
                        select(Booking.status)
                        .order_by(Booking.id)
                        .with_for_update(nowait=True)
                    )
                )
            )

    monkeypatch.setattr(worker, "SessionLocal", lambda: db)
    monkeypatch.setattr(bookings, "expire_stale_bookings", expire_batch)
    monkeypatch.setattr(db, "commit", record_commit)
    assert worker.expire_stale_bookings.apply(throw=True).get() == booking_count - 2
    pending = BookingStatus.PENDING
    cancelled = BookingStatus.CANCELLED
    expected = [pending, pending] + [cancelled] * (booking_count - 2)
    assert snapshots == [
        [pending] * booking_count,
        [pending, pending, cancelled, cancelled] + [pending] * (booking_count - 4),
    ] + ([expected] if booking_count == 5 else [])
    assert cursors == [0, items[1].id, items[3].id] + (
        [items[4].id] if booking_count == 5 else []
    )


def test_worker_uses_json_and_periodic_schedule() -> None:
    assert worker.app.conf.task_serializer == "json"
    assert worker.app.conf.result_serializer == "json"
    assert worker.app.conf.accept_content == ["json"]
    schedule = worker.app.conf.beat_schedule
    assert schedule["expire-stale-bookings"] == {
        "task": "app.worker.expire_stale_bookings",
        "schedule": 300.0,
    }
    assert schedule["retry-failed-webhooks"] == {
        "task": "app.worker.retry_failed_webhooks",
        "schedule": 60.0,
    }


def test_missing_redis_url_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker, "get_settings", lambda: SimpleNamespace(redis_url=None))
    with pytest.raises(SystemExit, match="REDIS_URL is not set"):
        worker._require_redis_url(sender=None)
    monkeypatch.setattr(worker, "get_settings", lambda: SimpleNamespace(redis_url=""))
    with pytest.raises(SystemExit, match="REDIS_URL is not set"):
        worker._require_redis_url(sender=None)
    monkeypatch.setattr(
        worker,
        "get_settings",
        lambda: SimpleNamespace(redis_url="redis://localhost:6379/0"),
    )
    worker._require_redis_url(sender=None)


def test_celery_setup_logging_uses_json_formatter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        worker, "get_settings", lambda: SimpleNamespace(log_level="INFO")
    )
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    try:
        worker.celery_setup_logging.send(sender=worker.app)
        assert any(
            isinstance(handler.formatter, JsonFormatter) for handler in root.handlers
        )
    finally:
        root.handlers[:] = handlers
        root.setLevel(level)
