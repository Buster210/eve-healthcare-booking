"""Allowed booking and payment transitions."""

from typing import Protocol

from app.enums import BookingStatus, PaymentStatus


class HasStatus(Protocol):
    """Objects accepted by the transition helpers."""

    status: BookingStatus | PaymentStatus | str


class InvalidTransition(Exception):
    """Raised when a status change is not allowed by the transition table."""


# Only pending entities can transition; the remaining states are terminal.
BOOKING_TRANSITIONS: dict[str, frozenset[str]] = {
    BookingStatus.PENDING: frozenset(
        {BookingStatus.CONFIRMED, BookingStatus.FAILED, BookingStatus.CANCELLED}
    ),
}

PAYMENT_TRANSITIONS: dict[str, frozenset[str]] = {
    PaymentStatus.PENDING: frozenset({PaymentStatus.SUCCESS, PaymentStatus.FAILED}),
}

_PAYMENT_TO_BOOKING = {
    PaymentStatus.SUCCESS: BookingStatus.CONFIRMED,
    PaymentStatus.FAILED: BookingStatus.FAILED,
}


def _validate_transition(
    entity: HasStatus, new_status: BookingStatus | PaymentStatus
) -> None:
    if isinstance(new_status, BookingStatus):
        table = BOOKING_TRANSITIONS
    elif isinstance(new_status, PaymentStatus):
        table = PAYMENT_TRANSITIONS
    else:
        raise InvalidTransition(f"Unknown status: {new_status!r}")
    current = entity.status
    # Both enum families contain PENDING; check types to distinguish them.
    # ORM models can expose the current status as a plain string.
    if type(current) is not type(new_status) and not (
        type(current) is str and current in table
    ):
        raise InvalidTransition(f"Cannot transition from {current!r} to {new_status!r}")
    if new_status not in table.get(current, frozenset()):
        raise InvalidTransition(
            f"Cannot transition from {entity.status} to {new_status}"
        )


def transition(entity: HasStatus, new_status: BookingStatus | PaymentStatus) -> None:
    """Move entity to new_status or raise InvalidTransition."""
    _validate_transition(entity, new_status)
    entity.status = new_status


def apply_payment_outcome(
    booking: HasStatus, payment: HasStatus, outcome: PaymentStatus | str
) -> None:
    """Validate both transitions before mutating. PENDING leaves both as-is."""
    outcome = PaymentStatus(outcome)
    if outcome == PaymentStatus.PENDING:
        return  # resolved later by webhook
    booking_status = _PAYMENT_TO_BOOKING[outcome]
    _validate_transition(payment, outcome)
    _validate_transition(booking, booking_status)
    payment.status = outcome
    booking.status = booking_status
