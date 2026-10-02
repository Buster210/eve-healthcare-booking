import hashlib
import hmac
import logging
from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.errors import PayloadTooLarge, Unauthorized
from app.models import User
from app.schemas.common import ErrorResponse
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


async def signed_payload(
    request: Request,
    signature: Annotated[str, Header(alias="X-Signature")] = "",
    content_length: Annotated[int | None, Header(ge=0)] = None,
) -> WebhookPayload:
    settings = get_settings()
    if content_length is not None and content_length > settings.webhook_max_body_bytes:
        raise PayloadTooLarge("Webhook body exceeds size limit")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > settings.webhook_max_body_bytes:
            raise PayloadTooLarge("Webhook body exceeds size limit")
        body.extend(chunk)
    expected = hmac.new(
        settings.webhook_secret.get_secret_value().encode(), body, hashlib.sha256
    ).hexdigest()
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


@router.post(
    "/webhook/",
    response_model=WebhookResponse,
    responses={
        202: {"model": WebhookResponse, "description": "Queued for retry"},
        401: {"model": ErrorResponse, "description": "Invalid webhook signature"},
        404: {"model": ErrorResponse, "description": "Payment not found"},
        413: {"model": ErrorResponse, "description": "Webhook body exceeds size limit"},
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {"schema": WebhookPayload.model_json_schema()}
            },
        }
    },
)
def webhook(
    response: Response,
    data: Annotated[WebhookPayload, Depends(signed_payload)],
    db: Annotated[Session, Depends(get_db)],
) -> WebhookResponse:
    result = process_webhook(db, data)
    if result == "queued":
        response.status_code = 202
    return WebhookResponse(
        result=cast(
            Literal["duplicate", "no_op", "ignored", "processed", "queued"], result
        )
    )
