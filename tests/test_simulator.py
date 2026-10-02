"""Simulator tests. No DB, no conftest fixtures."""

import re

import pytest

from app.enums import PaymentStatus
from app.simulator import simulate_payment

REF_RE = re.compile(r"^sim_[0-9a-f]{32}$")


@pytest.mark.parametrize("requested", ["SUCCESS", "FAILED", "PENDING"])
def test_requested_string_outcome(requested):
    status, ref = simulate_payment(requested)
    assert status == PaymentStatus(requested)
    assert REF_RE.match(ref)


@pytest.mark.parametrize("requested", list(PaymentStatus))
def test_requested_enum_outcome(requested):
    status, ref = simulate_payment(requested)
    assert status == requested
    assert REF_RE.match(ref)


def test_omitted_outcome_is_random_success_or_failed():
    for _ in range(20):
        status, ref = simulate_payment()
        assert status in (PaymentStatus.SUCCESS, PaymentStatus.FAILED)
        assert REF_RE.match(ref)


def test_provider_refs_unique():
    refs = {simulate_payment("SUCCESS")[1] for _ in range(100)}
    assert len(refs) == 100
