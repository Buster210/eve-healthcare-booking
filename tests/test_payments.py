from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Barrier
from types import SimpleNamespace
from typing import cast

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.enums import BookingStatus, PaymentStatus
from app.models import Booking, CentreTest, Payment, User
from app.state import transition


@pytest.fixture
def booking(
    db: Session,
    make_user: Callable[..., User],
    make_offering: Callable[..., CentreTest],
) -> tuple[Booking, User]:
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
    return booking, user


@pytest.mark.parametrize(
    ("outcome", "booking_status"),
    [("SUCCESS", "CONFIRMED"), ("FAILED", "FAILED"), ("PENDING", "PENDING")],
)
def test_payment_outcome_updates_booking(
    client: TestClient,
    db: Session,
    booking: tuple[Booking, User],
    auth_headers: Callable[[User], dict[str, str]],
    outcome: str,
    booking_status: str,
) -> None:
    row, user = booking
    response = client.post(
        "/payments/",
        headers=auth_headers(user),
        json={"booking_id": row.id, "simulate_outcome": outcome},
    )
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["booking_id"] == row.id
    assert Decimal(data["amount"]) == row.amount
    assert data["provider_ref"].startswith("sim_")
    assert data["status"] == outcome
    assert data["booking_status"] == booking_status
    db.expire_all()
    stored_booking = cast(Booking, db.get(Booking, row.id))
    assert stored_booking.status == booking_status
    stored_payment = cast(Payment, db.get(Payment, data["id"]))
    assert stored_payment.status == outcome


def test_payment_uses_booking_price_snapshot(
    client: TestClient,
    db: Session,
    booking: tuple[Booking, User],
    auth_headers: Callable[[User], dict[str, str]],
) -> None:
    row, user = booking
    offering = cast(
        CentreTest,
        db.scalar(
            select(CentreTest).where(
                CentreTest.centre_id == row.centre_id, CentreTest.test_id == row.test_id
            )
        ),
    )
    original_amount = row.amount
    offering.price = Decimal("999.00")
    db.commit()
    response = client.post(
        "/payments/",
        headers=auth_headers(user),
        json={"booking_id": row.id, "simulate_outcome": "PENDING"},
    )
    assert response.status_code == 201, response.text
    assert Decimal(response.json()["amount"]) == original_amount


def test_second_pending_payment_returns_payment_exists(
    client: TestClient,
    db: Session,
    booking: tuple[Booking, User],
    auth_headers: Callable[[User], dict[str, str]],
) -> None:
    row, user = booking
    payload = {"booking_id": row.id, "simulate_outcome": "PENDING"}
    headers = auth_headers(user)
    assert client.post("/payments/", headers=headers, json=payload).status_code == 201
    response = client.post("/payments/", headers=headers, json=payload)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "PAYMENT_EXISTS"
    assert db.scalar(select(func.count()).select_from(Payment)) == 1


def test_terminal_booking_rejects_second_payment(
    client: TestClient,
    db: Session,
    booking: tuple[Booking, User],
    auth_headers: Callable[[User], dict[str, str]],
) -> None:
    row, user = booking
    payload = {"booking_id": row.id, "simulate_outcome": "SUCCESS"}
    headers = auth_headers(user)
    assert client.post("/payments/", headers=headers, json=payload).status_code == 201
    response = client.post("/payments/", headers=headers, json=payload)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "INVALID_STATE"
    assert db.scalar(select(func.count()).select_from(Payment)) == 1


def test_other_users_booking_is_not_found(
    client: TestClient,
    db: Session,
    booking: tuple[Booking, User],
    make_user: Callable[..., User],
    auth_headers: Callable[[User], dict[str, str]],
) -> None:
    row, _ = booking
    other = make_user()
    response = client.post(
        "/payments/",
        headers=auth_headers(other),
        json={"booking_id": row.id, "simulate_outcome": "SUCCESS"},
    )
    assert response.status_code == 404, response.text
    assert db.scalar(select(func.count()).select_from(Payment)) == 0
    db.refresh(row)
    assert row.status == BookingStatus.PENDING


@pytest.mark.parametrize("outcome", ["PENDING", "SUCCESS", "FAILED"])
def test_failed_booking_accepts_another_payment(
    client: TestClient,
    db: Session,
    booking: tuple[Booking, User],
    auth_headers: Callable[[User], dict[str, str]],
    outcome: str,
) -> None:
    row, user = booking
    headers = auth_headers(user)
    first = client.post(
        "/payments/",
        headers=headers,
        json={"booking_id": row.id, "simulate_outcome": "FAILED"},
    )
    assert first.status_code == 201, first.text
    response = client.post(
        "/payments/",
        headers=headers,
        json={"booking_id": row.id, "simulate_outcome": outcome},
    )
    assert response.status_code == 201, response.text
    assert response.json()["id"] != first.json()["id"]
    assert response.json()["status"] == outcome
    db.expire_all()
    stored_payment = cast(Payment, db.get(Payment, first.json()["id"]))
    assert stored_payment.status == PaymentStatus.FAILED
    assert db.scalar(select(func.count()).select_from(Payment)) == 2


@pytest.mark.parametrize("retry", [False, True])
@pytest.mark.parametrize("elapsed", [0, 1])
def test_payment_rejects_elapsed_appointment(
    client: TestClient,
    db: Session,
    booking: tuple[Booking, User],
    auth_headers: Callable[[User], dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
    retry: bool,
    elapsed: int,
) -> None:
    from app.services import payments

    row, user = booking
    headers = auth_headers(user)
    if retry:
        response = client.post(
            "/payments/",
            headers=headers,
            json={"booking_id": row.id, "simulate_outcome": "FAILED"},
        )
        assert response.status_code == 201, response.text
    now = row.appointment_at + timedelta(seconds=elapsed)
    monkeypatch.setattr(
        payments, "datetime", SimpleNamespace(now=lambda tz: now), raising=False
    )
    response = client.post(
        "/payments/",
        headers=headers,
        json={"booking_id": row.id, "simulate_outcome": "SUCCESS"},
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "INVALID_STATE"
    db.refresh(row)
    assert row.status == (BookingStatus.FAILED if retry else BookingStatus.PENDING)
    assert db.scalar(select(func.count()).select_from(Payment)) == int(retry)


def test_payment_attempts_are_capped_at_three(
    client: TestClient,
    db: Session,
    booking: tuple[Booking, User],
    auth_headers: Callable[[User], dict[str, str]],
) -> None:
    row, user = booking
    headers = auth_headers(user)
    for _ in range(3):
        response = client.post(
            "/payments/",
            headers=headers,
            json={"booking_id": row.id, "simulate_outcome": "FAILED"},
        )
        assert response.status_code == 201, response.text
    response = client.post(
        "/payments/",
        headers=headers,
        json={"booking_id": row.id, "simulate_outcome": "SUCCESS"},
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "PAYMENT_LIMIT"
    db.refresh(row)
    assert row.status == BookingStatus.FAILED
    assert db.scalar(select(func.count()).select_from(Payment)) == 3


@pytest.mark.parametrize("outcome", ["PENDING", "SUCCESS", "FAILED"])
def test_retry_returns_conflict_when_slot_is_taken(
    client: TestClient,
    db: Session,
    booking: tuple[Booking, User],
    auth_headers: Callable[[User], dict[str, str]],
    outcome: str,
) -> None:
    row, user = booking
    headers = auth_headers(user)
    response = client.post(
        "/payments/",
        headers=headers,
        json={"booking_id": row.id, "simulate_outcome": "FAILED"},
    )
    assert response.status_code == 201, response.text
    replacement = Booking(
        user_id=row.user_id,
        centre_id=row.centre_id,
        test_id=row.test_id,
        appointment_at=row.appointment_at,
        amount=row.amount,
    )
    db.add(replacement)
    db.commit()
    response = client.post(
        "/payments/",
        headers=headers,
        json={"booking_id": row.id, "simulate_outcome": outcome},
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "CONFLICT"
    db.refresh(row)
    assert row.status == BookingStatus.FAILED
    assert db.scalar(select(func.count()).select_from(Payment)) == 1


def test_unknown_booking_is_not_found(
    client: TestClient,
    booking: tuple[Booking, User],
    auth_headers: Callable[[User], dict[str, str]],
) -> None:
    _, user = booking
    response = client.post(
        "/payments/",
        headers=auth_headers(user),
        json={"booking_id": 999999, "simulate_outcome": "PENDING"},
    )
    assert response.status_code == 404, response.text


def test_payment_requires_authentication(
    client: TestClient, db: Session, booking: tuple[Booking, User]
) -> None:
    row, _ = booking
    response = client.post("/payments/", json={"booking_id": row.id})
    assert response.status_code == 401, response.text
    assert db.scalar(select(func.count()).select_from(Payment)) == 0


def test_cancelled_booking_rejects_payment(
    client: TestClient,
    db: Session,
    booking: tuple[Booking, User],
    auth_headers: Callable[[User], dict[str, str]],
) -> None:
    row, user = booking
    transition(row, BookingStatus.CANCELLED)
    db.commit()
    response = client.post(
        "/payments/",
        headers=auth_headers(user),
        json={"booking_id": row.id, "simulate_outcome": "PENDING"},
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "INVALID_STATE"
    assert db.scalar(select(func.count()).select_from(Payment)) == 0


def test_payment_logs_include_payment_and_booking_ids(
    client: TestClient,
    booking: tuple[Booking, User],
    auth_headers: Callable[[User], dict[str, str]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    row, user = booking
    caplog.set_level("INFO", logger="app.services.payments")
    response = client.post(
        "/payments/",
        headers=auth_headers(user),
        json={"booking_id": row.id, "simulate_outcome": "SUCCESS"},
    )
    assert response.status_code == 201, response.text
    records = [
        record
        for record in caplog.records
        if record.message in ("payment_created", "payment_resolved")
    ]
    assert {record.__dict__["event"] for record in records} == {
        "payment_created",
        "payment_resolved",
    }
    assert all(record.__dict__["booking_id"] == row.id for record in records)
    assert all(
        record.__dict__["payment_id"] == response.json()["id"] for record in records
    )


def test_invalid_outcome_is_rejected(
    client: TestClient,
    db: Session,
    booking: tuple[Booking, User],
    auth_headers: Callable[[User], dict[str, str]],
) -> None:
    row, user = booking
    response = client.post(
        "/payments/",
        headers=auth_headers(user),
        json={"booking_id": row.id, "simulate_outcome": "CANCELLED"},
    )
    assert response.status_code == 422, response.text
    assert db.scalar(select(func.count()).select_from(Payment)) == 0


@pytest.mark.parametrize("booking_id", [0, -1, 2_147_483_648])
def test_invalid_booking_id_is_rejected(
    client: TestClient,
    db: Session,
    booking: tuple[Booking, User],
    auth_headers: Callable[[User], dict[str, str]],
    booking_id: int,
) -> None:
    _, user = booking
    response = client.post(
        "/payments/", headers=auth_headers(user), json={"booking_id": booking_id}
    )
    assert response.status_code == 422, response.text
    assert db.scalar(select(func.count()).select_from(Payment)) == 0


def test_omitted_outcome_uses_simulator(
    client: TestClient,
    booking: tuple[Booking, User],
    auth_headers: Callable[[User], dict[str, str]],
) -> None:
    row, user = booking
    response = client.post(
        "/payments/", headers=auth_headers(user), json={"booking_id": row.id}
    )
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["status"] in ("SUCCESS", "FAILED")
    assert (
        data["booking_status"]
        == {
            "SUCCESS": "CONFIRMED",
            "FAILED": "FAILED",
        }[data["status"]]
    )


@pytest.mark.parametrize("retry", [False, True])
def test_payment_failure_rolls_back_every_change(
    client: TestClient,
    db: Session,
    booking: tuple[Booking, User],
    auth_headers: Callable[[User], dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
    retry: bool,
) -> None:
    from app.services import payments

    row, user = booking
    if retry:
        response = client.post(
            "/payments/",
            headers=auth_headers(user),
            json={"booking_id": row.id, "simulate_outcome": "FAILED"},
        )
        assert response.status_code == 201, response.text
    apply_outcome = payments.apply_payment_outcome

    def fail_after_outcome(
        booking: Booking, payment: Payment, outcome: PaymentStatus | str
    ) -> None:
        apply_outcome(booking, payment, outcome)
        raise RuntimeError("Simulated transaction failure")

    monkeypatch.setattr(payments, "apply_payment_outcome", fail_after_outcome)
    with TestClient(client.app, raise_server_exceptions=False) as request_client:
        response = request_client.post(
            "/payments/",
            headers=auth_headers(user),
            json={"booking_id": row.id, "simulate_outcome": "SUCCESS"},
        )
    assert response.status_code == 500, response.text
    db.refresh(row)
    assert row.status == (BookingStatus.FAILED if retry else BookingStatus.PENDING)
    assert db.scalar(select(func.count()).select_from(Payment)) == int(retry)


@pytest.mark.parametrize("retry", [False, True])
def test_concurrent_payments_create_only_one_payment(
    client: TestClient,
    db: Session,
    booking: tuple[Booking, User],
    auth_headers: Callable[[User], dict[str, str]],
    retry: bool,
) -> None:
    row, user = booking
    payload = {"booking_id": row.id, "simulate_outcome": "PENDING"}
    headers = auth_headers(user)
    if retry:
        response = client.post(
            "/payments/",
            headers=headers,
            json={"booking_id": row.id, "simulate_outcome": "FAILED"},
        )
        assert response.status_code == 201, response.text
    barrier = Barrier(6)

    def pay(_: int) -> Response:
        with TestClient(client.app) as request_client:
            barrier.wait(timeout=15)
            return request_client.post("/payments/", headers=headers, json=payload)

    with ThreadPoolExecutor(max_workers=6) as executor:
        responses = list(executor.map(pay, range(6)))

    assert sorted(response.status_code for response in responses) == [201] + [409] * 5
    assert all(
        response.json()["error"]["code"] == "PAYMENT_EXISTS"
        for response in responses
        if response.status_code == 409
    )
    assert db.scalar(select(func.count()).select_from(Payment)) == 1 + int(retry)
    db.refresh(row)
    assert row.status == BookingStatus.PENDING
