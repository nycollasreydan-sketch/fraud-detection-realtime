"""Gerador de transações sintéticas contra a API real.

    python -m fraud.simulation.generator --customers 100 --days 10 --attacks 2

Reaproveita o mesmo mundo sintético do treino, mas envia por HTTP: cada transação passa por
validação, banco, fila, worker e modelo. O tempo do evento (`occurred_at`) é o simulado, num
período que termina agora, então o histórico de vários dias é enviado em segundos.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime

import httpx
import numpy as np

from fraud.config import get_settings
from fraud.features.state import DAY
from fraud.simulation.attacks import ATTACKS, attack_stream
from fraud.simulation.world import LabeledTx, World


@dataclass
class Sent:
    item: LabeledTx
    server_id: str | None
    status_code: int


def build_stream(
    n_customers: int = 40,
    days: float = 31.0,
    seed: int = 2026,
    attacks_per_kind: int = 2,
    end_ts: float | None = None,
    run_id: str | None = None,
    warmup_days: float = 21.0,
    known_fraud_rate: float = 6.0,
) -> list[LabeledTx]:
    """Tráfego legítimo + fraudes conhecidas + ataques, terminando em `end_ts` (agora).

    `run_id` prefixa todos os ids: execuções repetidas não misturam histórico entre si.

    Os primeiros `warmup_days` são aquecimento: constroem o histórico de clientes e
    recebedores, como um sistema que já está no ar há semanas. Os ataques só começam
    depois. Sem isso todo recebedor seria "conta nova" e o modelo, treinado com histórico,
    veria um mundo que não conhece (o PSI dos scores acusa isso na hora).
    """
    end = (time.time() if end_ts is None else end_ts) - 60.0
    start = end - days * DAY
    world = World(n_customers=n_customers, n_merchants=max(60, n_customers // 2), seed=seed)
    stream = world.generate(days, seed, start_ts=start, known_fraud_rate=known_fraud_rate)
    if attacks_per_kind > 0:
        rng = np.random.default_rng(seed + 1)
        stream.extend(
            attack_stream(
                world, stream, ATTACKS, attacks_per_kind, rng,
                first_day=warmup_days, last_day=days, start_ts=start,
            )
        )  # fmt: skip
    stream = [x for x in stream if x.event.ts < end]
    stream.sort(key=lambda x: x.event.ts)
    prefix = run_id or uuid.uuid4().hex[:6]
    return [
        replace(
            x,
            event=replace(
                x.event,
                customer_id=f"{prefix}-{x.event.customer_id}",
                counterparty_id=f"{prefix}-{x.event.counterparty_id}",
                device_id=f"{prefix}-{x.event.device_id}",
            ),
        )
        for x in stream
    ]


def to_payload(item: LabeledTx) -> dict[str, object]:
    e = item.event
    return {
        "customer_id": e.customer_id,
        "counterparty_id": e.counterparty_id,
        "amount": f"{e.amount:.2f}",
        "currency": "BRL",
        "channel": e.channel,
        "category": e.category,
        "device_id": e.device_id,
        "latitude": e.lat,
        "longitude": e.lon,
        "occurred_at": datetime.fromtimestamp(e.ts, UTC).isoformat(),
    }


def batches(stream: list[LabeledTx], size: int) -> list[list[LabeledTx]]:
    """Lotes enviados em paralelo sem repetir cliente nem recebedor dentro do lote.

    As features dependem da ordem das transações de cada entidade; dentro de um lote a
    ordem de chegada é arbitrária, então duas transações da mesma entidade nunca dividem
    um lote.
    """
    out: list[list[LabeledTx]] = []
    current: list[LabeledTx] = []
    seen: set[str] = set()
    for item in stream:
        keys = {item.event.customer_id, item.event.counterparty_id}
        if len(current) >= size or keys & seen:
            out.append(current)
            current, seen = [], set()
        current.append(item)
        seen |= keys
    if current:
        out.append(current)
    return out


async def send_stream(
    api_url: str, stream: list[LabeledTx], concurrency: int = 16, progress: bool = True
) -> list[Sent]:
    sent: list[Sent] = []
    started = time.perf_counter()
    limits = httpx.Limits(max_connections=concurrency)
    async with httpx.AsyncClient(base_url=api_url, timeout=30.0, limits=limits) as client:

        async def post(item: LabeledTx) -> Sent:
            response = await client.post("/transactions", json=to_payload(item))
            body = response.json() if response.status_code < 500 else {}
            return Sent(item, body.get("id"), response.status_code)

        for batch in batches(stream, concurrency):
            sent.extend(await asyncio.gather(*(post(item) for item in batch)))
            if progress and len(sent) % 200 < len(batch):
                rate = len(sent) / (time.perf_counter() - started)
                print(f"\r  enviadas {len(sent)}/{len(stream)} ({rate:.0f} tx/s)", end="")
                sys.stdout.flush()
    if progress:
        print()
    return sent


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default=get_settings().api_url)
    parser.add_argument("--customers", type=int, default=40)
    parser.add_argument("--days", type=float, default=31.0)
    parser.add_argument("--attacks", type=int, default=2, help="episódios por tipo de ataque")
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--concurrency", type=int, default=16)
    args = parser.parse_args()
    stream = build_stream(args.customers, args.days, args.seed, args.attacks)
    n_fraud = sum(x.is_fraud for x in stream)
    print(f"{len(stream)} transações ({n_fraud} fraudulentas) para {args.api_url}")
    sent = asyncio.run(send_stream(args.api_url, stream, args.concurrency))
    accepted = sum(1 for s in sent if s.status_code in (200, 202))
    print(f"aceitas: {accepted}/{len(sent)}")


if __name__ == "__main__":
    main()
