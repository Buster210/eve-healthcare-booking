import logging
from typing import cast

from fastapi import APIRouter, Depends
from psycopg import Error as PsycopgError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.errors import Conflict, Unauthorized
from app.models import User
from app.ratelimit import rate_limit
from app.schemas.auth import LoginRequest, SignupRequest, TokenResponse, UserResponse
from app.security import (
    create_access_token,
    get_current_user,
    hash_password,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])
logger = logging.getLogger(__name__)


@router.post(
    "/signup",
    response_model=UserResponse,
    status_code=201,
    dependencies=[Depends(rate_limit("signup", 10, 60))],
)
def signup(payload: SignupRequest, db: Session = Depends(get_db)) -> User:
    user = User(
        email=str(payload.email),
        password_hash=hash_password(payload.password),
        full_name=payload.full_name,
        is_admin=False,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if cast(PsycopgError, exc.orig).diag.constraint_name == "uq_users_email":
            raise Conflict("Email already registered") from exc
        raise
    db.refresh(user)
    logger.info("user_signed_up", extra={"user_id": user.id})
    return user


@router.post(
    "/login",
    response_model=TokenResponse,
    dependencies=[Depends(rate_limit("login", 10, 60))],
)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    user = db.scalar(select(User).where(User.email == str(payload.email)))
    if not verify_password(payload.password, user.password_hash if user else None):
        raise Unauthorized("Invalid email or password")
    return TokenResponse(
        access_token=create_access_token(cast(User, user).id),
        expires_in=get_settings().jwt_expire_minutes * 60,
    )


@router.get("/me", response_model=UserResponse)
def me(user: User = Depends(get_current_user)) -> User:
    return user
