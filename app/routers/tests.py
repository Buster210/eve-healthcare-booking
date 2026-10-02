from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import User
from app.schemas.catalogue import (
    DiagnosticTestCreate,
    DiagnosticTestResponse,
    DiagnosticTestUpdate,
)
from app.schemas.common import Int4Id, Page, pagination_params
from app.security import require_admin
from app.services.catalogue import (
    create_test,
    delete_test,
    list_tests,
    update_test,
)

router = APIRouter(prefix="/tests", tags=["tests"])


@router.get("", response_model=Page[DiagnosticTestResponse])
def list_all(
    pagination: tuple[int, int] = Depends(pagination_params),
    db: Session = Depends(get_db),
) -> Page[DiagnosticTestResponse]:
    limit, offset = pagination
    items, total = list_tests(db, limit, offset)
    return Page[DiagnosticTestResponse](
        items=items, total=total, limit=limit, offset=offset
    )


@router.post("", response_model=DiagnosticTestResponse, status_code=201)
def create(
    payload: DiagnosticTestCreate,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> DiagnosticTestResponse:
    return DiagnosticTestResponse.model_validate(create_test(db, payload))


@router.patch("/{test_id}", response_model=DiagnosticTestResponse)
def update(
    test_id: Int4Id,
    payload: DiagnosticTestUpdate,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> DiagnosticTestResponse:
    return DiagnosticTestResponse.model_validate(update_test(db, test_id, payload))


@router.delete("/{test_id}", status_code=204)
def delete(
    test_id: Int4Id,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> None:
    delete_test(db, test_id)
