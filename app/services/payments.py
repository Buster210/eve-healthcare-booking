import logging
from datetime import UTC, datetime
from typing import cast

from psycopg import Error as PsycopgError
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.enums import BookingStatus, PaymentStatus
from app.errors import Conflict, InvalidState, NotFound
from app.models import Booking, Payment, WebhookEvent
from app.schemas.payments import PaymentCreate, PaymentResponse, WebhookPayload
from app.simulator import simulate_payment
from app.state import InvalidTransition, apply_payment_outcome, transition

logger = logging.getLogger(__name__)


def create_payment(db: Session, user_id: int, data: PaymentCreate) -> PaymentResponse:
    try:
        booking = db.scalar(
            select(Booking)
            .where(Booking.id == data.booking_id, Booking.user_id == user_id)
            .with_for_update()
        )
        if booking is None:
            raise NotFound("Booking not found")
        if booking.status not in (BookingStatus.PENDING, BookingStatus.FAILED):
            raise InvalidState("Booking must be pending or failed to accept a payment")
        if booking.appointment_at <= datetime.now(UTC):
            raise InvalidState("Booking appointment must be in the future")
        attempts = db.execute(
            select(func.count())
            .select_from(Payment)
            .where(Payment.booking_id == booking.id)
        ).scalar_one()
        if attempts >= 3:
            raise Conflict("Payment attempt limit reached", code="PAYMENT_LIMIT")
        if booking.status == BookingStatus.FAILED:
            transition(booking, BookingStatus.PENDING)
            db.flush()

        outcome, provider_ref = simulate_payment(data.simulate_outcome)
        payment = Payment(
            booking_id=booking.id,
            amount=booking.amount,
            provider_ref=provider_ref,
            status=PaymentStatus.PENDING,
        )
        db.add(payment)
        db.flush()
        apply_payment_outcome(booking, payment, outcome)
        response = PaymentResponse(
            id=payment.id,
            booking_id=booking.id,
            amount=payment.amount,
            status=cast(PaymentStatus, payment.status),
            provider_ref=payment.provider_ref,
            booking_status=cast(BookingStatus, booking.status),
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        constraint = cast(PsycopgError, exc.orig).diag.constraint_name
        if constraint == "uq_payments_active_booking":
            raise Conflict("Payment already exists", code="PAYMENT_EXISTS") from exc
        if constraint == "uq_bookings_active_slot":
            raise Conflict("An active booking already exists for this slot") from exc
        raise
    except Exception:
        db.rollback()
        raise

    ids = {"payment_id": response.id, "booking_id": response.booking_id}
    logger.info("payment_created", extra={"event": "payment_created", **ids})
    if response.status != PaymentStatus.PENDING:
        logger.info(
            "payment_resolved",
            extra={"event": "payment_resolved", "status": response.status, **ids},
        )
    return response


def process_webhook(db: Session, data: WebhookPayload) -> str:
    try:
        event = db.scalar(
            insert(WebhookEvent)
            .values(
                event_id=data.event_id,
                provider_ref=data.provider_ref,
                status="PROCESSED",
                payload=data.model_dump(mode="json"),
            )
            .on_conflict_do_nothing(index_elements=[WebhookEvent.event_id])
            .returning(WebhookEvent)
        )
        if event is None:
            db.commit()
            logger.info(
                "webhook_duplicate",
                extra={"event": "webhook_duplicate", "event_id": data.event_id},
            )
            return "duplicate"

        try:
            result = _apply_webhook_outcome(db, data)
        except NotFound:
            raise
        except Exception as exc:
            event.status = "FAILED"
            event.last_error = str(exc)
            logger.exception(
                "webhook_failed",
                extra={"event": "webhook_failed", "event_id": data.event_id},
            )
            result = "queued"
        db.commit()
    except Exception:
        db.rollback()
        raise
    return result


def _apply_webhook_outcome(
    db: Session, data: WebhookPayload, *, skip_locked: bool = False
) -> str:
    try:
        with db.begin_nested():
            booking_id = db.scalar(
                select(Payment.booking_id).where(
                    Payment.provider_ref == data.provider_ref
                )
            )
            if booking_id is None:
                raise NotFound("Payment not found")
            booking = db.scalar(
                select(Booking)
                .where(Booking.id == booking_id)
                .with_for_update(skip_locked=skip_locked)
            )
            if booking is None and skip_locked:
                return "busy"
            payment = db.scalar(
                select(Payment)
                .where(Payment.provider_ref == data.provider_ref)
                .with_for_update(skip_locked=skip_locked)
            )
            if payment is None and skip_locked:
                return "busy"
            if payment is None:
                raise NotFound("Payment not found")
            payment_id = payment.id
            if payment.status == data.status:
                result = "no_op"
            else:
                apply_payment_outcome(
                    cast(Booking, booking), payment, PaymentStatus(data.status)
                )
                result = "processed"
    except InvalidTransition:
        result = "ignored"

    ids = {
        "event_id": data.event_id,
        "payment_id": payment_id,
        "booking_id": booking_id,
    }
    if result == "ignored":
        logger.warning("webhook_ignored", extra={"event": "webhook_ignored", **ids})
    elif result == "processed":
        logger.info(
            "payment_resolved",
            extra={"event": "payment_resolved", "status": data.status, **ids},
        )
    return result


def retry_failed_webhook_event(
    db: Session, last_id: int = 0, max_attempts: int = 5
) -> tuple[int, str]:
    with db.begin_nested() as claim:
        event = db.scalar(
            select(WebhookEvent)
            .where(
                WebhookEvent.status == "FAILED",
                WebhookEvent.attempts < max_attempts,
                WebhookEvent.id > last_id,
            )
            .order_by(WebhookEvent.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if event is None:
            return last_id, "empty"
        last_id = event.id
        try:
            data = WebhookPayload.model_validate(event.payload)
            if _apply_webhook_outcome(db, data, skip_locked=True) == "busy":
                claim.rollback()
                return last_id, "busy"
        except Exception as exc:
            event.last_error = str(exc)
            logger.exception(
                "webhook_retry_failed",
                extra={"event": "webhook_retry_failed", "event_id": event.event_id},
            )
            result = "failed"
        else:
            event.status = "PROCESSED"
            event.last_error = None
            result = "processed"
        event.attempts += 1
        if result == "failed" and event.attempts >= max_attempts:
            logger.warning(
                "webhook_retries_exhausted",
                extra={"event_id": event.event_id, "attempts": event.attempts},
            )
    return last_id, result


def retry_failed_webhook_events(db: Session, max_attempts: int = 5) -> int:
    processed = 0
    last_id = 0
    while True:
        last_id, result = retry_failed_webhook_event(db, last_id, max_attempts)
        if result == "empty":
            break
        processed += result == "processed"
    return processed
