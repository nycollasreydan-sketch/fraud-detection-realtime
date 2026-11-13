"""Feature store online: estados em Redis, como cache do que o PostgreSQL já sabe (ADR 008)."""

from __future__ import annotations

import json

from redis.asyncio import Redis

from fraud.features.state import CounterpartyState, CustomerState


def customer_key(customer_id: str) -> str:
    return f"state:c:{customer_id}"


def counterparty_key(counterparty_id: str) -> str:
    return f"state:cp:{counterparty_id}"


class StateStore:
    def __init__(self, redis: Redis, ttl_s: int) -> None:
        self._redis = redis
        self._ttl = ttl_s

    async def load(
        self, customer_id: str, counterparty_id: str
    ) -> tuple[CustomerState | None, CounterpartyState | None]:
        """Uma ida ao Redis para os dois estados. `None` significa cache miss."""
        raw_c, raw_cp = await self._redis.mget(
            customer_key(customer_id), counterparty_key(counterparty_id)
        )
        cs = CustomerState.from_dict(json.loads(raw_c)) if raw_c else None
        cps = CounterpartyState.from_dict(json.loads(raw_cp)) if raw_cp else None
        return cs, cps

    async def save(
        self, customer_id: str, cs: CustomerState, counterparty_id: str, cps: CounterpartyState
    ) -> None:
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.set(customer_key(customer_id), json.dumps(cs.to_dict()), ex=self._ttl)
            pipe.set(counterparty_key(counterparty_id), json.dumps(cps.to_dict()), ex=self._ttl)
            await pipe.execute()
