"""Contratos da API (Pydantic v2)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from fraud.domain import (
    CATEGORIES,
    AlertSeverity,
    AlertStatus,
    Channel,
    Decision,
    TransactionStatus,
)

EntityId = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:\-]+$")]


class TransactionIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    customer_id: EntityId = Field(description="Quem paga", examples=["c000042"])
    counterparty_id: EntityId = Field(description="Quem recebe: lojista ou conta de destino")
    amount: Decimal = Field(gt=0, le=Decimal("10000000"), decimal_places=2, examples=["129.90"])
    currency: str = Field(default="BRL", pattern=r"^[A-Z]{3}$")
    channel: Channel
    category: str = Field(default="other", description=f"Uma de: {', '.join(CATEGORIES)}")
    device_id: EntityId
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    occurred_at: AwareDatetime | None = Field(
        default=None, description="Tempo do evento, com fuso. Padrão: agora."
    )

    @field_validator("category")
    @classmethod
    def _known_category(cls, value: str) -> str:
        if value not in CATEGORIES:
            raise ValueError(f"categoria desconhecida; use uma de {list(CATEGORIES)}")
        return value

    def event_time(self) -> datetime:
        return (self.occurred_at or datetime.now(UTC)).astimezone(UTC)


class Reason(BaseModel):
    feature: str
    value: float
    contribution: float = Field(description="Em log-odds; positivo empurra para fraude")
    description: str


class TransactionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    customer_id: str
    counterparty_id: str
    amount: Decimal
    currency: str
    channel: Channel
    category: str
    device_id: str
    latitude: float
    longitude: float
    occurred_at: datetime
    status: TransactionStatus
    score: float | None = None
    decision: Decision | None = None
    model_version: str | None = None
    explanation: list[Reason] | None = None
    error: str | None = None
    created_at: datetime
    processed_at: datetime | None = None


class AlertOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    transaction_id: uuid.UUID
    customer_id: str
    counterparty_id: str
    amount: Decimal
    score: float
    severity: AlertSeverity
    status: AlertStatus
    reasons: list[Reason]
    model_version: str
    created_at: datetime


class AlertPage(BaseModel):
    items: list[AlertOut]
    total: int
    limit: int
    offset: int


class HealthOut(BaseModel):
    status: str
    checks: dict[str, str]


class StatsOut(BaseModel):
    window_minutes: int
    transactions: dict[str, int]
    decisions: dict[str, int]
    alerts: dict[str, int]
    decision_latency_ms: dict[str, float | None]
    queue: dict[str, int]


class ErrorBody(BaseModel):
    code: str
    message: str
    details: Any | None = None
    request_id: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody
