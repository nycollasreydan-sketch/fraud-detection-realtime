"""Modelos de persistência (SQLAlchemy 2). O esquema é criado pelas migrações Alembic."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from fraud.domain import AlertSeverity, AlertStatus, Channel, Decision, TransactionStatus, TxEvent


def _in(column: str, enum: type) -> str:
    values = ", ".join(f"'{m.value}'" for m in enum)  # type: ignore[attr-defined]
    return f"{column} IN ({values})"


class Base(DeclarativeBase):
    pass


class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_transactions_amount_positive"),
        CheckConstraint(_in("channel", Channel), name="ck_transactions_channel"),
        CheckConstraint(_in("status", TransactionStatus), name="ck_transactions_status"),
        CheckConstraint(
            f"decision IS NULL OR {_in('decision', Decision)}", name="ck_transactions_decision"
        ),
        Index("ix_transactions_customer_occurred", "customer_id", "occurred_at"),
        Index("ix_transactions_counterparty_occurred", "counterparty_id", "occurred_at"),
        Index("ix_transactions_status_created", "status", "created_at"),
        Index("ix_transactions_created", "created_at"),  # janelas de tempo do /stats
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), unique=True)
    customer_id: Mapped[str] = mapped_column(String(64))
    counterparty_id: Mapped[str] = mapped_column(String(64))
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    currency: Mapped[str] = mapped_column(String(3))
    channel: Mapped[str] = mapped_column(String(16))
    category: Mapped[str] = mapped_column(String(32))
    device_id: Mapped[str] = mapped_column(String(64))
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    status: Mapped[str] = mapped_column(String(16), default=TransactionStatus.PENDING.value)
    score: Mapped[float | None] = mapped_column(Float)
    decision: Mapped[str | None] = mapped_column(String(16))
    model_version: Mapped[str | None] = mapped_column(String(64))
    features: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    explanation: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def to_event(self) -> TxEvent:
        return TxEvent(
            tx_id=str(self.id),
            customer_id=self.customer_id,
            counterparty_id=self.counterparty_id,
            amount=float(self.amount),
            channel=self.channel,
            category=self.category,
            device_id=self.device_id,
            lat=self.latitude,
            lon=self.longitude,
            ts=self.occurred_at.timestamp(),
        )


class Alert(Base):
    __tablename__ = "alerts"
    __table_args__ = (
        CheckConstraint(_in("severity", AlertSeverity), name="ck_alerts_severity"),
        CheckConstraint(_in("status", AlertStatus), name="ck_alerts_status"),
        Index("ix_alerts_created", "created_at"),
        Index("ix_alerts_customer", "customer_id"),
        Index("ix_alerts_severity_status", "severity", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    transaction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("transactions.id", ondelete="CASCADE"), unique=True
    )
    customer_id: Mapped[str] = mapped_column(String(64))
    counterparty_id: Mapped[str] = mapped_column(String(64))
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    score: Mapped[float] = mapped_column(Float)
    severity: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default=AlertStatus.OPEN.value)
    reasons: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    model_version: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
