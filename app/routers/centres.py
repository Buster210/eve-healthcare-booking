from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import User
from app.schemas.catalogue import (
    CentreCreate,
    CentreDetailResponse,
    CentreResponse,
    CentreUpdate,
    OfferedTestResponse,
    OfferingResponse,
    OfferingUpsert,
)
from app.schemas.common import Int4Id, Page, pagination_params
from app.security import require_admin
from app.services.catalogue import (
    create_centre,
    delete_centre,
    delete_offering,
    get_centre_detail,
    list_centres,
    update_centre,
    upsert_offering,
)

router = APIRouter(prefix="/centres", tags=["centres"])


@router.get("", response_model=Page[CentreResponse])
def list_all(
    q: str | None = Query(default=None),
    pagination: tuple[int, int] = Depends(pagination_params),
    db: Session = Depends(get_db),
) -> Page[CentreResponse]:
    limit, offset = pagination
    items, total = list_centres(db, limit, offset, q)
    return Page[CentreResponse](items=items, total=total, limit=limit, offset=offset)


@router.get("/{centre_id}", response_model=CentreDetailResponse)
def get_one(centre_id: Int4Id, db: Session = Depends(get_db)) -> CentreDetailResponse:
    centre, offerings = get_centre_detail(db, centre_id)
    return CentreDetailResponse(
        id=centre.id,
        name=centre.name,
        location=centre.location,
        created_at=centre.created_at,
        updated_at=centre.updated_at,
        tests=[
            OfferedTestResponse(
                id=test.id,
                name=test.name,
                description=test.description,
                price=price,
            )
            for test, price in offerings
        ],
    )


@router.post("", response_model=CentreResponse, status_code=201)
def create(
    payload: CentreCreate,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> CentreResponse:
    return CentreResponse.model_validate(create_centre(db, payload))


@router.patch("/{centre_id}", response_model=CentreResponse)
def update(
    centre_id: Int4Id,
    payload: CentreUpdate,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> CentreResponse:
    return CentreResponse.model_validate(update_centre(db, centre_id, payload))


@router.delete("/{centre_id}", status_code=204)
def delete(
    centre_id: Int4Id,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> None:
    delete_centre(db, centre_id)


@router.put("/{centre_id}/tests/{test_id}", response_model=OfferingResponse)
def upsert_price(
    centre_id: Int4Id,
    test_id: Int4Id,
    payload: OfferingUpsert,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> OfferingResponse:
    offering = upsert_offering(db, centre_id, test_id, payload.price)
    return OfferingResponse.model_validate(offering)


@router.delete("/{centre_id}/tests/{test_id}", status_code=204)
def delete_price(
    centre_id: Int4Id,
    test_id: Int4Id,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> None:
    delete_offering(db, centre_id, test_id)
