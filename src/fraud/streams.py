"""Fila em Redis Streams (ADR 002).

A mensagem carrega só o id da transação (o banco é a fonte da verdade) e o contexto de
trace. Consumer group dá entrega at-least-once com ACK explícito.
"""

from __future__ import annotations

import time

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from fraud.config import Settings


async def ensure_group(redis: Redis, settings: Settings) -> None:
    """Cria stream e consumer group se ainda não existirem (idempotente)."""
    try:
        await redis.xgroup_create(
            settings.stream_name, settings.consumer_group, id="0", mkstream=True
        )
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


async def publish(
    redis: Redis, settings: Settings, transaction_id: str, carrier: dict[str, str] | None = None
) -> str:
    fields: dict[str, str] = {
        "transaction_id": transaction_id,
        "enqueued_at": f"{time.time():.6f}",
        **(carrier or {}),
    }
    message_id = await redis.xadd(
        settings.stream_name,
        fields,  # type: ignore[arg-type]
        maxlen=settings.stream_maxlen,
        approximate=True,
    )
    return str(message_id)


async def publish_dead_letter(
    redis: Redis, settings: Settings, fields: dict[str, str], error: str, deliveries: int
) -> str:
    payload: dict[str, str] = {
        **fields,
        "error": error[:2000],
        "deliveries": str(deliveries),
        "failed_at": f"{time.time():.6f}",
    }
    message_id = await redis.xadd(settings.dlq_stream, payload, maxlen=50_000, approximate=True)  # type: ignore[arg-type]
    return str(message_id)


async def group_stats(redis: Redis, settings: Settings) -> dict[str, int]:
    """`lag`: mensagens ainda não entregues ao grupo; `pending`: entregues e sem ACK."""
    for group in await redis.xinfo_groups(settings.stream_name):
        if group.get("name") == settings.consumer_group:
            return {"lag": int(group.get("lag") or 0), "pending": int(group.get("pending") or 0)}
    return {"lag": 0, "pending": 0}


async def group_lag(redis: Redis, settings: Settings) -> int:
    return (await group_stats(redis, settings))["lag"]
