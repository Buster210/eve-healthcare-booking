from typing import cast

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.enums import BookingStatus
from app.models import Booking, User
from app.schemas.bookings import BookingCreate, BookingResponse
from app.schemas.common import Int4Id, Page, pagination_params
from app.security import get_current_user
from app.services.bookings import (
    cancel_booking,
    create_booking,
    get_booking,
    list_bookings,
)

router = APIRouter(prefix="/bookings", tags=["bookings"])


@router.post("", response_model=BookingResponse, status_code=201)
def create(
    payload: BookingCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Booking:
    return create_booking(db, user.id, payload)


@router.get("", response_model=Page[BookingResponse])
def list_own(
    status: BookingStatus | None = None,
    pagination: tuple[int, int] = Depends(pagination_params),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Page[BookingResponse]:
    limit, offset = pagination
    items, total = list_bookings(db, user.id, limit, offset, status)
    return Page[BookingResponse](
        items=cast(list[BookingResponse], items),
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{booking_id}", response_model=BookingResponse)
def get_own(
    booking_id: Int4Id,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Booking:
    return get_booking(db, user.id, booking_id)


@router.post("/{booking_id}/cancel", response_model=BookingResponse)
def cancel(
    booking_id: Int4Id,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Booking:
    return cancel_booking(db, user.id, booking_id)
