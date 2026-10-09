from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class ScanStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    EXPIRED = "expired"
    FAILED = "failed"
    CANCELLED = "cancelled"


ACTIVE_SCAN_STATUSES = (ScanStatus.PENDING, ScanStatus.PROCESSING)


class PrintStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    is_authenticated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    authenticated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_auth_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    auth_locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (CheckConstraint("telegram_id > 0", name="ck_users_telegram_id_positive"),)


class ScanRequest(Base):
    __tablename__ = "scan_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=ScanStatus.PENDING)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','processing','completed','expired','failed','cancelled')",
            name="ck_scan_requests_status",
        ),
        # FIFO lookup of the next pending request.
        Index("ix_scan_requests_status_created", "status", "created_at"),
        # A user can have at most one active (pending/processing) request.
        Index(
            "uq_scan_requests_one_active_per_user",
            "telegram_user_id",
            unique=True,
            postgresql_where=text("status IN ('pending','processing')"),
            sqlite_where=text("status IN ('pending','processing')"),
        ),
    )


class PrintJob(Base):
    __tablename__ = "print_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=PrintStatus.QUEUED)
    copies: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    file_size: Mapped[int] = mapped_column(Integer, nullable=False)
    page_count: Mapped[int | None] = mapped_column(Integer)
    printer_job_id: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # Business rule enforced by the database too: every job is exactly one copy.
        CheckConstraint("copies = 1", name="ck_print_jobs_single_copy"),
        CheckConstraint(
            "status IN ('queued','processing','completed','failed')", name="ck_print_jobs_status"
        ),
    )
