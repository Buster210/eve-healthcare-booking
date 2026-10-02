import hashlib
import hmac
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.errors import Unauthorized
from app.models import User
from app.schemas.payments import (
    PaymentCreate,
    PaymentResponse,
    WebhookPayload,
    WebhookResponse,
)
from app.security import get_current_user
from app.services.payments import create_payment, process_webhook

router = APIRouter(prefix="/payments", tags=["payments"])
logger = logging.getLogger(__name__)


async def signed_payload(request: Request) -> WebhookPayload:
    body = await request.body()
    expected = hmac.new(
        get_settings().webhook_secret.get_secret_value().encode(), body, hashlib.sha256
    ).hexdigest()
    signature = request.headers.get("X-Signature", "")
    if not hmac.compare_digest(expected.encode(), signature.encode()):
        logger.warning(
            "webhook_bad_signature", extra={"event": "webhook_bad_signature"}
        )
        raise Unauthorized("Invalid webhook signature")
    try:
        return WebhookPayload.model_validate_json(body)
    except ValidationError as exc:
        raise RequestValidationError(exc.errors()) from exc


@router.post("/", response_model=PaymentResponse, status_code=201)
def pay(
    data: PaymentCreate,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> PaymentResponse:
    return create_payment(db, user.id, data)


@router.post("/webhook/", response_model=WebhookResponse)
def webhook(
    response: Response,
    data: Annotated[WebhookPayload, Depends(signed_payload)],
    db: Annotated[Session, Depends(get_db)],
) -> WebhookResponse:
    result = process_webhook(db, data)
    if result == "queued":
        response.status_code = 202
    return WebhookResponse(result=result)
