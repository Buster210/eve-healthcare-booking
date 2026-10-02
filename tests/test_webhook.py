import hashlib
import hmac
import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.enums import BookingStatus, PaymentStatus
from app.models import Booking, CentreTest, Payment, User, WebhookEvent
from app.state import transition


@pytest.fixture
def pending_payment(
    client: TestClient,
    db: Session,
    make_user: Callable[..., User],
    make_offering: Callable[..., CentreTest],
    auth_headers: Callable[[User], dict[str, str]],
) -> Payment:
    user = make_user()
    offering = make_offering()
    booking = Booking(
        user_id=user.id,
        centre_id=offering.centre_id,
        test_id=offering.test_id,
        appointment_at=datetime.now(UTC) + timedelta(days=1),
        amount=offering.price,
        status=BookingStatus.PENDING,
    )
    db.add(booking)
    db.commit()
    response = client.post(
        "/payments/",
        headers=auth_headers(user),
        json={"booking_id": booking.id, "simulate_outcome": "PENDING"},
    )
    assert response.status_code == 201, response.text
    return db.get(Payment, response.json()["id"])


def signed_body(payload: dict[str, str]) -> dict[str, Any]:
    body = json.dumps(payload).encode()
    signature = hmac.new(
        get_settings().webhook_secret.get_secret_value().encode(), body, hashlib.sha256
    ).hexdigest()
    return {
        "content": body,
        "headers": {"X-Signature": signature, "Content-Type": "application/json"},
    }


def event(
    payment: Payment, status: str = "SUCCESS", event_id: str = "evt-1"
) -> dict[str, str]:
    return {
        "event_id": event_id,
        "provider_ref": payment.provider_ref,
        "status": status,
    }


@pytest.mark.parametrize("signature", [None, "bad", "é"])
def test_bad_signature_is_rejected_before_database_work(
    client: TestClient,
    db: Session,
    signature: str | None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def unexpected_database_work() -> None:
        pytest.fail("Unsigned webhook reached the database dependency")

    monkeypatch.setitem(
        client.app.dependency_overrides, get_db, unexpected_database_work
    )
    headers = {} if signature is None else {"X-Signature": signature}
    # Invalid JSON must still fail authentication before parsing.
    if signature == "é":
        headers = {b"X-Signature": signature.encode()}
    response = client.post("/payments/webhook/", content=b"not json", headers=headers)
    assert response.status_code == 401, response.text
    assert db.scalar(select(func.count()).select_from(WebhookEvent)) == 0
    assert any(record.message == "webhook_bad_signature" for record in caplog.records)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"event_id": "", "provider_ref": "sim_unknown", "status": "SUCCESS"},
        {"event_id": "evt-1", "provider_ref": "", "status": "SUCCESS"},
        {"event_id": "evt-1", "provider_ref": "sim_unknown", "status": "PENDING"},
        {"event_id": "x" * 101, "provider_ref": "sim_unknown", "status": "SUCCESS"},
        {"event_id": "evt-1", "provider_ref": "x" * 101, "status": "SUCCESS"},
    ],
)
def test_signed_invalid_payload_returns_validation_error(
    client: TestClient, db: Session, payload: dict[str, str]
) -> None:
    response = client.post("/payments/webhook/", **signed_body(payload))
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert db.scalar(select(func.count()).select_from(WebhookEvent)) == 0


def test_signed_invalid_json_returns_validation_error(
    client: TestClient, db: Session
) -> None:
    body = b"not json"
    signature = hmac.new(
        get_settings().webhook_secret.get_secret_value().encode(), body, hashlib.sha256
    ).hexdigest()
    response = client.post(
        "/payments/webhook/", content=body, headers={"X-Signature": signature}
    )
    assert response.status_code == 422, response.text
    assert db.scalar(select(func.count()).select_from(WebhookEvent)) == 0


@pytest.mark.parametrize(
    ("outcome", "booking_status"), [("SUCCESS", "CONFIRMED"), ("FAILED", "FAILED")]
)
def test_webhook_resolves_pending_payment(
    client: TestClient,
    db: Session,
    pending_payment: Payment,
    outcome: str,
    booking_status: str,
) -> None:
    response = client.post(
        "/payments/webhook/", **signed_body(event(pending_payment, status=outcome))
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"result": "processed"}
    db.refresh(pending_payment)
    assert pending_payment.status == outcome
    assert db.get(Booking, pending_payment.booking_id).status == booking_status
    stored = db.scalars(select(WebhookEvent)).one()
    assert stored.payload == event(pending_payment, status=outcome)


def test_signature_covers_exact_raw_body(
    client: TestClient, db: Session, pending_payment: Payment
) -> None:
    payload = event(pending_payment)
    body = json.dumps(payload, indent=2).encode() + b"\n"
    signature = hmac.new(
        get_settings().webhook_secret.get_secret_value().encode(), body, hashlib.sha256
    ).hexdigest()
    response = client.post(
        "/payments/webhook/",
        content=body,
        headers={"X-Signature": signature, "Content-Type": "application/json"},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"result": "processed"}


def test_tampered_body_is_rejected(
    client: TestClient, db: Session, pending_payment: Payment
) -> None:
    request = signed_body(event(pending_payment))
    request["content"] = request["content"].replace(b"SUCCESS", b"FAILED")
    response = client.post("/payments/webhook/", **request)
    assert response.status_code == 401, response.text
    db.refresh(pending_payment)
    assert pending_payment.status == PaymentStatus.PENDING
    assert db.scalar(select(func.count()).select_from(WebhookEvent)) == 0


def test_duplicate_webhook_does_not_change_booking(
    client: TestClient,
    db: Session,
    pending_payment: Payment,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO", logger="app.services.payments")
    payload = event(pending_payment)
    first = client.post("/payments/webhook/", **signed_body(payload))
    assert first.status_code == 200, first.text
    assert first.json() == {"result": "processed"}
    payload["status"] = "FAILED"
    second = client.post("/payments/webhook/", **signed_body(payload))
    assert second.status_code == 200, second.text
    assert second.json() == {"result": "duplicate"}
    db.refresh(pending_payment)
    assert pending_payment.status == PaymentStatus.SUCCESS
    assert db.get(Booking, pending_payment.booking_id).status == BookingStatus.CONFIRMED
    assert db.scalar(select(func.count()).select_from(WebhookEvent)) == 1
    duplicate = next(
        record for record in caplog.records if record.message == "webhook_duplicate"
    )
    assert duplicate.event_id == payload["event_id"]


def test_illegal_booking_transition_undoes_payment_change(
    client: TestClient, db: Session, pending_payment: Payment
) -> None:
    booking = db.get(Booking, pending_payment.booking_id)
    transition(booking, BookingStatus.CANCELLED)
    db.commit()
    response = client.post("/payments/webhook/", **signed_body(event(pending_payment)))
    assert response.status_code == 200, response.text
    assert response.json() == {"result": "ignored"}
    db.refresh(pending_payment)
    db.refresh(booking)
    assert pending_payment.status == PaymentStatus.PENDING
    assert booking.status == BookingStatus.CANCELLED
    assert db.scalar(select(func.count()).select_from(WebhookEvent)) == 1


def test_unknown_payment_rolls_back_event(client: TestClient, db: Session) -> None:
    payload = {
        "event_id": "evt-unknown",
        "provider_ref": "sim_unknown",
        "status": "SUCCESS",
    }
    for _ in range(2):
        response = client.post("/payments/webhook/", **signed_body(payload))
        assert response.status_code == 404, response.text
        assert db.scalar(select(func.count()).select_from(WebhookEvent)) == 0


def test_unknown_payment_event_can_be_retried(
    client: TestClient, db: Session, pending_payment: Payment
) -> None:
    payload = event(pending_payment)
    provider_ref = pending_payment.provider_ref
    pending_payment.provider_ref = "sim_not_announced"
    db.commit()
    missing = client.post("/payments/webhook/", **signed_body(payload))
    assert missing.status_code == 404, missing.text
    assert db.scalar(select(func.count()).select_from(WebhookEvent)) == 0
    pending_payment.provider_ref = provider_ref
    db.commit()
    retry = client.post("/payments/webhook/", **signed_body(payload))
    assert retry.status_code == 200, retry.text
    assert retry.json() == {"result": "processed"}


@pytest.mark.parametrize("outcome", ["SUCCESS", "FAILED"])
def test_repeated_outcome_with_new_event_is_no_op(
    client: TestClient, db: Session, pending_payment: Payment, outcome: str
) -> None:
    response = client.post(
        "/payments/webhook/", **signed_body(event(pending_payment, status=outcome))
    )
    assert response.json() == {"result": "processed"}
    response = client.post(
        "/payments/webhook/",
        **signed_body(event(pending_payment, status=outcome, event_id="evt-2")),
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"result": "no_op"}
    assert db.scalar(select(func.count()).select_from(WebhookEvent)) == 2


@pytest.mark.parametrize(
    ("first", "stale", "booking_status"),
    [("SUCCESS", "FAILED", "CONFIRMED"), ("FAILED", "SUCCESS", "FAILED")],
)
def test_stale_outcome_is_ignored(
    client: TestClient,
    db: Session,
    pending_payment: Payment,
    first: str,
    stale: str,
    booking_status: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    response = client.post(
        "/payments/webhook/", **signed_body(event(pending_payment, status=first))
    )
    assert response.json() == {"result": "processed"}
    response = client.post(
        "/payments/webhook/",
        **signed_body(event(pending_payment, status=stale, event_id="evt-stale")),
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"result": "ignored"}
    db.refresh(pending_payment)
    assert pending_payment.status == first
    assert db.get(Booking, pending_payment.booking_id).status == booking_status
    assert db.scalar(select(func.count()).select_from(WebhookEvent)) == 2
    assert any(record.message == "webhook_ignored" for record in caplog.records)


def test_webhook_failure_rolls_back_and_retry_processes(
    client: TestClient,
    db: Session,
    pending_payment: Payment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import payments

    apply_outcome = payments.apply_payment_outcome

    def fail_after_outcome(
        booking: Booking, payment: Payment, outcome: PaymentStatus | str
    ) -> None:
        apply_outcome(booking, payment, outcome)
        raise RuntimeError("Simulated transaction failure")

    request = signed_body(event(pending_payment))
    monkeypatch.setattr(payments, "apply_payment_outcome", fail_after_outcome)
    with TestClient(client.app, raise_server_exceptions=False) as request_client:
        response = request_client.post("/payments/webhook/", **request)
    assert response.status_code == 500, response.text
    db.refresh(pending_payment)
    assert pending_payment.status == PaymentStatus.PENDING
    assert db.get(Booking, pending_payment.booking_id).status == BookingStatus.PENDING
    assert db.scalar(select(func.count()).select_from(WebhookEvent)) == 0
    monkeypatch.setattr(payments, "apply_payment_outcome", apply_outcome)
    response = client.post("/payments/webhook/", **request)
    assert response.status_code == 200, response.text
    assert response.json() == {"result": "processed"}


def test_concurrent_webhooks_process_event_only_once(
    client: TestClient, db: Session, pending_payment: Payment
) -> None:
    request = signed_body(event(pending_payment))
    barrier = Barrier(6)

    def send(_: int) -> Response:
        with TestClient(client.app) as request_client:
            barrier.wait(timeout=15)
            return request_client.post("/payments/webhook/", **request)

    with ThreadPoolExecutor(max_workers=6) as executor:
        responses = list(executor.map(send, range(6)))

    assert all(response.status_code == 200 for response in responses)
    assert sorted(response.json()["result"] for response in responses) == [
        "duplicate"
    ] * 5 + ["processed"]
    assert db.scalar(select(func.count()).select_from(WebhookEvent)) == 1
    db.refresh(pending_payment)
    assert pending_payment.status == PaymentStatus.SUCCESS
    assert db.get(Booking, pending_payment.booking_id).status == BookingStatus.CONFIRMED
