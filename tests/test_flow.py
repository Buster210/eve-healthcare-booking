import hashlib
import hmac
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.enums import BookingStatus, PaymentStatus
from app.models import CentreTest, Payment, WebhookEvent


def register_and_login(client: TestClient, email: str) -> dict[str, str]:
    signup = client.post(
        "/auth/signup",
        json={
            "email": email,
            "password": "secure-test-password",
            "full_name": "Flow Tester",
        },
    )
    assert signup.status_code == 201, signup.text

    login = client.post(
        "/auth/login", json={"email": email, "password": "secure-test-password"}
    )
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def find_offering(
    client: TestClient, make_offering: Callable[..., CentreTest]
) -> tuple[int, int]:
    make_offering()
    centres = client.get("/centres")
    assert centres.status_code == 200, centres.text
    assert centres.json()["items"]
    centre_id = centres.json()["items"][0]["id"]

    detail = client.get(f"/centres/{centre_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["tests"]
    return centre_id, detail.json()["tests"][0]["id"]


def create_booking(
    client: TestClient, headers: dict[str, str], centre_id: int, test_id: int
) -> dict:
    response = client.post(
        "/bookings",
        headers=headers,
        json={
            "centre_id": centre_id,
            "test_id": test_id,
            "appointment_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def signed_webhook(payload: dict[str, str]) -> tuple[bytes, dict[str, str]]:
    body = json.dumps(payload).encode()
    signature = hmac.new(
        get_settings().webhook_secret.get_secret_value().encode(),
        body,
        hashlib.sha256,
    ).hexdigest()
    return body, {"X-Signature": signature, "Content-Type": "application/json"}


def test_signup_centre_booking_success_and_confirmed_booking_cannot_be_cancelled(
    client: TestClient, make_offering: Callable[..., CentreTest]
) -> None:
    headers = register_and_login(client, "success@example.com")
    centre_id, test_id = find_offering(client, make_offering)
    booking = create_booking(client, headers, centre_id, test_id)

    payment = client.post(
        "/payments/",
        headers=headers,
        json={"booking_id": booking["id"], "simulate_outcome": "SUCCESS"},
    )
    assert payment.status_code == 201, payment.text
    assert payment.json()["status"] == PaymentStatus.SUCCESS
    assert payment.json()["booking_status"] == BookingStatus.CONFIRMED

    stored = client.get(f"/bookings/{booking['id']}", headers=headers)
    assert stored.status_code == 200, stored.text
    assert stored.json()["status"] == BookingStatus.CONFIRMED

    cancelled = client.post(f"/bookings/{booking['id']}/cancel", headers=headers)
    assert cancelled.status_code == 409, cancelled.text
    assert cancelled.json()["error"]["code"] == "INVALID_STATE"


def test_failed_payment_can_be_retried_on_the_same_booking(
    client: TestClient, make_offering: Callable[..., CentreTest]
) -> None:
    headers = register_and_login(client, "failed@example.com")
    centre_id, test_id = find_offering(client, make_offering)
    booking = create_booking(client, headers, centre_id, test_id)

    payment = client.post(
        "/payments/",
        headers=headers,
        json={"booking_id": booking["id"], "simulate_outcome": "FAILED"},
    )
    assert payment.status_code == 201, payment.text
    assert payment.json()["status"] == PaymentStatus.FAILED
    assert payment.json()["booking_status"] == BookingStatus.FAILED

    stored = client.get(f"/bookings/{booking['id']}", headers=headers)
    assert stored.status_code == 200, stored.text
    assert stored.json()["status"] == BookingStatus.FAILED

    retry = client.post(
        "/payments/",
        headers=headers,
        json={"booking_id": booking["id"], "simulate_outcome": "SUCCESS"},
    )
    assert retry.status_code == 201, retry.text
    assert retry.json()["id"] != payment.json()["id"]
    assert retry.json()["booking_id"] == booking["id"]
    assert retry.json()["status"] == PaymentStatus.SUCCESS
    assert retry.json()["booking_status"] == BookingStatus.CONFIRMED
    stored = client.get(f"/bookings/{booking['id']}", headers=headers)
    assert stored.json()["status"] == BookingStatus.CONFIRMED


def test_pending_payment_webhook_resolves_once_and_rejects_bad_signature(
    client: TestClient, db: Session, make_offering: Callable[..., CentreTest]
) -> None:
    headers = register_and_login(client, "webhook@example.com")
    centre_id, test_id = find_offering(client, make_offering)
    booking = create_booking(client, headers, centre_id, test_id)
    payment_response = client.post(
        "/payments/",
        headers=headers,
        json={"booking_id": booking["id"], "simulate_outcome": "PENDING"},
    )
    assert payment_response.status_code == 201, payment_response.text
    payment_data = payment_response.json()
    assert payment_data["status"] == PaymentStatus.PENDING
    assert payment_data["booking_status"] == BookingStatus.PENDING

    payload = {
        "event_id": "flow-event-1",
        "provider_ref": payment_data["provider_ref"],
        "status": PaymentStatus.SUCCESS,
    }
    body, headers_for_webhook = signed_webhook(payload)
    bad_signature = client.post(
        "/payments/webhook/",
        content=body,
        headers={"X-Signature": "bad", "Content-Type": "application/json"},
    )
    assert bad_signature.status_code == 401, bad_signature.text

    processed = client.post(
        "/payments/webhook/", content=body, headers=headers_for_webhook
    )
    assert processed.status_code == 200, processed.text
    assert processed.json() == {"result": "processed"}

    replayed = client.post(
        "/payments/webhook/", content=body, headers=headers_for_webhook
    )
    assert replayed.status_code == 200, replayed.text
    assert replayed.json() == {"result": "duplicate"}

    stored_booking = client.get(f"/bookings/{booking['id']}", headers=headers)
    assert stored_booking.status_code == 200, stored_booking.text
    assert stored_booking.json()["status"] == BookingStatus.CONFIRMED
    db.expire_all()
    stored_payment = db.get(Payment, payment_data["id"])
    assert stored_payment is not None
    assert stored_payment.status == PaymentStatus.SUCCESS
    assert db.scalar(select(func.count()).select_from(WebhookEvent)) == 1


def test_booking_routes_require_auth_and_hide_another_users_booking(
    client: TestClient, make_offering: Callable[..., CentreTest]
) -> None:
    owner_headers = register_and_login(client, "owner@example.com")
    other_headers = register_and_login(client, "other@example.com")
    centre_id, test_id = find_offering(client, make_offering)
    booking = create_booking(client, owner_headers, centre_id, test_id)

    assert client.get(f"/bookings/{booking['id']}").status_code == 401
    assert (
        client.post("/payments/", json={"booking_id": booking["id"]}).status_code == 401
    )

    hidden = client.get(f"/bookings/{booking['id']}", headers=other_headers)
    assert hidden.status_code == 404, hidden.text
    hidden_cancel = client.post(
        f"/bookings/{booking['id']}/cancel", headers=other_headers
    )
    assert hidden_cancel.status_code == 404, hidden_cancel.text
    hidden_payment = client.post(
        "/payments/",
        headers=other_headers,
        json={"booking_id": booking["id"], "simulate_outcome": "SUCCESS"},
    )
    assert hidden_payment.status_code == 404, hidden_payment.text
