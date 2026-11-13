"""Tipos de domínio compartilhados por API, worker, features e simuladores.

`TxEvent` é a visão mínima de uma transação de que o cálculo de features precisa. É
deliberadamente independente de SQLAlchemy e de Pydantic: a mesma função de features roda
no replay offline (milhões de chamadas) e no worker online.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Channel(StrEnum):
    POS = "pos"
    ECOMMERCE = "ecommerce"
    TRANSFER = "transfer"


class TransactionStatus(StrEnum):
    PENDING = "pending"
    SCORED = "scored"
    FAILED = "failed"


class Decision(StrEnum):
    APPROVE = "approve"
    REVIEW = "review"
    BLOCK = "block"


class AlertSeverity(StrEnum):
    MEDIUM = "medium"
    HIGH = "high"


class AlertStatus(StrEnum):
    OPEN = "open"
    CONFIRMED = "confirmed"
    DISMISSED = "dismissed"


CATEGORIES: tuple[str, ...] = (
    "grocery",
    "restaurant",
    "transport",
    "fuel",
    "pharmacy",
    "fashion",
    "electronics",
    "travel",
    "services",
    "entertainment",
    "p2p",
    "other",
)

CHANNEL_CODES: dict[str, int] = {c.value: i for i, c in enumerate(Channel)}
CATEGORY_CODES: dict[str, int] = {c: i for i, c in enumerate(CATEGORIES)}
OTHER_CATEGORY_CODE = CATEGORY_CODES["other"]


@dataclass(slots=True, frozen=True)
class TxEvent:
    """Transação no formato consumido pelo cálculo de features."""

    tx_id: str
    customer_id: str
    counterparty_id: str
    amount: float
    channel: str
    category: str
    device_id: str
    lat: float
    lon: float
    ts: float  # tempo do evento, epoch em segundos (UTC)
