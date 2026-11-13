"""Worker assíncrono: consome o Redis Stream e trata falhas (ADR 009).

Três mecanismos, cada um para um tipo de falha:

* erro transitório no processamento → novas tentativas em processo, com backoff exponencial;
  esgotou → dead-letter;
* worker morreu com mensagens sem ACK → outro worker assume com XAUTOCLAIM; mensagem
  entregue vezes demais (derruba quem a pega) → dead-letter;
* API gravou no banco mas não conseguiu publicar → o sweeper republica pendentes antigos.
"""

from __future__ import annotations

import asyncio
import os
import signal
import socket
from datetime import UTC, datetime, timedelta
from typing import Any

from opentelemetry import trace
from prometheus_client import start_http_server
from redis.asyncio import Redis
from sqlalchemy import select, update

from fraud import streams
from fraud.config import Settings, get_settings
from fraud.db.models import Transaction
from fraud.db.session import SessionFactory, create_engine, create_session_factory
from fraud.domain import TransactionStatus
from fraud.ml.drift import ScoreDriftMonitor
from fraud.ml.model import FraudModel
from fraud.observability import metrics
from fraud.observability.logging import configure_logging, get_logger
from fraud.observability.tracing import (
    configure_tracing,
    extract_context,
    get_tracer,
    instrument_engine,
    instrument_redis,
)
from fraud.worker.processor import PermanentError, Processor

log = get_logger(__name__)
tracer = get_tracer(__name__)


class Worker:
    def __init__(
        self,
        settings: Settings,
        redis: Redis,
        sessions: SessionFactory,
        processor: Processor,
        consumer_name: str | None = None,
        drift_monitor: ScoreDriftMonitor | None = None,
    ) -> None:
        self.settings = settings
        self.redis = redis
        self.sessions = sessions
        self.processor = processor
        self.consumer = consumer_name or f"{socket.gethostname()}-{os.getpid()}"
        self.drift_monitor = drift_monitor

    # ------------------------------------------------------------------ uma mensagem
    async def handle(self, message_id: str, fields: dict[str, str], deliveries: int = 1) -> str:
        """Processa uma mensagem até um desfecho definitivo. Devolve scored|skipped|dlq."""
        s = self.settings
        with tracer.start_as_current_span(
            "worker.process_transaction",
            context=extract_context(fields),
            kind=trace.SpanKind.CONSUMER,
            attributes={
                "messaging.system": "redis",
                "messaging.destination.name": s.stream_name,
                "messaging.message.id": message_id,
                "fraud.transaction_id": fields.get("transaction_id", ""),
            },
        ) as span:
            transaction_id = fields.get("transaction_id")
            if not transaction_id:
                return await self._dead_letter(
                    message_id, fields, "mensagem sem transaction_id", deliveries
                )
            if deliveries > s.worker_max_deliveries:
                return await self._dead_letter(
                    message_id, fields, f"entregue {deliveries} vezes sem ACK", deliveries
                )
            for attempt in range(1, s.worker_max_attempts + 1):
                try:
                    result = await self.processor.process(transaction_id)
                except PermanentError as exc:
                    span.record_exception(exc)
                    return await self._dead_letter(message_id, fields, str(exc), deliveries)
                except Exception as exc:
                    span.record_exception(exc)
                    if attempt == s.worker_max_attempts:
                        log.error(
                            "processing_failed", transaction_id=transaction_id, attempts=attempt,
                            error=repr(exc),
                        )  # fmt: skip
                        return await self._dead_letter(message_id, fields, repr(exc), deliveries)
                    metrics.WORKER_FAILURES.labels(outcome="retry").inc()
                    delay = s.worker_backoff_base_s * 2 ** (attempt - 1)
                    log.warning(
                        "processing_retry", transaction_id=transaction_id, attempt=attempt,
                        retry_in_s=delay, error=repr(exc),
                    )  # fmt: skip
                    await asyncio.sleep(delay)
                else:
                    await self.redis.xack(s.stream_name, s.consumer_group, message_id)
                    span.set_attribute("fraud.outcome", result.outcome)
                    if result.decision is not None:
                        span.set_attribute("fraud.decision", result.decision.value)
                    return result.outcome
        raise AssertionError("inalcançável")  # pragma: no cover

    async def _dead_letter(
        self, message_id: str, fields: dict[str, str], error: str, deliveries: int
    ) -> str:
        s = self.settings
        await streams.publish_dead_letter(self.redis, s, fields, error, deliveries)
        transaction_id = fields.get("transaction_id")
        if transaction_id:
            try:
                async with self.sessions() as session:
                    await session.execute(
                        update(Transaction)
                        .where(
                            Transaction.id == transaction_id,
                            Transaction.status == TransactionStatus.PENDING.value,
                        )
                        .values(status=TransactionStatus.FAILED.value, error=error[:2000])
                    )
                    await session.commit()
            except Exception as exc:  # o banco pode ser justamente o que está fora
                log.warning("mark_failed_error", transaction_id=transaction_id, error=repr(exc))
        await self.redis.xack(s.stream_name, s.consumer_group, message_id)
        metrics.WORKER_FAILURES.labels(outcome="dlq").inc()
        log.error(
            "dead_lettered", transaction_id=transaction_id, message_id=message_id, error=error
        )
        return "dlq"

    # ------------------------------------------------------------------ laços
    async def poll_once(self, block_ms: int | None = None) -> int:
        """Lê um lote de mensagens novas e processa em ordem. Devolve quantas tratou."""
        s = self.settings
        response: Any = await self.redis.xreadgroup(
            s.consumer_group,
            self.consumer,
            {s.stream_name: ">"},
            count=s.worker_batch_size,
            block=s.worker_block_ms if block_ms is None else block_ms,
        )
        handled = 0
        for _stream, messages in response or []:
            for message_id, fields in messages:
                await self.handle(message_id, fields)
                handled += 1
        return handled

    async def claim_stale(self) -> int:
        """Assume mensagens paradas com consumidores que sumiram."""
        s = self.settings
        result: Any = await self.redis.xautoclaim(
            s.stream_name, s.consumer_group, self.consumer,
            min_idle_time=s.worker_claim_idle_ms, start_id="0-0", count=s.worker_batch_size,
        )  # fmt: skip
        claimed = result[1]
        for message_id, fields in claimed:
            if fields is None:  # mensagem já removida do stream
                await self.redis.xack(s.stream_name, s.consumer_group, message_id)
                continue
            pending = await self.redis.xpending_range(
                s.stream_name, s.consumer_group, min=message_id, max=message_id, count=1
            )
            deliveries = int(pending[0]["times_delivered"]) if pending else 1
            metrics.WORKER_FAILURES.labels(outcome="claimed").inc()
            await self.handle(message_id, fields, deliveries=deliveries)
        return len(claimed)

    async def sweep_pending(self) -> int:
        """Republica transações `pending` antigas (publicação que falhou na API).

        Só age com a fila drenada. Enquanto há mensagens por entregar, uma transação
        `pending` antiga quase certamente está apenas esperando a vez; republicá-la seria
        trabalho duplicado justamente quando o sistema está sob carga. (Foi o que a
        primeira versão fazia: a demo em lote mostrou 15% de reentregas inúteis.)
        """
        s = self.settings
        if await streams.group_lag(self.redis, s) > 0:
            return 0
        cutoff = datetime.now(UTC) - timedelta(seconds=s.sweeper_pending_age_s)
        async with self.sessions() as session:
            ids = (
                await session.scalars(
                    select(Transaction.id)
                    .where(
                        Transaction.status == TransactionStatus.PENDING.value,
                        Transaction.created_at < cutoff,
                    )
                    .order_by(Transaction.created_at)
                    .limit(s.sweeper_batch)
                )
            ).all()
        republished = 0
        for tx_id in ids:
            # guarda para não republicar a mesma transação a cada ciclo enquanto há fila
            guard_ttl = max(int(s.sweeper_pending_age_s * 5), 1)
            if await self.redis.set(f"sweeper:{tx_id}", "1", nx=True, ex=guard_ttl):
                await streams.publish(self.redis, s, str(tx_id))
                republished += 1
        if republished:
            metrics.SWEEPER_REPUBLISHED.inc(republished)
            log.warning("sweeper_republished", count=republished)
        return republished

    async def update_gauges(self) -> None:
        s = self.settings
        for group in await self.redis.xinfo_groups(s.stream_name):
            if group.get("name") == s.consumer_group:
                metrics.STREAM_PENDING.set(group.get("pending") or 0)
                metrics.STREAM_LAG.set(group.get("lag") or 0)
        if self.drift_monitor is not None:
            value = self.drift_monitor.value()
            if value is not None:
                metrics.SCORE_PSI.set(value)

    async def maintenance(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.claim_stale()
                await self.sweep_pending()
                await self.update_gauges()
            except Exception as exc:
                log.warning("maintenance_error", error=repr(exc))
            try:
                await asyncio.wait_for(stop.wait(), self.settings.worker_maintenance_interval_s)
            except TimeoutError:
                continue

    async def run(self, stop: asyncio.Event) -> None:
        await streams.ensure_group(self.redis, self.settings)
        log.info("worker_started", consumer=self.consumer)
        maintenance = asyncio.create_task(self.maintenance(stop))
        try:
            while not stop.is_set():
                try:
                    await self.poll_once()
                except Exception as exc:  # Redis fora do ar: espera e tenta de novo
                    log.error("poll_error", error=repr(exc))
                    await asyncio.sleep(1.0)
        finally:
            await maintenance
            log.info("worker_stopped", consumer=self.consumer)


async def main() -> None:
    settings = get_settings()
    service = "fraud-worker"
    configure_logging(service, settings.log_level, settings.log_json)
    configure_tracing(service, settings.otlp_endpoint)
    instrument_redis()

    model = FraudModel.load(settings.model_dir)
    metrics.MODEL_INFO.info(
        {"version": model.version, "feature_version": str(model.feature_version)}
    )
    log.info("model_loaded", version=model.version, features=len(model.model_features))

    engine = create_engine(settings)
    instrument_engine(engine)
    sessions = create_session_factory(engine)
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    drift_monitor = ScoreDriftMonitor(model.score_edges, model.score_expected)
    processor = Processor(settings, redis, sessions, model, drift_monitor)
    worker = Worker(settings, redis, sessions, processor, drift_monitor=drift_monitor)

    start_http_server(settings.worker_metrics_port)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        await worker.run(stop)
    finally:
        await redis.aclose()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
