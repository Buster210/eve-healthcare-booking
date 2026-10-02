import logging
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import cache
from app.errors import Conflict, NotFound
from app.models import Centre, CentreTest, DiagnosticTest
from app.schemas.catalogue import (
    CentreCreate,
    CentreUpdate,
    DiagnosticTestCreate,
    DiagnosticTestUpdate,
)

logger = logging.getLogger(__name__)


def _constraint_name(exc: IntegrityError) -> str | None:
    return getattr(getattr(exc.orig, "diag", None), "constraint_name", None)


def list_centres(
    db: Session, limit: int, offset: int, q: str | None = None
) -> tuple[list[Centre], int]:
    filters = []
    if q:
        pattern = f"%{q}%"
        filters.append(Centre.name.ilike(pattern) | Centre.location.ilike(pattern))
    total = db.scalar(select(func.count()).select_from(Centre).where(*filters)) or 0
    items = db.scalars(
        select(Centre).where(*filters).order_by(Centre.id).limit(limit).offset(offset)
    ).all()
    return list(items), total


def get_centre_detail(
    db: Session, centre_id: int
) -> tuple[Centre, list[tuple[DiagnosticTest, Decimal]]]:
    centre = db.get(Centre, centre_id)
    if centre is None:
        raise NotFound("Centre not found")
    rows = db.execute(
        select(DiagnosticTest, CentreTest.price)
        .join(CentreTest, CentreTest.test_id == DiagnosticTest.id)
        .where(CentreTest.centre_id == centre_id)
        .order_by(DiagnosticTest.id)
    ).all()
    return centre, [(test, price) for test, price in rows]


def create_centre(db: Session, payload: CentreCreate) -> Centre:
    centre = Centre(name=payload.name, location=payload.location)
    db.add(centre)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if _constraint_name(exc) == "uq_centres_name_location":
            raise Conflict("Centre already exists") from exc
        raise
    cache.invalidate()
    db.refresh(centre)
    return centre


def update_centre(db: Session, centre_id: int, payload: CentreUpdate) -> Centre:
    centre = db.get(Centre, centre_id)
    if centre is None:
        raise NotFound("Centre not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(centre, field, value)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if _constraint_name(exc) == "uq_centres_name_location":
            raise Conflict("Centre already exists") from exc
        raise
    cache.invalidate()
    db.refresh(centre)
    return centre


def delete_centre(db: Session, centre_id: int) -> None:
    # Lock the parent before checking: its FK blocks concurrent offering inserts.
    centre = db.get(Centre, centre_id, with_for_update=True)
    if centre is None:
        raise NotFound("Centre not found")
    if (
        db.scalar(
            select(CentreTest.id).where(CentreTest.centre_id == centre_id).limit(1)
        )
        is not None
    ):
        raise Conflict("Centre has offerings and cannot be deleted")
    try:
        db.delete(centre)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if _constraint_name(exc) == "fk_bookings_centre_test":
            raise Conflict("Centre has bookings and cannot be deleted") from exc
        raise
    cache.invalidate()


def list_tests(
    db: Session, limit: int, offset: int
) -> tuple[list[DiagnosticTest], int]:
    total = db.scalar(select(func.count()).select_from(DiagnosticTest)) or 0
    items = db.scalars(
        select(DiagnosticTest).order_by(DiagnosticTest.id).limit(limit).offset(offset)
    ).all()
    return list(items), total


def create_test(db: Session, payload: DiagnosticTestCreate) -> DiagnosticTest:
    test = DiagnosticTest(name=payload.name, description=payload.description)
    db.add(test)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if _constraint_name(exc) == "uq_diagnostic_tests_name":
            raise Conflict("Test already exists") from exc
        raise
    cache.invalidate()
    db.refresh(test)
    return test


def update_test(
    db: Session, test_id: int, payload: DiagnosticTestUpdate
) -> DiagnosticTest:
    test = db.get(DiagnosticTest, test_id)
    if test is None:
        raise NotFound("Test not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(test, field, value)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if _constraint_name(exc) == "uq_diagnostic_tests_name":
            raise Conflict("Test already exists") from exc
        raise
    cache.invalidate()
    db.refresh(test)
    return test


def delete_test(db: Session, test_id: int) -> None:
    test = db.get(DiagnosticTest, test_id, with_for_update=True)
    if test is None:
        raise NotFound("Test not found")
    try:
        db.delete(test)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if _constraint_name(exc) in {
            "fk_centre_tests_test_id_diagnostic_tests",
            "fk_bookings_centre_test",
        }:
            raise Conflict("Test is in use and cannot be deleted") from exc
        raise
    cache.invalidate()


def upsert_offering(
    db: Session, centre_id: int, test_id: int, price: Decimal
) -> CentreTest:
    if db.get(Centre, centre_id, with_for_update=True) is None:
        raise NotFound("Centre not found")
    if db.get(DiagnosticTest, test_id, with_for_update=True) is None:
        raise NotFound("Test not found")
    offering = db.scalar(
        insert(CentreTest)
        .values(centre_id=centre_id, test_id=test_id, price=price)
        .on_conflict_do_update(
            constraint="uq_centre_tests_centre_test",
            set_={"price": price},
        )
        .returning(CentreTest)
    )
    db.commit()
    cache.invalidate()
    assert offering is not None
    logger.info(
        "offering_upserted",
        extra={"centre_id": centre_id, "test_id": test_id},
    )
    return offering


def delete_offering(db: Session, centre_id: int, test_id: int) -> None:
    offering = db.scalar(
        select(CentreTest).where(
            CentreTest.centre_id == centre_id,
            CentreTest.test_id == test_id,
        )
    )
    if offering is None:
        raise NotFound("Offering not found")
    try:
        db.delete(offering)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if _constraint_name(exc) == "fk_bookings_centre_test":
            raise Conflict("Offering has bookings and cannot be deleted") from exc
        raise
    cache.invalidate()
