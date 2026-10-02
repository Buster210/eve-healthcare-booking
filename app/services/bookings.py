import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.enums import BookingStatus, PaymentStatus
from app.errors import Conflict, InvalidState, NotFound, Unauthorized
from app.models import Booking, CentreTest, Payment
from app.schemas.bookings import BookingCreate
from app.state import InvalidTransition, transition

logger = logging.getLogger(__name__)


def expire_stale_bookings(db: Session, older_than: timedelta) -> int:
    if older_than <= timedelta(0):
        raise ValueError("older_than must be positive")
    bookings = db.scalars(
        select(Booking)
        .where(
            Booking.status == BookingStatus.PENDING,
            Booking.created_at < datetime.now(UTC) - older_than,
        )
        .order_by(Booking.id)
        .with_for_update(skip_locked=True)
    ).all()
    count = 0
    for booking in bookings:
        active_payment = db.scalar(
            select(Payment.id)
            .where(
                Payment.booking_id == booking.id,
                Payment.status.in_([PaymentStatus.PENDING, PaymentStatus.SUCCESS]),
            )
            .with_for_update(skip_locked=True)
        )
        if active_payment is None:
            has_active = db.scalar(
                select(Payment.id).where(
                    Payment.booking_id == booking.id,
                    Payment.status.in_([PaymentStatus.PENDING, PaymentStatus.SUCCESS]),
                )
            )
            if has_active is not None:
                continue
            transition(booking, BookingStatus.CANCELLED)
            count += 1
    db.flush()
    return count


def create_booking(db: Session, user_id: int, payload: BookingCreate) -> Booking:
    offering = db.scalar(
        select(CentreTest).where(
            CentreTest.centre_id == payload.centre_id,
            CentreTest.test_id == payload.test_id,
        )
    )
    if offering is None:
        raise NotFound("Test is not offered at this centre")
    booking = Booking(
        user_id=user_id,
        centre_id=payload.centre_id,
        test_id=payload.test_id,
        appointment_at=payload.appointment_at,
        amount=offering.price,
    )
    db.add(booking)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        constraint_name = exc.orig.diag.constraint_name
        if constraint_name == "uq_bookings_active_slot":
            raise Conflict("An active booking already exists for this slot") from exc
        if constraint_name == "fk_bookings_centre_test":
            raise NotFound("Test is not offered at this centre") from exc
        if constraint_name == "fk_bookings_user_id_users":
            raise Unauthorized() from exc
        raise
    db.refresh(booking)
    logger.info("booking_created", extra={"booking_id": booking.id, "user_id": user_id})
    return booking


def list_bookings(
    db: Session,
    user_id: int,
    limit: int,
    offset: int,
    status: BookingStatus | None,
) -> tuple[list[Booking], int]:
    filters = [Booking.user_id == user_id]
    if status is not None:
        filters.append(Booking.status == status)
    total = db.scalar(select(func.count()).select_from(Booking).where(*filters)) or 0
    items = db.scalars(
        select(Booking)
        .where(*filters)
        .order_by(Booking.created_at.desc(), Booking.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return list(items), total


def get_booking(db: Session, user_id: int, booking_id: int) -> Booking:
    booking = db.scalar(
        select(Booking).where(Booking.id == booking_id, Booking.user_id == user_id)
    )
    if booking is None:
        raise NotFound("Booking not found")
    return booking


def cancel_booking(db: Session, user_id: int, booking_id: int) -> Booking:
    booking = db.scalar(
        select(Booking)
        .where(Booking.id == booking_id, Booking.user_id == user_id)
        .with_for_update()
    )
    if booking is None:
        raise NotFound("Booking not found")
    pending_payment = db.scalar(
        select(Payment.id).where(
            Payment.booking_id == booking_id, Payment.status == PaymentStatus.PENDING
        )
    )
    try:
        if pending_payment is not None:
            raise InvalidState("Booking has a pending payment")
        transition(booking, BookingStatus.CANCELLED)
        db.commit()
    except InvalidTransition as exc:
        db.rollback()
        raise InvalidState("Only pending bookings can be cancelled") from exc
    except Exception:
        db.rollback()
        raise
    db.refresh(booking)
    logger.info(
        "booking_cancelled", extra={"booking_id": booking.id, "user_id": user_id}
    )
    return booking
