"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-10-07
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

ACTIVE = sa.text("status IN ('pending','processing')")


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False, unique=True),
        sa.Column("is_authenticated", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("authenticated_at", sa.DateTime(timezone=True)),
        sa.Column("failed_auth_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("auth_locked_until", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("telegram_id > 0", name="ck_users_telegram_id_positive"),
    )
    op.create_table(
        "scan_requests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "telegram_user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.telegram_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("error", sa.Text()),
        sa.CheckConstraint(
            "status IN ('pending','processing','completed','expired','failed','cancelled')",
            name="ck_scan_requests_status",
        ),
    )
    op.create_index("ix_scan_requests_status_created", "scan_requests", ["status", "created_at"])
    op.create_index(
        "uq_scan_requests_one_active_per_user",
        "scan_requests",
        ["telegram_user_id"],
        unique=True,
        postgresql_where=ACTIVE,
        sqlite_where=ACTIVE,
    )
    op.create_table(
        "print_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "telegram_user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.telegram_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("copies", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("file_size", sa.Integer(), nullable=False),
        sa.Column("page_count", sa.Integer()),
        sa.Column("printer_job_id", sa.Integer()),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("copies = 1", name="ck_print_jobs_single_copy"),
        sa.CheckConstraint("status IN ('queued','processing','completed','failed')", name="ck_print_jobs_status"),
    )


def downgrade() -> None:
    op.drop_table("print_jobs")
    op.drop_index("uq_scan_requests_one_active_per_user", table_name="scan_requests")
    op.drop_index("ix_scan_requests_status_created", table_name="scan_requests")
    op.drop_table("scan_requests")
    op.drop_table("users")
