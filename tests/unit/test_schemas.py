from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from fraud.api.schemas import TransactionIn

VALID = {
    "customer_id": "c1",
    "counterparty_id": "m1",
    "amount": "10.50",
    "channel": "pos",
    "device_id": "d1",
    "latitude": -23.5,
    "longitude": -46.6,
}


def test_valid_payload_gets_defaults() -> None:
    tx = TransactionIn.model_validate(VALID)
    assert tx.amount == Decimal("10.50")
    assert tx.currency == "BRL"
    assert tx.category == "other"
    assert tx.event_time().tzinfo is UTC


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("amount", "0"),
        ("amount", "-1"),
        ("amount", "10.999"),  # mais de duas casas
        ("amount", "99999999999"),
        ("channel", "boleto"),
        ("category", "desconhecida"),
        ("currency", "reais"),
        ("latitude", 91),
        ("longitude", -181),
        ("customer_id", ""),
        ("customer_id", "com espaço"),
        ("device_id", "x" * 65),
        ("occurred_at", "2026-01-01T10:00:00"),  # sem fuso: ambíguo, rejeitado
    ],
)
def test_invalid_fields_are_rejected(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        TransactionIn.model_validate({**VALID, field: value})


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        TransactionIn.model_validate({**VALID, "is_fraud": True})


def test_event_time_is_normalised_to_utc() -> None:
    tx = TransactionIn.model_validate({**VALID, "occurred_at": "2026-01-01T10:00:00-03:00"})
    assert tx.event_time() == datetime(2026, 1, 1, 13, 0, tzinfo=UTC)
