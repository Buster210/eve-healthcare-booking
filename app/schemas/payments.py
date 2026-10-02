from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

from app.enums import BookingStatus, PaymentStatus
from app.schemas.common import Int4Id


class PaymentCreate(BaseModel):
    booking_id: Int4Id
    simulate_outcome: PaymentStatus | None = None


class PaymentResponse(BaseModel):
    id: Int4Id
    booking_id: Int4Id
    amount: Decimal
    status: PaymentStatus
    provider_ref: str
    booking_status: BookingStatus


class WebhookPayload(BaseModel):
    event_id: str = Field(min_length=1, max_length=100)
    provider_ref: str = Field(min_length=1, max_length=100)
    status: Literal[PaymentStatus.SUCCESS, PaymentStatus.FAILED]


class WebhookResponse(BaseModel):
    result: Literal["duplicate", "no_op", "ignored", "processed", "queued"]
