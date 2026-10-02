"""Simulated payment provider: in-process, no I/O."""

import random
import uuid

from app.enums import PaymentStatus


def simulate_payment(
    requested_outcome: PaymentStatus | str | None = None,
) -> tuple[PaymentStatus, str]:
    """Return (outcome, provider_ref). Omitted outcome -> random SUCCESS/FAILED."""
    if requested_outcome is None:
        outcome = random.choice([PaymentStatus.SUCCESS, PaymentStatus.FAILED])
    else:
        outcome = PaymentStatus(requested_outcome)
    return outcome, f"sim_{uuid.uuid4().hex}"
