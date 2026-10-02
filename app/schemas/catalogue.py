from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.common import Int4Id


class CentreCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    location: str = Field(min_length=1, max_length=255)


class CentreUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    location: str | None = Field(default=None, min_length=1, max_length=255)

    @model_validator(mode="after")
    def require_change(self) -> "CentreUpdate":
        if not self.model_fields_set:
            raise ValueError("Nothing to update")
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("Name and location cannot be null")
        return self


class CentreResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: Int4Id
    name: str
    location: str
    created_at: datetime
    updated_at: datetime


class OfferedTestResponse(BaseModel):
    id: Int4Id
    name: str
    description: str | None
    price: Decimal


class CentreDetailResponse(CentreResponse):
    tests: list[OfferedTestResponse] = []


class DiagnosticTestCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = None


class DiagnosticTestUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = None

    @model_validator(mode="after")
    def require_change(self) -> "DiagnosticTestUpdate":
        if not self.model_fields_set:
            raise ValueError("Nothing to update")
        if "name" in self.model_fields_set and self.name is None:
            raise ValueError("Name cannot be null")
        return self


class DiagnosticTestResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: Int4Id
    name: str
    description: str | None
    created_at: datetime


class OfferingUpsert(BaseModel):
    price: Decimal = Field(gt=0, max_digits=10, decimal_places=2)


class OfferingResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    centre_id: Int4Id
    test_id: Int4Id
    price: Decimal
