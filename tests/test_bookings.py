from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.enums import BookingStatus, PaymentStatus
from app.models import Booking, CentreTest, Payment, User
from app.state import transition


def booking_payload(offering: CentreTest, days: int = 1) -> dict[str, int | str]:
    return {
        "centre_id": offering.centre_id,
        "test_id": offering.test_id,
        "appointment_at": (datetime.now(UTC) + timedelta(days=days)).isoformat(),
    }


def create_booking(
    client: TestClient, headers: dict[str, str], offering: CentreTest, days: int = 1
) -> dict:
    response = client.post(
        "/bookings", json=booking_payload(offering, days), headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_booking_snapshots_price(
    client: TestClient,
    db: Session,
    make_user: Callable[..., User],
    auth_headers: Callable[[User], dict[str, str]],
    make_offering: Callable[..., CentreTest],
) -> None:
    user = make_user()
    headers = auth_headers(user)
    offering = make_offering(price=Decimal("123.45"))
    booking = create_booking(client, headers, offering)
    assert booking["status"] == "PENDING"
    assert booking["user_id"] == user.id
    assert Decimal(booking["amount"]) == Decimal("123.45")
    offering.price = Decimal("999.99")
    db.commit()
    response = client.get(f"/bookings/{booking['id']}", headers=headers)
    assert response.status_code == 200
    assert Decimal(response.json()["amount"]) == Decimal("123.45")


@pytest.mark.parametrize("appointment", ["past", "naive"])
def test_booking_requires_future_aware_datetime(
    client: TestClient,
    make_user: Callable[..., User],
    auth_headers: Callable[[User], dict[str, str]],
    make_offering: Callable[..., CentreTest],
    appointment: str,
) -> None:
    payload = booking_payload(make_offering())
    value = datetime.now(UTC)
    if appointment == "past":
        value -= timedelta(days=1)
    else:
        value = (value + timedelta(days=1)).replace(tzinfo=None)
    payload["appointment_at"] = value.isoformat()
    response = client.post("/bookings", json=payload, headers=auth_headers(make_user()))
    assert response.status_code == 422
    assert "error" in response.json()


def test_test_must_be_offered_at_selected_centre(
    client: TestClient,
    make_user: Callable[..., User],
    auth_headers: Callable[[User], dict[str, str]],
    make_offering: Callable[..., CentreTest],
) -> None:
    payload = booking_payload(make_offering())
    payload["test_id"] = make_offering().test_id
    response = client.post("/bookings", json=payload, headers=auth_headers(make_user()))
    assert response.status_code == 404


def test_offering_deleted_after_lookup_returns_not_found(
    client: TestClient,
    make_user: Callable[..., User],
    auth_headers: Callable[[User], dict[str, str]],
    make_offering: Callable[..., CentreTest],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    offering = make_offering()
    original_scalar = Session.scalar

    def delete_after_offering_lookup(session: Session, *args: object, **kwargs: object):
        result = original_scalar(session, *args, **kwargs)
        if isinstance(result, CentreTest):
            with Session(session.get_bind()) as deleting:
                deleting.execute(delete(CentreTest).where(CentreTest.id == result.id))
                deleting.commit()
        return result

    monkeypatch.setattr(Session, "scalar", delete_after_offering_lookup)
    response = client.post(
        "/bookings",
        json=booking_payload(offering),
        headers=auth_headers(make_user()),
    )
    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "NOT_FOUND",
            "message": "Test is not offered at this centre",
        }
    }


def test_user_deleted_after_lookup_returns_unauthorized(
    client: TestClient,
    make_user: Callable[..., User],
    auth_headers: Callable[[User], dict[str, str]],
    make_offering: Callable[..., CentreTest],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    offering = make_offering()
    user = make_user()
    headers = auth_headers(user)
    original_scalar = Session.scalar

    def delete_after_offering_lookup(session: Session, *args: object, **kwargs: object):
        result = original_scalar(session, *args, **kwargs)
        if isinstance(result, CentreTest):
            with Session(session.get_bind()) as deleting:
                deleting.execute(delete(User).where(User.id == user.id))
                deleting.commit()
        return result

    monkeypatch.setattr(Session, "scalar", delete_after_offering_lookup)
    response = client.post("/bookings", json=booking_payload(offering), headers=headers)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


def test_duplicate_active_slot_is_conflict(
    client: TestClient,
    make_user: Callable[..., User],
    auth_headers: Callable[[User], dict[str, str]],
    make_offering: Callable[..., CentreTest],
) -> None:
    headers = auth_headers(make_user())
    payload = booking_payload(make_offering())
    assert client.post("/bookings", json=payload, headers=headers).status_code == 201
    response = client.post("/bookings", json=payload, headers=headers)
    assert response.status_code == 409
    assert "error" in response.json()


def test_other_users_booking_is_hidden(
    client: TestClient,
    make_user: Callable[..., User],
    auth_headers: Callable[[User], dict[str, str]],
    make_offering: Callable[..., CentreTest],
) -> None:
    booking = create_booking(client, auth_headers(make_user()), make_offering())
    headers = auth_headers(make_user())
    assert client.get(f"/bookings/{booking['id']}", headers=headers).status_code == 404
    assert (
        client.post(f"/bookings/{booking['id']}/cancel", headers=headers).status_code
        == 404
    )
    page = client.get("/bookings", headers=headers).json()
    assert page["items"] == []
    assert page["total"] == 0


def test_list_is_paginated_and_status_filtered(
    client: TestClient,
    make_user: Callable[..., User],
    auth_headers: Callable[[User], dict[str, str]],
    make_offering: Callable[..., CentreTest],
) -> None:
    headers = auth_headers(make_user())
    offering = make_offering()
    first = create_booking(client, headers, offering, days=1)
    second = create_booking(client, headers, offering, days=2)
    assert (
        client.post(f"/bookings/{first['id']}/cancel", headers=headers).status_code
        == 200
    )
    page = client.get("/bookings?limit=1&offset=0", headers=headers).json()
    assert page["total"] == 2
    assert page["limit"] == 1
    assert page["offset"] == 0
    assert [item["id"] for item in page["items"]] == [second["id"]]
    page = client.get("/bookings?limit=1&offset=1", headers=headers).json()
    assert [item["id"] for item in page["items"]] == [first["id"]]
    page = client.get("/bookings?status=CANCELLED", headers=headers).json()
    assert page["total"] == 1
    assert [item["id"] for item in page["items"]] == [first["id"]]
    for query in ("limit=0", "limit=101", "offset=-1", "status=UNKNOWN"):
        assert client.get(f"/bookings?{query}", headers=headers).status_code == 422


def test_cancel_pending_booking_and_rebook_same_slot(
    client: TestClient,
    make_user: Callable[..., User],
    auth_headers: Callable[[User], dict[str, str]],
    make_offering: Callable[..., CentreTest],
) -> None:
    headers = auth_headers(make_user())
    payload = booking_payload(make_offering())
    booking = client.post("/bookings", json=payload, headers=headers).json()
    response = client.post(f"/bookings/{booking['id']}/cancel", headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == "CANCELLED"
    again = client.post(f"/bookings/{booking['id']}/cancel", headers=headers)
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "INVALID_STATE"
    assert client.post("/bookings", json=payload, headers=headers).status_code == 201


@pytest.mark.parametrize("status", [BookingStatus.CONFIRMED, BookingStatus.FAILED])
def test_terminal_booking_cannot_be_cancelled(
    client: TestClient,
    db: Session,
    make_user: Callable[..., User],
    auth_headers: Callable[[User], dict[str, str]],
    make_offering: Callable[..., CentreTest],
    status: BookingStatus,
) -> None:
    headers = auth_headers(make_user())
    booking = create_booking(client, headers, make_offering())
    stored = db.get(Booking, booking["id"])
    assert stored is not None
    transition(stored, status)
    db.commit()
    response = client.post(f"/bookings/{booking['id']}/cancel", headers=headers)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INVALID_STATE"


def test_pending_payment_blocks_cancellation(
    client: TestClient,
    db: Session,
    make_user: Callable[..., User],
    auth_headers: Callable[[User], dict[str, str]],
    make_offering: Callable[..., CentreTest],
) -> None:
    headers = auth_headers(make_user())
    booking = create_booking(client, headers, make_offering())
    db.add(
        Payment(
            booking_id=booking["id"],
            amount=Decimal(booking["amount"]),
            provider_ref=f"sim_{uuid4().hex}",
        )
    )
    db.commit()
    response = client.post(f"/bookings/{booking['id']}/cancel", headers=headers)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INVALID_STATE"
    assert (
        client.get(f"/bookings/{booking['id']}", headers=headers).json()["status"]
        == "PENDING"
    )


def test_failed_payment_does_not_block_cancellation(
    client: TestClient,
    db: Session,
    make_user: Callable[..., User],
    auth_headers: Callable[[User], dict[str, str]],
    make_offering: Callable[..., CentreTest],
) -> None:
    headers = auth_headers(make_user())
    booking = create_booking(client, headers, make_offering())
    payment = Payment(
        booking_id=booking["id"],
        amount=Decimal(booking["amount"]),
        provider_ref=f"sim_{uuid4().hex}",
    )
    db.add(payment)
    db.flush()
    transition(payment, PaymentStatus.FAILED)
    db.commit()
    response = client.post(f"/bookings/{booking['id']}/cancel", headers=headers)
    assert response.status_code == 200


def test_bookings_require_auth_and_missing_booking_is_not_found(
    client: TestClient,
    make_user: Callable[..., User],
    auth_headers: Callable[[User], dict[str, str]],
    make_offering: Callable[..., CentreTest],
) -> None:
    assert (
        client.post("/bookings", json=booking_payload(make_offering())).status_code
        == 401
    )
    assert client.get("/bookings").status_code == 401
    assert client.get("/bookings/99999").status_code == 401
    assert client.post("/bookings/99999/cancel").status_code == 401
    headers = auth_headers(make_user())
    assert client.get("/bookings/99999", headers=headers).status_code == 404
    assert client.post("/bookings/99999/cancel", headers=headers).status_code == 404


@pytest.mark.parametrize("booking_id", [0, -1, 2_147_483_648])
@pytest.mark.parametrize("action", ["get", "cancel"])
def test_booking_path_ids_must_fit_postgres_int4(
    client: TestClient,
    make_user: Callable[..., User],
    auth_headers: Callable[[User], dict[str, str]],
    booking_id: int,
    action: str,
) -> None:
    headers = auth_headers(make_user())
    if action == "get":
        response = client.get(f"/bookings/{booking_id}", headers=headers)
    else:
        response = client.post(f"/bookings/{booking_id}/cancel", headers=headers)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize("field", ["centre_id", "test_id"])
@pytest.mark.parametrize("invalid_id", [0, -1, 2_147_483_648])
def test_booking_body_ids_must_fit_postgres_int4(
    client: TestClient,
    make_user: Callable[..., User],
    auth_headers: Callable[[User], dict[str, str]],
    make_offering: Callable[..., CentreTest],
    field: str,
    invalid_id: int,
) -> None:
    payload = booking_payload(make_offering())
    payload[field] = invalid_id
    response = client.post("/bookings", json=payload, headers=auth_headers(make_user()))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize(
    ("age", "status", "payment_status", "expected"),
    [
        (31, BookingStatus.PENDING, None, BookingStatus.CANCELLED),
        (29, BookingStatus.PENDING, None, BookingStatus.PENDING),
        (30, BookingStatus.PENDING, None, BookingStatus.PENDING),
        (31, BookingStatus.PENDING, PaymentStatus.PENDING, BookingStatus.PENDING),
        (31, BookingStatus.PENDING, PaymentStatus.SUCCESS, BookingStatus.PENDING),
        (31, BookingStatus.PENDING, PaymentStatus.FAILED, BookingStatus.CANCELLED),
        (31, BookingStatus.CONFIRMED, None, BookingStatus.CONFIRMED),
        (31, BookingStatus.FAILED, None, BookingStatus.FAILED),
        (31, BookingStatus.CANCELLED, None, BookingStatus.CANCELLED),
    ],
)
def test_expire_stale_bookings(
    client: TestClient,
    db: Session,
    make_user: Callable[..., User],
    auth_headers: Callable[[User], dict[str, str]],
    make_offering: Callable[..., CentreTest],
    monkeypatch: pytest.MonkeyPatch,
    age: int,
    status: BookingStatus,
    payment_status: PaymentStatus | None,
    expected: BookingStatus,
) -> None:
    from app.services import bookings

    now = datetime.now(UTC)

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(bookings, "datetime", FixedDatetime)
    headers = auth_headers(make_user())
    payload = booking_payload(make_offering())
    booking_id = client.post("/bookings", json=payload, headers=headers).json()["id"]
    booking = db.get(Booking, booking_id)
    booking.created_at = now - timedelta(minutes=age)
    if status != BookingStatus.PENDING:
        transition(booking, status)
    if payment_status is not None:
        db.add(
            Payment(
                booking_id=booking.id,
                amount=booking.amount,
                provider_ref=f"sim_{uuid4().hex}",
                status=payment_status,
            )
        )
    db.commit()
    count = bookings.expire_stale_bookings(db, timedelta(minutes=30))
    assert count == int(expected == BookingStatus.CANCELLED and status != expected)
    db.commit()
    db.refresh(booking)
    assert booking.status == expected
    assert bookings.expire_stale_bookings(db, timedelta(minutes=30)) == 0
    db.commit()
    if expected == BookingStatus.CANCELLED:
        assert (
            client.post("/bookings", json=payload, headers=headers).status_code == 201
        )


def test_expiry_skips_locked_bookings(
    client: TestClient,
    db: Session,
    make_user: Callable[..., User],
    auth_headers: Callable[[User], dict[str, str]],
    make_offering: Callable[..., CentreTest],
) -> None:
    from app.services.bookings import expire_stale_bookings

    created = create_booking(client, auth_headers(make_user()), make_offering())
    booking = db.get(Booking, created["id"])
    booking.created_at = datetime.now(UTC) - timedelta(hours=1)
    db.commit()
    with Session(db.get_bind()) as other:
        other.get(Booking, booking.id, with_for_update=True)
        assert expire_stale_bookings(db, timedelta(minutes=30)) == 0
        db.commit()
    assert expire_stale_bookings(db, timedelta(minutes=30)) == 1
    db.rollback()
    db.refresh(booking)
    assert booking.status == BookingStatus.PENDING


@pytest.mark.parametrize("minutes", [0, -1])
def test_expiry_rejects_nonpositive_age(db: Session, minutes: int) -> None:
    from app.services.bookings import expire_stale_bookings

    with pytest.raises(ValueError, match="must be positive"):
        expire_stale_bookings(db, timedelta(minutes=minutes))


def test_expiry_with_no_bookings(db: Session) -> None:
    from app.services.bookings import expire_stale_bookings

    assert expire_stale_bookings(db, timedelta(minutes=30)) == 0


def test_expiry_skips_booking_with_locked_payment(
    client: TestClient,
    db: Session,
    make_user: Callable[..., User],
    auth_headers: Callable[[User], dict[str, str]],
    make_offering: Callable[..., CentreTest],
) -> None:
    from sqlalchemy.orm import Session as SASession

    from app.services.bookings import expire_stale_bookings

    created = create_booking(client, auth_headers(make_user()), make_offering())
    booking = db.get(Booking, created["id"])
    assert booking is not None
    booking.created_at = datetime.now(UTC) - timedelta(hours=1)
    payment = Payment(
        booking_id=booking.id,
        amount=booking.amount,
        provider_ref=f"sim_{uuid4().hex}",
    )
    db.add(payment)
    db.commit()
    with SASession(db.get_bind()) as other:
        other.get(Payment, payment.id, with_for_update=True)
        assert expire_stale_bookings(db, timedelta(minutes=30)) == 0
        db.commit()
    db.refresh(booking)
    assert booking.status == BookingStatus.PENDING
    assert expire_stale_bookings(db, timedelta(minutes=30)) == 0
    db.rollback()
