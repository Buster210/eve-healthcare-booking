import logging

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.enums import BookingStatus, PaymentStatus
from app.errors import Conflict, InvalidState, NotFound
from app.models import Booking, Payment, WebhookEvent
from app.schemas.payments import PaymentCreate, PaymentResponse, WebhookPayload
from app.simulator import simulate_payment
from app.state import InvalidTransition, apply_payment_outcome

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
        if booking.status != BookingStatus.PENDING:
            raise InvalidState("Booking must be pending to accept a payment")

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
            status=payment.status,
            provider_ref=payment.provider_ref,
            booking_status=booking.status,
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if exc.orig.diag.constraint_name == "uq_payments_active_booking":
            raise Conflict("Payment already exists", code="PAYMENT_EXISTS") from exc
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
        event_id = db.scalar(
            insert(WebhookEvent)
            .values(
                event_id=data.event_id,
                provider_ref=data.provider_ref,
                status=data.status,
                payload=data.model_dump(mode="json"),
            )
            .on_conflict_do_nothing(index_elements=[WebhookEvent.event_id])
            .returning(WebhookEvent.id)
        )
        if event_id is None:
            db.commit()
            logger.info(
                "webhook_duplicate",
                extra={"event": "webhook_duplicate", "event_id": data.event_id},
            )
            return "duplicate"

        booking_id = db.scalar(
            select(Payment.booking_id).where(Payment.provider_ref == data.provider_ref)
        )
        if booking_id is None:
            raise NotFound("Payment not found")

        # Every payment flow locks the booking first to avoid deadlocks.
        booking = db.scalar(
            select(Booking).where(Booking.id == booking_id).with_for_update()
        )
        payment = db.scalars(
            select(Payment)
            .where(Payment.provider_ref == data.provider_ref)
            .with_for_update()
        ).one()
        if payment.status == data.status:
            result = "no_op"
        else:
            try:
                # Isolate outcome database changes while retaining the webhook event.
                # apply_payment_outcome validates both transitions before mutating.
                with db.begin_nested():
                    apply_payment_outcome(booking, payment, PaymentStatus(data.status))
            except InvalidTransition:
                result = "ignored"
            else:
                result = "processed"
        payment_id = payment.id
        db.commit()
    except Exception:
        db.rollback()
        raise

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
