from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Insert, Select, delete, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.models import Booking, Centre, CentreTest, User
from app.services import catalogue


def _admin_headers(
    make_user: Callable[..., User], auth_headers: Callable[[User], dict[str, str]]
) -> dict[str, str]:
    return auth_headers(make_user(is_admin=True))


def test_list_centres_is_public(
    client: TestClient,
    make_user: Callable[..., User],
    auth_headers: Callable[[User], dict[str, str]],
) -> None:
    headers = _admin_headers(make_user, auth_headers)
    client.post(
        "/centres", json={"name": "CityCare", "location": "Mumbai"}, headers=headers
    )
    response = client.get("/centres")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["limit"] == 20
    assert body["offset"] == 0
    assert body["items"][0]["name"] == "CityCare"


def test_centre_writes_need_admin(
    client: TestClient,
    make_user: Callable[..., User],
    auth_headers: Callable[[User], dict[str, str]],
) -> None:
    admin_headers = _admin_headers(make_user, auth_headers)
    created = client.post(
        "/centres",
        json={"name": "CityCare", "location": "Mumbai"},
        headers=admin_headers,
    ).json()

    payload = {"name": "Other", "location": "Pune"}
    assert client.post("/centres", json=payload).status_code == 401
    user_headers = auth_headers(make_user())
    assert (
        client.post("/centres", json=payload, headers=user_headers).status_code == 403
    )

    assert (
        client.patch(f"/centres/{created['id']}", json={"name": "X"}).status_code == 401
    )
    assert (
        client.patch(
            f"/centres/{created['id']}", json={"name": "X"}, headers=user_headers
        ).status_code
        == 403
    )
    assert client.delete(f"/centres/{created['id']}").status_code == 401
    assert (
        client.delete(f"/centres/{created['id']}", headers=user_headers).status_code
        == 403
    )


def test_create_and_get_centre_with_tests(
    client: TestClient,
    make_user: Callable[..., User],
    auth_headers: Callable[[User], dict[str, str]],
    make_offering: Callable[..., CentreTest],
) -> None:
    offering = make_offering(price=Decimal("450.00"))
    cid, tid = offering.centre_id, offering.test_id
    response = client.get(f"/centres/{cid}")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == cid
    assert len(body["tests"]) == 1
    assert body["tests"][0]["id"] == tid
    assert body["tests"][0]["price"] == "450.00"

    headers = _admin_headers(make_user, auth_headers)
    created = client.post(
        "/centres", json={"name": "NewCare", "location": "Delhi"}, headers=headers
    )
    assert created.status_code == 201
    assert created.json()["name"] == "NewCare"


def test_duplicate_centre_returns_409(
    client: TestClient,
    make_user: Callable[..., User],
    auth_headers: Callable[[User], dict[str, str]],
) -> None:
    headers = _admin_headers(make_user, auth_headers)
    payload = {"name": "CityCare", "location": "Mumbai"}
    assert client.post("/centres", json=payload, headers=headers).status_code == 201
    response = client.post("/centres", json=payload, headers=headers)
    assert response.status_code == 409


def test_update_centre_and_duplicate_conflict(
    client: TestClient,
    make_user: Callable[..., User],
    auth_headers: Callable[[User], dict[str, str]],
) -> None:
    headers = _admin_headers(make_user, auth_headers)
    first = client.post(
        "/centres", json={"name": "Alpha", "location": "Mumbai"}, headers=headers
    ).json()
    client.post("/centres", json={"name": "Beta", "location": "Pune"}, headers=headers)

    updated = client.patch(
        f"/centres/{first['id']}", json={"location": "Thane"}, headers=headers
    )
    assert updated.status_code == 200
    assert updated.json()["location"] == "Thane"

    conflict = client.patch(
        f"/centres/{first['id']}",
        json={"name": "Beta", "location": "Pune"},
        headers=headers,
    )
    assert conflict.status_code == 409
    assert (
        client.patch("/centres/999999", json={"name": "X"}, headers=headers).status_code
        == 404
    )


def test_centre_pagination_and_search(
    client: TestClient,
    make_user: Callable[..., User],
    auth_headers: Callable[[User], dict[str, str]],
) -> None:
    headers = _admin_headers(make_user, auth_headers)
    for i in range(5):
        client.post(
            "/centres",
            json={"name": f"Centre-{i}", "location": "Mumbai" if i < 3 else "Pune"},
            headers=headers,
        )
    page = client.get("/centres", params={"limit": 2, "offset": 2}).json()
    assert page["total"] == 5
    assert page["limit"] == 2
    assert page["offset"] == 2
    assert [c["name"] for c in page["items"]] == ["Centre-2", "Centre-3"]

    found = client.get("/centres", params={"q": "pune"}).json()
    assert found["total"] == 2
    named = client.get("/centres", params={"q": "CENTRE-1"}).json()
    assert named["total"] == 1
    assert named["items"][0]["name"] == "Centre-1"
    missing = client.get("/centres", params={"q": "nowhere"}).json()
    assert missing["total"] == 0


def test_unknown_centre_returns_404(client: TestClient) -> None:
    assert client.get("/centres/999999").status_code == 404


@pytest.mark.parametrize("invalid_id", [0, -1, 2_147_483_648])
def test_centre_path_ids_must_fit_postgres_int4(
    client: TestClient,
    make_user: Callable[..., User],
    auth_headers: Callable[[User], dict[str, str]],
    invalid_id: int,
) -> None:
    headers = _admin_headers(make_user, auth_headers)
    requests = [
        ("GET", f"/centres/{invalid_id}", None),
        ("PATCH", f"/centres/{invalid_id}", {"name": "Updated"}),
        ("DELETE", f"/centres/{invalid_id}", None),
        ("PUT", f"/centres/{invalid_id}/tests/1", {"price": "10.00"}),
        ("DELETE", f"/centres/{invalid_id}/tests/1", None),
        ("PUT", f"/centres/1/tests/{invalid_id}", {"price": "10.00"}),
        ("DELETE", f"/centres/1/tests/{invalid_id}", None),
    ]
    for method, url, json in requests:
        response = client.request(method, url, json=json, headers=headers)
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_delete_centre_in_use_returns_409(
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
    assert client.delete(f"/centres/{cid}", headers=headers).status_code == 409
    assert (
        client.delete(f"/centres/{cid}/tests/{tid}", headers=headers).status_code == 409
    )
    assert client.delete("/centres/999999", headers=headers).status_code == 404
    assert client.get(f"/centres/{cid}").json()["tests"][0]["id"] == tid


def test_delete_centre_with_offerings_returns_409(
    client: TestClient,
    make_user: Callable[..., User],
    auth_headers: Callable[[User], dict[str, str]],
    make_offering: Callable[..., CentreTest],
) -> None:
    offering = make_offering()
    headers = _admin_headers(make_user, auth_headers)
    assert (
        client.delete(f"/centres/{offering.centre_id}", headers=headers).status_code
        == 409
    )
    assert client.get(f"/centres/{offering.centre_id}").status_code == 200
    assert (
        client.delete(
            f"/centres/{offering.centre_id}/tests/{offering.test_id}",
            headers=headers,
        ).status_code
        == 204
    )
    assert (
        client.delete(f"/centres/{offering.centre_id}", headers=headers).status_code
        == 204
    )
    assert client.get(f"/centres/{offering.centre_id}").status_code == 404
    assert (
        client.delete(f"/tests/{offering.test_id}", headers=headers).status_code == 204
    )


def test_delete_centre_blocks_offering_insert_after_empty_check(
    db: Session,
    make_offering: Callable[..., CentreTest],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    offering = make_offering()
    cid, tid = offering.centre_id, offering.test_id
    db.delete(offering)
    db.commit()

    with Session(db.get_bind()) as deleting:
        scalar = deleting.scalar

        def interleave(statement: Select[tuple[int]]) -> int | None:
            result = scalar(statement)
            assert result is None
            with Session(db.get_bind()) as inserting:
                inserting.execute(text("SET LOCAL lock_timeout = '200ms'"))
                inserting.add(CentreTest(centre_id=cid, test_id=tid, price=10))
                with pytest.raises(OperationalError) as error:
                    inserting.commit()
                assert error.value.orig.sqlstate == "55P03"
            return result

        monkeypatch.setattr(deleting, "scalar", interleave)
        catalogue.delete_centre(deleting, cid)

    assert db.scalar(select(Centre.id).where(Centre.id == cid)) is None


def test_upsert_offering_blocks_centre_delete_after_validation(
    db: Session,
    make_offering: Callable[..., CentreTest],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    offering = make_offering()
    cid, tid = offering.centre_id, offering.test_id
    db.delete(offering)
    db.commit()

    with Session(db.get_bind()) as inserting:
        scalar = inserting.scalar

        def interleave(statement: Insert) -> CentreTest | None:
            with Session(db.get_bind()) as deleting:
                deleting.execute(text("SET LOCAL lock_timeout = '200ms'"))
                with pytest.raises(OperationalError) as error:
                    deleting.execute(delete(Centre).where(Centre.id == cid))
                assert error.value.orig.sqlstate == "55P03"
            return scalar(statement)

        monkeypatch.setattr(inserting, "scalar", interleave)
        catalogue.upsert_offering(inserting, cid, tid, Decimal("10.00"))

    assert db.scalar(select(CentreTest.id).where(CentreTest.centre_id == cid))


def test_upsert_and_delete_offering(
    client: TestClient,
    make_user: Callable[..., User],
    auth_headers: Callable[[User], dict[str, str]],
    make_offering: Callable[..., CentreTest],
) -> None:
    offering = make_offering(price=Decimal("500.00"))
    cid, tid = offering.centre_id, offering.test_id
    headers = _admin_headers(make_user, auth_headers)

    changed = client.put(
        f"/centres/{cid}/tests/{tid}", json={"price": "650.00"}, headers=headers
    )
    assert changed.status_code == 200
    assert changed.json()["price"] == "650.00"
    assert client.put(f"/centres/{cid}/tests/{tid}").status_code == 401

    detail = client.get(f"/centres/{cid}").json()
    assert detail["tests"][0]["price"] == "650.00"

    assert (
        client.delete(f"/centres/{cid}/tests/{tid}", headers=headers).status_code == 204
    )
    assert client.get(f"/centres/{cid}").json()["tests"] == []
    assert (
        client.delete(f"/centres/{cid}/tests/{tid}", headers=headers).status_code == 404
    )


def test_upsert_offering_unknown_ids_404(
    client: TestClient,
    make_user: Callable[..., User],
    auth_headers: Callable[[User], dict[str, str]],
    make_offering: Callable[..., CentreTest],
) -> None:
    offering = make_offering()
    headers = _admin_headers(make_user, auth_headers)
    assert (
        client.put(
            "/centres/999999/tests/1", json={"price": "100.00"}, headers=headers
        ).status_code
        == 404
    )
    assert (
        client.put(
            f"/centres/{offering.centre_id}/tests/999999",
            json={"price": "100.00"},
            headers=headers,
        ).status_code
        == 404
    )


def test_offering_writes_require_admin(
    client: TestClient,
    make_user: Callable[..., User],
    auth_headers: Callable[[User], dict[str, str]],
    make_offering: Callable[..., CentreTest],
) -> None:
    offering = make_offering()
    path = f"/centres/{offering.centre_id}/tests/{offering.test_id}"
    user_headers = auth_headers(make_user())
    assert client.put(path, json={"price": "700.00"}).status_code == 401
    assert (
        client.put(path, json={"price": "700.00"}, headers=user_headers).status_code
        == 403
    )
    assert client.delete(path).status_code == 401
    assert client.delete(path, headers=user_headers).status_code == 403
    assert (
        client.get(f"/centres/{offering.centre_id}").json()["tests"][0]["price"]
        == "500.00"
    )


def test_upsert_creates_missing_offering(
    client: TestClient,
    make_user: Callable[..., User],
    auth_headers: Callable[[User], dict[str, str]],
) -> None:
    headers = _admin_headers(make_user, auth_headers)
    centre = client.post(
        "/centres", json={"name": "CityCare", "location": "Mumbai"}, headers=headers
    ).json()
    test = client.post("/tests", json={"name": "CBC"}, headers=headers).json()
    response = client.put(
        f"/centres/{centre['id']}/tests/{test['id']}",
        json={"price": "350.00"},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json() == {
        "centre_id": centre["id"],
        "test_id": test["id"],
        "price": "350.00",
    }
    assert len(client.get(f"/centres/{centre['id']}").json()["tests"]) == 1


@pytest.mark.parametrize("payload", [{}, {"name": None}, {"location": None}])
def test_centre_patch_rejects_empty_or_null_fields(
    client: TestClient,
    make_user: Callable[..., User],
    auth_headers: Callable[[User], dict[str, str]],
    make_offering: Callable[..., CentreTest],
    payload: dict[str, None],
) -> None:
    offering = make_offering()
    headers = _admin_headers(make_user, auth_headers)
    path = f"/centres/{offering.centre_id}"
    before = client.get(path).json()
    response = client.patch(path, json=payload, headers=headers)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert client.get(path).json() == before


@pytest.mark.parametrize("path", ["/centres", "/tests"])
@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"offset": -1}])
def test_catalogue_pagination_rejects_invalid_bounds(
    client: TestClient, path: str, params: dict[str, int]
) -> None:
    response = client.get(path, params=params)
    assert response.status_code == 422


@pytest.mark.parametrize("path", ["/centres", "/tests"])
def test_catalogue_pagination_beyond_last_item_returns_empty_page(
    client: TestClient, path: str
) -> None:
    response = client.get(path, params={"limit": 100, "offset": 100})
    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0, "limit": 100, "offset": 100}
