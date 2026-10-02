from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, call

import pytest
from sqlalchemy.exc import OperationalError

from app import worker
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
    service = MagicMock(return_value=3)
    if name == "expire_stale_bookings":
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
        service.assert_called_once_with(session, timedelta(minutes=30))
        session.commit.assert_called_once_with()
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
    service = MagicMock(side_effect=[OperationalError("query", {}, Exception()), 1])
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
    assert service.call_count == (2 if name == "expire_stale_bookings" else 3)
    assert session.__exit__.call_count == 2
    if name == "expire_stale_bookings":
        session.commit.assert_called_once_with()
    else:
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


def test_webhook_task_keeps_prior_commit_on_later_error(
    session: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = MagicMock(side_effect=[(1, "processed"), ValueError("invalid")])
    monkeypatch.setattr(payments, "retry_failed_webhook_event", service)
    result = worker.retry_failed_webhooks.apply()
    assert result.failed()
    assert isinstance(result.result, ValueError)
    assert service.call_args_list == [call(session, 0), call(session, 1)]
    session.commit.assert_called_once_with()
    session.rollback.assert_called_once_with()
    session.__exit__.assert_called_once()


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
