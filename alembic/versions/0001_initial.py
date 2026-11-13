"""Esquema inicial: transactions e alerts.

Revision ID: 0001
Revises:
Create Date: 2026-11-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "transactions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("idempotency_key", sa.String(128), nullable=True, unique=True),
        sa.Column("customer_id", sa.String(64), nullable=False),
        sa.Column("counterparty_id", sa.String(64), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("device_id", sa.String(64), nullable=False),
        sa.Column("latitude", sa.Float, nullable=False),
        sa.Column("longitude", sa.Float, nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("score", sa.Float, nullable=True),
        sa.Column("decision", sa.String(16), nullable=True),
        sa.Column("model_version", sa.String(64), nullable=True),
        sa.Column("features", postgresql.JSONB, nullable=True),
        sa.Column("explanation", postgresql.JSONB, nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("amount > 0", name="ck_transactions_amount_positive"),
        sa.CheckConstraint(
            "channel IN ('pos', 'ecommerce', 'transfer')", name="ck_transactions_channel"
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'scored', 'failed')", name="ck_transactions_status"
        ),
        sa.CheckConstraint(
            "decision IS NULL OR decision IN ('approve', 'review', 'block')",
            name="ck_transactions_decision",
        ),
    )
    op.create_index(
        "ix_transactions_customer_occurred", "transactions", ["customer_id", "occurred_at"]
    )
    op.create_index(
        "ix_transactions_counterparty_occurred", "transactions", ["counterparty_id", "occurred_at"]
    )
    op.create_index("ix_transactions_status_created", "transactions", ["status", "created_at"])

    op.create_table(
        "alerts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "transaction_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("transactions.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("customer_id", sa.String(64), nullable=False),
        sa.Column("counterparty_id", sa.String(64), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("score", sa.Float, nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column("reasons", postgresql.JSONB, nullable=False),
        sa.Column("model_version", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("severity IN ('medium', 'high')", name="ck_alerts_severity"),
        sa.CheckConstraint("status IN ('open', 'confirmed', 'dismissed')", name="ck_alerts_status"),
    )
    op.create_index("ix_alerts_created", "alerts", ["created_at"])
    op.create_index("ix_alerts_customer", "alerts", ["customer_id"])
    op.create_index("ix_alerts_severity_status", "alerts", ["severity", "status"])


def downgrade() -> None:
    op.drop_table("alerts")
    op.drop_table("transactions")
