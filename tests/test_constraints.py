from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import Engine, insert, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Booking, CentreTest, Payment, User, WebhookEvent


@pytest.mark.parametrize(
    ("model", "name", "columns"),
    [
        (Payment, "ix_payments_booking_id", ["booking_id"]),
        (WebhookEvent, "ix_webhook_events_status_id", ["status", "id"]),
    ],
)
def test_migrated_query_indexes_match_metadata(
    test_engine: Engine,
    model: type[Payment] | type[WebhookEvent],
    name: str,
    columns: list[str],
) -> None:
    migrated = next(
        index
        for index in inspect(test_engine).get_indexes(model.__tablename__)
        if index["name"] == name
    )
    declared = next(index for index in model.__table__.indexes if index.name == name)
    assert (
        migrated["column_names"]
        == [column.name for column in declared.columns]
        == columns
    )
    assert migrated["unique"] is declared.unique is False
    assert not migrated.get("dialect_options", {}).get("postgresql_where")
    assert declared.dialect_options["postgresql"]["where"] is None


@pytest.mark.parametrize("amount", [Decimal("0"), Decimal("-1"), Decimal("NaN")])
@pytest.mark.parametrize("model", [Booking, Payment, CentreTest])
def test_money_constraints_reject_direct_inserts(
    db: Session,
    make_user: Callable[..., User],
    make_offering: Callable[..., CentreTest],
    model: type[Booking] | type[Payment] | type[CentreTest],
    amount: Decimal,
) -> None:
    user = make_user()
    offering = make_offering()
    if model is CentreTest:
        values = {
            "centre_id": offering.centre_id,
            "test_id": offering.test_id,
            "price": amount,
        }
        db.delete(offering)
        db.flush()
        constraint = "ck_centre_tests_price_positive"
    else:
        values = {
            "user_id": user.id,
            "centre_id": offering.centre_id,
            "test_id": offering.test_id,
            "appointment_at": datetime.now(UTC) + timedelta(days=1),
            "amount": amount,
        }
        if model is Payment:
            booking = Booking(**{**values, "amount": offering.price})
            db.add(booking)
            db.flush()
            values = {
                "booking_id": booking.id,
                "amount": amount,
                "provider_ref": "sim_constraint",
            }
        constraint = f"ck_{model.__tablename__}_amount_positive"
    with pytest.raises(IntegrityError) as caught:
        db.execute(insert(model).values(**values))
    assert caught.value.orig.diag.constraint_name == constraint
    db.rollback()
