"""Booking/payment state machines — the ONLY place statuses change."""

from typing import Protocol

from app.enums import BookingStatus, PaymentStatus


class HasStatus(Protocol):
    """Anything with a settable status (ORM row, SimpleNamespace in tests)."""

    status: BookingStatus | PaymentStatus | str


class InvalidTransition(Exception):
    """Raised when a status change is not allowed by the transition table."""


# Allowed next statuses per current status. Terminal states have no entry:
# any transition out of them raises InvalidTransition.
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
    # StrEnum members compare by value, so PENDING == PENDING across families;
    # tell them apart by type. A plain-str current (raw DB value) is accepted
    # when it names a non-terminal state.
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
