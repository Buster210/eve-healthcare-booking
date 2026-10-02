"""State machine tests. Plain objects only — no DB, no conftest fixtures."""

from types import SimpleNamespace

import pytest

from app.enums import BookingStatus, PaymentStatus
from app.state import InvalidTransition, apply_payment_outcome, transition


def entity(status):
    return SimpleNamespace(status=status)


@pytest.mark.parametrize(
    "new_status",
    [BookingStatus.CONFIRMED, BookingStatus.FAILED, BookingStatus.CANCELLED],
)
def test_booking_pending_to_terminal(new_status):
    b = entity(BookingStatus.PENDING)
    transition(b, new_status)
    assert b.status == new_status


@pytest.mark.parametrize(
    "current",
    [BookingStatus.CONFIRMED, BookingStatus.FAILED, BookingStatus.CANCELLED],
)
@pytest.mark.parametrize("new_status", list(BookingStatus))
def test_booking_terminal_has_no_outgoing(current, new_status):
    with pytest.raises(InvalidTransition):
        transition(entity(current), new_status)


def test_booking_rejects_payment_only_status():
    with pytest.raises(InvalidTransition):
        transition(entity(BookingStatus.PENDING), PaymentStatus.SUCCESS)
    with pytest.raises(InvalidTransition):
        transition(entity(BookingStatus.PENDING), PaymentStatus.FAILED)


def test_self_transitions_rejected():
    with pytest.raises(InvalidTransition):
        transition(entity(BookingStatus.PENDING), BookingStatus.PENDING)
    with pytest.raises(InvalidTransition):
        transition(entity(PaymentStatus.PENDING), PaymentStatus.PENDING)


def test_unknown_status_rejected():
    with pytest.raises(InvalidTransition):
        transition(entity(BookingStatus.PENDING), "BOGUS")


@pytest.mark.parametrize("new_status", [PaymentStatus.SUCCESS, PaymentStatus.FAILED])
def test_payment_pending_to_terminal(new_status):
    p = entity(PaymentStatus.PENDING)
    transition(p, new_status)
    assert p.status == new_status


@pytest.mark.parametrize("current", [PaymentStatus.SUCCESS, PaymentStatus.FAILED])
@pytest.mark.parametrize("new_status", list(PaymentStatus))
def test_payment_terminal_has_no_outgoing(current, new_status):
    with pytest.raises(InvalidTransition):
        transition(entity(current), new_status)


def test_payment_rejects_booking_only_status():
    with pytest.raises(InvalidTransition):
        transition(entity(PaymentStatus.PENDING), BookingStatus.CONFIRMED)
    with pytest.raises(InvalidTransition):
        transition(entity(PaymentStatus.PENDING), BookingStatus.FAILED)


def test_plain_string_current_status_accepted():
    b = entity("PENDING")
    transition(b, BookingStatus.CONFIRMED)
    assert b.status == BookingStatus.CONFIRMED


def test_apply_payment_outcome_success():
    b, p = entity(BookingStatus.PENDING), entity(PaymentStatus.PENDING)
    apply_payment_outcome(b, p, PaymentStatus.SUCCESS)
    assert p.status == PaymentStatus.SUCCESS
    assert b.status == BookingStatus.CONFIRMED


def test_apply_payment_outcome_failed():
    b, p = entity(BookingStatus.PENDING), entity(PaymentStatus.PENDING)
    apply_payment_outcome(b, p, PaymentStatus.FAILED)
    assert p.status == PaymentStatus.FAILED
    assert b.status == BookingStatus.FAILED


def test_apply_payment_outcome_pending_leaves_both():
    b, p = entity(BookingStatus.PENDING), entity(PaymentStatus.PENDING)
    apply_payment_outcome(b, p, PaymentStatus.PENDING)
    assert b.status == BookingStatus.PENDING
    assert p.status == PaymentStatus.PENDING


def test_apply_payment_outcome_accepts_strings():
    b, p = entity(BookingStatus.PENDING), entity(PaymentStatus.PENDING)
    apply_payment_outcome(b, p, "SUCCESS")
    assert p.status == PaymentStatus.SUCCESS
    assert b.status == BookingStatus.CONFIRMED


def test_apply_payment_outcome_on_terminal_payment_raises():
    b = entity(BookingStatus.CONFIRMED)
    p = entity(PaymentStatus.SUCCESS)
    with pytest.raises(InvalidTransition):
        apply_payment_outcome(b, p, PaymentStatus.FAILED)


@pytest.mark.parametrize(
    "status", [BookingStatus.CONFIRMED, BookingStatus.FAILED, BookingStatus.CANCELLED]
)
@pytest.mark.parametrize("outcome", [PaymentStatus.SUCCESS, PaymentStatus.FAILED])
def test_terminal_booking_rejects_outcome_without_mutating_payment(status, outcome):
    b, p = entity(status), entity(PaymentStatus.PENDING)
    with pytest.raises(InvalidTransition):
        apply_payment_outcome(b, p, outcome)
    assert b.status == status
    assert p.status == PaymentStatus.PENDING


@pytest.mark.parametrize("status", [PaymentStatus.SUCCESS, PaymentStatus.FAILED])
def test_terminal_payment_rejects_outcome_without_mutating_booking(status):
    b, p = entity(BookingStatus.PENDING), entity(status)
    with pytest.raises(InvalidTransition):
        apply_payment_outcome(b, p, PaymentStatus.SUCCESS)
    assert b.status == BookingStatus.PENDING
    assert p.status == status
