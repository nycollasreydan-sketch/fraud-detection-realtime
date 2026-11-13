from __future__ import annotations

import asyncio
import json
import math
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import func, select, update

from fraud import streams
from fraud.config import Settings
from fraud.db.models import Alert, Transaction
from fraud.db.session import SessionFactory
from fraud.features.store import counterparty_key, customer_key
from fraud.ml.dataset import ReplayEngine
from fraud.ml.model import FraudModel
from fraud.simulation.generator import build_stream, to_payload
from fraud.simulation.world import LabeledTx
from fraud.worker.main import Worker
from fraud.worker.processor import PermanentError, Processor
from tests.integration.conftest import payload


async def post(client: httpx.AsyncClient, **overrides: object) -> str:
    response = await client.post("/transactions", json=payload(**overrides))
    assert response.status_code == 202, response.text
    return str(response.json()["id"])


async def drain(worker: Worker) -> int:
    total = 0
    while handled := await worker.poll_once(block_ms=10):
        total += handled
    return total


async def pending_count(redis: Redis, settings: Settings) -> int:
    info = await redis.xpending(settings.stream_name, settings.consumer_group)
    return int(info["pending"])


# ---------------------------------------------------------------- caminho feliz
async def test_end_to_end_scoring(
    client: httpx.AsyncClient, worker: Worker, redis: Redis, settings: Settings, model: FraudModel
) -> None:
    tx_id = await post(client)
    assert await drain(worker) == 1

    body = (await client.get(f"/transactions/{tx_id}")).json()
    assert body["status"] == "scored"
    assert 0.0 <= body["score"] <= 1.0
    assert body["decision"] in {"approve", "review", "block"}
    assert body["model_version"] == model.version
    assert body["processed_at"] is not None
    assert len(body["explanation"]) == 5
    assert {"feature", "value", "contribution", "description"} <= set(body["explanation"][0])

    assert await pending_count(redis, settings) == 0  # ACK feito
    state = json.loads(await redis.get(customer_key("c1")))
    assert state["n"] == 1
    assert tx_id in state["applied"]
    assert json.loads(await redis.get(counterparty_key("m1")))["n"] == 1
    assert await redis.ttl(customer_key("c1")) > 0


async def test_suspicious_transaction_creates_alert(
    client: httpx.AsyncClient, worker: Worker, model: FraudModel, sessions: SessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    # limiares no chão: qualquer score vira bloqueio; o que se testa é o encanamento do alerta
    monkeypatch.setattr(model, "review_threshold", 0.0)
    monkeypatch.setattr(model, "block_threshold", 0.0)
    tx_id = await post(client, amount="4321.00")
    await drain(worker)

    alerts = (await client.get("/alerts")).json()
    assert alerts["total"] == 1
    alert = alerts["items"][0]
    assert alert["transaction_id"] == tx_id
    assert alert["severity"] == "high"
    assert alert["status"] == "open"
    assert alert["amount"] == "4321.00"
    assert all(r["contribution"] > 0 for r in alert["reasons"])  # só o que empurra para fraude
    assert (await client.get(f"/transactions/{tx_id}")).json()["decision"] == "block"


async def test_legit_transaction_creates_no_alert(
    client: httpx.AsyncClient, worker: Worker, model: FraudModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(model, "review_threshold", 1.1)
    monkeypatch.setattr(model, "block_threshold", 1.1)
    await post(client)
    await drain(worker)
    assert (await client.get("/alerts")).json()["total"] == 0


async def test_history_features_accumulate_across_transactions(
    client: httpx.AsyncClient, worker: Worker, sessions: SessionFactory
) -> None:
    base = datetime.now(UTC) - timedelta(hours=1)
    ids = []
    for i in range(4):
        when = (base + timedelta(minutes=i)).isoformat()
        ids.append(await post(client, occurred_at=when, device_id="d1" if i < 3 else "d-novo"))
    await drain(worker)
    async with sessions() as session:
        rows = [await session.get(Transaction, uuid.UUID(i)) for i in ids]
    counts = [r.features["c_tx_count"] for r in rows if r and r.features]
    assert counts == pytest.approx([math.log1p(i) for i in range(4)])
    assert rows[3] is not None
    assert rows[3].features is not None
    assert rows[3].features["c_new_device"] == 1.0
    assert rows[3].features["cp_tx_count"] == pytest.approx(math.log1p(3))
    assert "anomaly_score" in rows[3].features


async def test_online_features_match_offline_replay(
    client: httpx.AsyncClient, worker: Worker, sessions: SessionFactory
) -> None:
    """Paridade treino/serviço de ponta a ponta: as features gravadas pelo worker (HTTP →
    banco → fila → Redis) são as mesmas do replay offline usado no treino."""
    end = datetime.now(UTC).timestamp()
    stream: list[LabeledTx] = build_stream(8, 3, seed=5, attacks_per_kind=1, end_ts=end)[:150]
    engine = ReplayEngine()
    for item in stream:
        response = await client.post("/transactions", json=to_payload(item))
        assert response.status_code == 202
        await drain(worker)
        async with sessions() as session:
            row = await session.get(Transaction, uuid.UUID(response.json()["id"]))
        assert row is not None
        assert row.features is not None
        # o replay offline usa o que o banco guardou (valor com 2 casas, ts em microssegundos)
        offline = engine.step(LabeledTx(row.to_event()))
        online = {k: v for k, v in row.features.items() if k != "anomaly_score"}
        assert online == pytest.approx(offline, rel=1e-9, abs=1e-9)


# ---------------------------------------------------------------- cache-aside
async def test_state_is_rebuilt_from_postgres_after_cache_loss(
    client: httpx.AsyncClient, worker: Worker, redis: Redis, sessions: SessionFactory
) -> None:
    base = datetime.now(UTC) - timedelta(hours=2)
    for i in range(5):
        await post(client, occurred_at=(base + timedelta(minutes=i)).isoformat())
    await drain(worker)
    before = json.loads(await redis.get(customer_key("c1")))

    await redis.delete(customer_key("c1"), counterparty_key("m1"))  # Redis "perdeu" o estado
    tx_id = await post(client, occurred_at=(base + timedelta(minutes=10)).isoformat())
    await drain(worker)

    async with sessions() as session:
        row = await session.get(Transaction, uuid.UUID(tx_id))
    assert row is not None
    assert row.features is not None
    assert row.features["c_tx_count"] == pytest.approx(math.log1p(5))
    assert row.features["cp_tx_count"] == pytest.approx(math.log1p(5))
    after = json.loads(await redis.get(customer_key("c1")))
    assert after["n"] == before["n"] + 1


# ---------------------------------------------------------------- idempotência
async def test_redelivery_does_not_double_count(
    client: httpx.AsyncClient, worker: Worker, redis: Redis, settings: Settings,
    model: FraudModel, monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    monkeypatch.setattr(model, "review_threshold", 0.0)
    tx_id = await post(client)
    await drain(worker)
    # a mesma transação chega de novo (reentrega, sweeper, duplicata)
    await streams.publish(redis, settings, tx_id)
    await streams.publish(redis, settings, tx_id)
    assert await drain(worker) == 2

    assert (await client.get("/alerts")).json()["total"] == 1
    assert json.loads(await redis.get(customer_key("c1")))["n"] == 1
    assert json.loads(await redis.get(counterparty_key("m1")))["n"] == 1
    assert await pending_count(redis, settings) == 0


async def test_redelivery_repairs_state_lost_between_commit_and_cache_write(
    client: httpx.AsyncClient, worker: Worker, redis: Redis, settings: Settings
) -> None:
    """Se o worker morre depois do commit e antes de gravar o Redis, a reentrega encontra
    a transação já decidida e apenas completa o estado."""
    tx_id = await post(client)
    await drain(worker)
    await redis.set(customer_key("c1"), json.dumps({"n": 0}))  # estado sem a transação
    await streams.publish(redis, settings, tx_id)
    await drain(worker)
    state = json.loads(await redis.get(customer_key("c1")))
    assert state["n"] == 1
    assert tx_id in state["applied"]


# ---------------------------------------------------------------- falhas
class Flaky(Processor):
    """Falha as primeiras `failures` chamadas e depois se comporta normalmente."""

    failures = 0
    calls = 0

    async def process(self, transaction_id: str):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self.calls <= self.failures:
            raise ConnectionError("falha transitória simulada")
        return await super().process(transaction_id)


@pytest.fixture
def flaky(settings: Settings, redis: Redis, sessions: SessionFactory, model: FraudModel) -> Flaky:
    return Flaky(settings, redis, sessions, model)


async def test_transient_failure_is_retried(
    client: httpx.AsyncClient, flaky: Flaky, redis: Redis, sessions: SessionFactory,
    settings: Settings,
) -> None:  # fmt: skip
    flaky.failures = 2  # max_attempts = 3: a terceira tentativa passa
    worker = Worker(settings, redis, sessions, flaky, consumer_name="flaky")
    tx_id = await post(client)
    await drain(worker)
    assert flaky.calls == 3
    assert (await client.get(f"/transactions/{tx_id}")).json()["status"] == "scored"
    assert await redis.xlen(settings.dlq_stream) == 0


async def test_exhausted_retries_go_to_dead_letter(
    client: httpx.AsyncClient, flaky: Flaky, redis: Redis, sessions: SessionFactory,
    settings: Settings,
) -> None:  # fmt: skip
    flaky.failures = 99
    worker = Worker(settings, redis, sessions, flaky, consumer_name="flaky")
    tx_id = await post(client)
    await drain(worker)

    assert flaky.calls == settings.worker_max_attempts
    dead = await redis.xrange(settings.dlq_stream)
    assert len(dead) == 1
    assert dead[0][1]["transaction_id"] == tx_id
    assert "falha transitória simulada" in dead[0][1]["error"]
    body = (await client.get(f"/transactions/{tx_id}")).json()
    assert body["status"] == "failed"
    assert "falha transitória" in body["error"]
    assert await pending_count(redis, settings) == 0  # não trava a fila

    # depois da DLQ o worker continua saudável para a próxima transação
    flaky.failures = 0
    ok_id = await post(client, customer_id="c2")
    await drain(worker)
    assert (await client.get(f"/transactions/{ok_id}")).json()["status"] == "scored"


async def test_poison_messages_go_straight_to_dead_letter(
    worker: Worker, redis: Redis, settings: Settings, processor: Processor
) -> None:
    await redis.xadd(settings.stream_name, {"transaction_id": "isto-nao-e-uuid"})
    await redis.xadd(settings.stream_name, {"transaction_id": str(uuid.uuid4())})  # não existe
    await redis.xadd(settings.stream_name, {"outro_campo": "x"})
    assert await drain(worker) == 3
    errors = [fields["error"] for _, fields in await redis.xrange(settings.dlq_stream)]
    assert len(errors) == 3
    assert "id inválido" in errors[0]
    assert "não existe" in errors[1]
    assert "sem transaction_id" in errors[2]
    assert await pending_count(redis, settings) == 0
    with pytest.raises(PermanentError):
        await processor.process("x")


async def test_stale_message_is_claimed_from_dead_consumer(
    client: httpx.AsyncClient, worker: Worker, redis: Redis, settings: Settings
) -> None:
    tx_id = await post(client)
    # outro consumidor leu a mensagem e morreu sem dar ACK
    await redis.xreadgroup(settings.consumer_group, "morto", {settings.stream_name: ">"}, count=10)
    assert await pending_count(redis, settings) == 1
    assert await drain(worker) == 0  # para leituras normais, a mensagem não existe mais

    await asyncio.sleep(settings.worker_claim_idle_ms / 1000 + 0.05)
    assert await worker.claim_stale() == 1
    assert (await client.get(f"/transactions/{tx_id}")).json()["status"] == "scored"
    assert await pending_count(redis, settings) == 0


async def test_message_delivered_too_many_times_is_dead_lettered(
    client: httpx.AsyncClient, worker: Worker, redis: Redis, settings: Settings
) -> None:
    tx_id = await post(client)
    for consumer in ("a", "b", "c"):  # três consumidores morreram com ela
        await redis.xreadgroup(
            settings.consumer_group, consumer, {settings.stream_name: ">"}, count=1
        )
        await asyncio.sleep(0.06)
        await redis.xautoclaim(
            settings.stream_name,
            settings.consumer_group,
            consumer,
            min_idle_time=50,
            start_id="0-0",
        )
    await asyncio.sleep(0.06)
    await worker.claim_stale()
    dead = await redis.xrange(settings.dlq_stream)
    assert len(dead) == 1
    assert "sem ACK" in dead[0][1]["error"]
    assert (await client.get(f"/transactions/{tx_id}")).json()["status"] == "failed"


async def test_sweeper_republishes_orphaned_pending_transactions(
    client: httpx.AsyncClient, worker: Worker, redis: Redis, settings: Settings,
    sessions: SessionFactory,
) -> None:  # fmt: skip
    tx_id = await post(client)
    await redis.delete(settings.stream_name)  # a publicação "se perdeu"
    await streams.ensure_group(redis, settings)
    async with sessions() as session:
        await session.execute(
            update(Transaction)
            .where(Transaction.id == uuid.UUID(tx_id))
            .values(created_at=datetime.now(UTC) - timedelta(minutes=5))
        )
        await session.commit()

    # com fila por entregar, o sweeper não age: a transação pode só estar esperando a vez
    other = await post(client, customer_id="c2")
    assert await worker.sweep_pending() == 0
    await drain(worker)
    assert (await client.get(f"/transactions/{other}")).json()["status"] == "scored"

    assert await worker.sweep_pending() == 1
    assert await worker.sweep_pending() == 0  # guarda: não republica a cada ciclo
    await drain(worker)
    assert (await client.get(f"/transactions/{tx_id}")).json()["status"] == "scored"


async def test_concurrent_workers_keep_per_customer_state_consistent(
    client: httpx.AsyncClient, settings: Settings, redis: Redis, sessions: SessionFactory,
    model: FraudModel,
) -> None:  # fmt: skip
    """Dois workers no mesmo grupo, 40 transações do mesmo cliente: o lock por entidade
    garante que nenhuma atualização de estado se perde."""
    for _ in range(40):
        await post(client)
    workers = [
        Worker(settings, redis, sessions, Processor(settings, redis, sessions, model), f"w{i}")
        for i in range(2)
    ]
    await asyncio.gather(*(drain(w) for w in workers))
    await asyncio.gather(*(drain(w) for w in workers))

    async with sessions() as session:
        scored = await session.scalar(
            select(func.count()).select_from(Transaction).where(Transaction.status == "scored")
        )
        alerts = await session.scalar(select(func.count()).select_from(Alert))
    assert scored == 40
    assert json.loads(await redis.get(customer_key("c1")))["n"] == 40
    assert json.loads(await redis.get(counterparty_key("m1")))["n"] == 40
    assert alerts is not None
    assert alerts <= 40


async def test_worker_gauges(client: httpx.AsyncClient, worker: Worker) -> None:
    from fraud.observability import metrics

    await post(client)
    await worker.update_gauges()
    assert metrics.STREAM_LAG._value.get() == 1
    await drain(worker)
    await worker.update_gauges()
    assert metrics.STREAM_LAG._value.get() == 0
    assert metrics.STREAM_PENDING._value.get() == 0
