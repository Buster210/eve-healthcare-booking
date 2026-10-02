from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, validates

from app.enums import BookingStatus, PaymentStatus


class Base(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "pk": "pk_%(table_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ix": "ix_%(table_name)s_%(column_0_name)s",
        }
    )


class User(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("email", name="uq_users_email"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320))
    password_hash: Mapped[str] = mapped_column(Text)
    full_name: Mapped[str] = mapped_column(String(100))
    is_admin: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    @validates("email")
    def normalize_email(self, key: str, value: str) -> str:
        return value.lower()


class Centre(Base):
    __tablename__ = "centres"
    __table_args__ = (
        UniqueConstraint("name", "location", name="uq_centres_name_location"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    location: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class DiagnosticTest(Base):
    __tablename__ = "diagnostic_tests"
    __table_args__ = (UniqueConstraint("name", name="uq_diagnostic_tests_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class CentreTest(Base):
    __tablename__ = "centre_tests"
    __table_args__ = (
        UniqueConstraint("centre_id", "test_id", name="uq_centre_tests_centre_test"),
        CheckConstraint(
            "price > 0 AND price <> 'NaN'", name="ck_centre_tests_price_positive"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    centre_id: Mapped[int] = mapped_column(ForeignKey("centres.id", ondelete="CASCADE"))
    test_id: Mapped[int] = mapped_column(
        ForeignKey("diagnostic_tests.id", ondelete="RESTRICT")
    )
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2))


class Booking(Base):
    __tablename__ = "bookings"
    __table_args__ = (
        ForeignKeyConstraint(
            ["centre_id", "test_id"],
            ["centre_tests.centre_id", "centre_tests.test_id"],
            name="fk_bookings_centre_test",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "status IN ('PENDING', 'CONFIRMED', 'FAILED', 'CANCELLED')",
            name="ck_bookings_status",
        ),
        CheckConstraint(
            "amount > 0 AND amount <> 'NaN'", name="ck_bookings_amount_positive"
        ),
        Index("ix_bookings_user_created_at", "user_id", text("created_at DESC")),
        Index(
            "uq_bookings_active_slot",
            "user_id",
            "centre_id",
            "test_id",
            "appointment_at",
            unique=True,
            postgresql_where=text("status IN ('PENDING', 'CONFIRMED')"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    centre_id: Mapped[int]
    test_id: Mapped[int]
    appointment_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    status: Mapped[str] = mapped_column(
        String(20), default=BookingStatus.PENDING, server_default="PENDING"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (
        UniqueConstraint("provider_ref", name="uq_payments_provider_ref"),
        CheckConstraint(
            "status IN ('PENDING', 'SUCCESS', 'FAILED')", name="ck_payments_status"
        ),
        CheckConstraint(
            "amount > 0 AND amount <> 'NaN'", name="ck_payments_amount_positive"
        ),
        Index(
            "uq_payments_active_booking",
            "booking_id",
            unique=True,
            postgresql_where=text("status IN ('PENDING', 'SUCCESS')"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    booking_id: Mapped[int] = mapped_column(ForeignKey("bookings.id"))
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    status: Mapped[str] = mapped_column(
        String(20), default=PaymentStatus.PENDING, server_default="PENDING"
    )
    provider_ref: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class WebhookEvent(Base):
    __tablename__ = "webhook_events"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_webhook_events_event_id"),
        CheckConstraint(
            "status IN ('PROCESSED', 'FAILED')", name="ck_webhook_events_status"
        ),
        Index("ix_webhook_events_provider_ref", "provider_ref"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[str] = mapped_column(String(100))
    provider_ref: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(
        String(20), default="PROCESSED", server_default="PROCESSED"
    )
    attempts: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    last_error: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
