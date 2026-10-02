import os
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Booking, Centre, CentreTest, DiagnosticTest, User
from app.security import verify_password
from scripts.seed import CENTRES, TESTS


def _admin_headers(
    make_user: Callable[..., User], auth_headers: Callable[[User], dict[str, str]]
) -> dict[str, str]:
    return auth_headers(make_user(is_admin=True))


def test_list_tests_is_public(
    client: TestClient,
    make_user: Callable[..., User],
    auth_headers: Callable[[User], dict[str, str]],
) -> None:
    headers = _admin_headers(make_user, auth_headers)
    client.post("/tests", json={"name": "CBC"}, headers=headers)
    response = client.get("/tests")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["name"] == "CBC"


def test_test_writes_need_admin(
    client: TestClient,
    make_user: Callable[..., User],
    auth_headers: Callable[[User], dict[str, str]],
) -> None:
    admin_headers = _admin_headers(make_user, auth_headers)
    created = client.post("/tests", json={"name": "CBC"}, headers=admin_headers).json()

    assert client.post("/tests", json={"name": "X"}).status_code == 401
    user_headers = auth_headers(make_user())
    assert (
        client.post("/tests", json={"name": "X"}, headers=user_headers).status_code
        == 403
    )
    assert (
        client.patch(f"/tests/{created['id']}", json={"name": "Y"}).status_code == 401
    )
    assert (
        client.patch(
            f"/tests/{created['id']}", json={"name": "Y"}, headers=user_headers
        ).status_code
        == 403
    )
    assert client.delete(f"/tests/{created['id']}").status_code == 401
    assert (
        client.delete(f"/tests/{created['id']}", headers=user_headers).status_code
        == 403
    )


def test_create_update_test(
    client: TestClient,
    make_user: Callable[..., User],
    auth_headers: Callable[[User], dict[str, str]],
) -> None:
    headers = _admin_headers(make_user, auth_headers)
    created = client.post(
        "/tests",
        json={"name": "HbA1c", "description": "Sugar average"},
        headers=headers,
    )
    assert created.status_code == 201
    body = created.json()
    assert body["description"] == "Sugar average"

    updated = client.patch(
        f"/tests/{body['id']}",
        json={"description": "Glycated haemoglobin"},
        headers=headers,
    )
    assert updated.status_code == 200
    assert updated.json()["description"] == "Glycated haemoglobin"


def test_duplicate_test_returns_409(
    client: TestClient,
    make_user: Callable[..., User],
    auth_headers: Callable[[User], dict[str, str]],
) -> None:
    headers = _admin_headers(make_user, auth_headers)
    assert (
        client.post("/tests", json={"name": "CBC"}, headers=headers).status_code == 201
    )
    assert (
        client.post("/tests", json={"name": "CBC"}, headers=headers).status_code == 409
    )

    other = client.post("/tests", json={"name": "XRay"}, headers=headers).json()
    conflict = client.patch(
        f"/tests/{other['id']}", json={"name": "CBC"}, headers=headers
    )
    assert conflict.status_code == 409


def test_test_pagination(
    client: TestClient,
    make_user: Callable[..., User],
    auth_headers: Callable[[User], dict[str, str]],
) -> None:
    headers = _admin_headers(make_user, auth_headers)
    for i in range(4):
        client.post("/tests", json={"name": f"Test-{i}"}, headers=headers)
    page = client.get("/tests", params={"limit": 3, "offset": 1}).json()
    assert page["total"] == 4
    assert [t["name"] for t in page["items"]] == ["Test-1", "Test-2", "Test-3"]


def test_delete_offered_test_returns_409(
    client: TestClient,
    make_user: Callable[..., User],
    auth_headers: Callable[[User], dict[str, str]],
    make_offering: Callable[..., CentreTest],
) -> None:
    offering = make_offering()
    headers = _admin_headers(make_user, auth_headers)
    assert (
        client.delete(f"/tests/{offering.test_id}", headers=headers).status_code == 409
    )


def test_delete_booked_test_returns_409(
    client: TestClient,
    db: Session,
    make_user: Callable[..., User],
    auth_headers: Callable[[User], dict[str, str]],
    make_offering: Callable[..., CentreTest],
) -> None:
    offering = make_offering()
    cid, tid, price = offering.centre_id, offering.test_id, offering.price
    user = make_user()
    db.add(
        Booking(
            user_id=user.id,
            centre_id=cid,
            test_id=tid,
            appointment_at=datetime.now(UTC) + timedelta(days=1),
            amount=price,
            status="PENDING",
        )
    )
    db.commit()

    headers = _admin_headers(make_user, auth_headers)
    assert client.delete(f"/tests/{tid}", headers=headers).status_code == 409


def test_delete_unused_test(
    client: TestClient,
    make_user: Callable[..., User],
    auth_headers: Callable[[User], dict[str, str]],
) -> None:
    headers = _admin_headers(make_user, auth_headers)
    created = client.post("/tests", json={"name": "Temp"}, headers=headers).json()
    assert client.delete(f"/tests/{created['id']}", headers=headers).status_code == 204
    assert client.delete(f"/tests/{created['id']}", headers=headers).status_code == 404
    assert client.delete("/tests/999999", headers=headers).status_code == 404


def test_update_unknown_test_404(
    client: TestClient,
    make_user: Callable[..., User],
    auth_headers: Callable[[User], dict[str, str]],
) -> None:
    headers = _admin_headers(make_user, auth_headers)
    assert (
        client.patch("/tests/999999", json={"name": "X"}, headers=headers).status_code
        == 404
    )


@pytest.mark.parametrize("invalid_id", [0, -1, 2_147_483_648])
def test_test_path_ids_must_fit_postgres_int4(
    client: TestClient,
    make_user: Callable[..., User],
    auth_headers: Callable[[User], dict[str, str]],
    invalid_id: int,
) -> None:
    headers = _admin_headers(make_user, auth_headers)
    for method, url, json in [
        ("PATCH", f"/tests/{invalid_id}", {"name": "Updated"}),
        ("DELETE", f"/tests/{invalid_id}", None),
    ]:
        response = client.request(method, url, json=json, headers=headers)
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_offering_price_must_be_positive(
    client: TestClient,
    make_user: Callable[..., User],
    auth_headers: Callable[[User], dict[str, str]],
    make_offering: Callable[..., CentreTest],
) -> None:
    offering = make_offering()
    headers = _admin_headers(make_user, auth_headers)
    response = client.put(
        f"/centres/{offering.centre_id}/tests/{offering.test_id}",
        json={"price": "-10.00"},
        headers=headers,
    )
    assert response.status_code == 422
    assert Decimal(offering.price) > 0


@pytest.mark.parametrize("payload", [{}, {"name": None}])
def test_test_patch_rejects_empty_or_null_name(
    client: TestClient,
    make_user: Callable[..., User],
    auth_headers: Callable[[User], dict[str, str]],
    make_offering: Callable[..., CentreTest],
    payload: dict[str, None],
) -> None:
    offering = make_offering()
    headers = _admin_headers(make_user, auth_headers)
    response = client.patch(f"/tests/{offering.test_id}", json=payload, headers=headers)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_test_description_can_be_cleared(
    client: TestClient,
    make_user: Callable[..., User],
    auth_headers: Callable[[User], dict[str, str]],
    make_offering: Callable[..., CentreTest],
) -> None:
    offering = make_offering()
    headers = _admin_headers(make_user, auth_headers)
    response = client.patch(
        f"/tests/{offering.test_id}", json={"description": None}, headers=headers
    )
    assert response.status_code == 200
    assert response.json()["description"] is None


@pytest.mark.parametrize(
    "price", ["0", "0.001", "100000000", "1.234", "NaN", "Infinity"]
)
def test_offering_rejects_prices_outside_database_limits(
    client: TestClient,
    make_user: Callable[..., User],
    auth_headers: Callable[[User], dict[str, str]],
    make_offering: Callable[..., CentreTest],
    price: str,
) -> None:
    offering = make_offering()
    headers = _admin_headers(make_user, auth_headers)
    response = client.put(
        f"/centres/{offering.centre_id}/tests/{offering.test_id}",
        json={"price": price},
        headers=headers,
    )
    assert response.status_code == 422
    assert (
        client.get(f"/centres/{offering.centre_id}").json()["tests"][0]["price"]
        == "500.00"
    )


def test_offering_accepts_maximum_database_price(
    client: TestClient,
    make_user: Callable[..., User],
    auth_headers: Callable[[User], dict[str, str]],
    make_offering: Callable[..., CentreTest],
) -> None:
    offering = make_offering()
    headers = _admin_headers(make_user, auth_headers)
    response = client.put(
        f"/centres/{offering.centre_id}/tests/{offering.test_id}",
        json={"price": "99999999.99"},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["price"] == "99999999.99"


def test_seed_module_is_idempotent_and_preserves_existing_data(db: Session) -> None:
    unrelated = Centre(name=CENTRES[0][0], location="Hyderabad")
    db.add(unrelated)
    db.commit()
    env = {
        **os.environ,
        "DATABASE_URL": os.environ["TEST_DATABASE_URL"],
        "ADMIN_EMAIL": "Seed-Admin@example.com",
        "ADMIN_PASSWORD": "seed-test-password",
    }
    seeded = subprocess.run(
        [sys.executable, "-m", "scripts.seed"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    assert "prices in INR" in seeded.stdout
    admin = db.scalar(select(User))
    assert admin is not None
    assert admin.is_admin
    assert admin.email == "seed-admin@example.com"
    assert verify_password("seed-test-password", admin.password_hash)
    password_hash = admin.password_hash
    offering = db.scalar(select(CentreTest).order_by(CentreTest.id))
    assert offering is not None
    assert offering.price == Decimal(TESTS[0][2])
    offering.price = Decimal("123.45")
    db.commit()
    env["ADMIN_PASSWORD"] = "changed-test-password"
    subprocess.run(
        [sys.executable, "-m", "scripts.seed"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(User)) == 1
    assert db.scalar(select(func.count()).select_from(Centre)) == len(CENTRES) + 1
    assert db.scalar(select(func.count()).select_from(DiagnosticTest)) == len(TESTS)
    assert db.scalar(select(func.count()).select_from(CentreTest)) == len(
        CENTRES
    ) * len(TESTS)
    assert admin.password_hash == password_hash
    assert offering.price == Decimal("123.45")
    assert (
        db.scalar(
            select(func.count())
            .select_from(CentreTest)
            .where(CentreTest.centre_id == unrelated.id)
        )
        == 0
    )


def test_seed_promotes_existing_user_without_resetting_password(
    db: Session, make_user: Callable[..., User], monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts import seed

    user = make_user(email="seed-admin@example.com")
    password_hash = user.password_hash
    monkeypatch.setenv("ADMIN_EMAIL", "seed-admin@example.com")
    monkeypatch.setenv("ADMIN_PASSWORD", "seed-test-password")
    monkeypatch.setattr(seed, "SessionLocal", lambda: Session(db.get_bind()))
    seed.main()
    db.refresh(user)
    assert user.is_admin
    assert user.password_hash == password_hash


def test_seed_requires_admin_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts import seed

    monkeypatch.delenv("ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    with pytest.raises(SystemExit, match="ADMIN_EMAIL and ADMIN_PASSWORD"):
        seed.main()
