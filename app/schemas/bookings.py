from datetime import UTC, datetime
from decimal import Decimal

from pydantic import AwareDatetime, BaseModel, ConfigDict, field_validator

from app.enums import BookingStatus
from app.schemas.common import Int4Id


class BookingCreate(BaseModel):
    centre_id: Int4Id
    test_id: Int4Id
    appointment_at: AwareDatetime

    @field_validator("appointment_at")
    @classmethod
    def future_appointment(cls, value: datetime) -> datetime:
        try:
            value = value.astimezone(UTC)
        except OverflowError as exc:
            raise ValueError("Appointment is out of range") from exc
        if value <= datetime.now(UTC):
            raise ValueError("Appointment must be in the future")
        return value


class BookingResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: Int4Id
    user_id: Int4Id
    centre_id: Int4Id
    test_id: Int4Id
    appointment_at: datetime
    amount: Decimal
    status: BookingStatus
    created_at: datetime
    updated_at: datetime
