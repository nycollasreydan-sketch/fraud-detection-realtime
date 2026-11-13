"""Processamento de UMA transação: estado → features → modelo → banco → alerta.

Separado do laço de consumo (`worker/main.py`) para ser testável sem fila.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

from redis.asyncio import Redis
from redis.exceptions import LockError
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from fraud.config import Settings
from fraud.db.models import Alert, Transaction
from fraud.db.session import SessionFactory
from fraud.domain import AlertSeverity, Decision, TransactionStatus, TxEvent
from fraud.features.compute import (
    apply_transaction,
    compute_features,
    update_counterparty,
    update_customer,
)
from fraud.features.state import CounterpartyState, CustomerState
from fraud.features.store import StateStore
from fraud.ml.drift import ScoreDriftMonitor
from fraud.ml.model import FraudModel
from fraud.observability import metrics
from fraud.observability.logging import get_logger
from fraud.observability.tracing import get_tracer

log = get_logger(__name__)
tracer = get_tracer(__name__)


class PermanentError(Exception):
    """Erro que não adianta tentar de novo: vai direto para a DLQ."""


class LockTimeout(Exception):
    """Não conseguiu o lock da entidade a tempo; é transitório, vale nova tentativa."""


@dataclass(frozen=True)
class ProcessResult:
    transaction_id: str
    outcome: str  # scored | skipped
    decision: Decision | None = None
    score: float | None = None


@contextmanager
def stage(name: str) -> Iterator[None]:
    """Mede uma etapa: vira span no trace e observação no histograma."""
    start = time.perf_counter()
    with tracer.start_as_current_span(f"worker.{name}"):
        try:
            yield
        finally:
            metrics.STAGE_DURATION.labels(stage=name).observe(time.perf_counter() - start)


class Processor:
    def __init__(
        self,
        settings: Settings,
        redis: Redis,
        sessions: SessionFactory,
        model: FraudModel,
        drift_monitor: ScoreDriftMonitor | None = None,
    ) -> None:
        self._settings = settings
        self._redis = redis
        self._sessions = sessions
        self._model = model
        self._store = StateStore(redis, settings.state_ttl_s)
        self._drift = drift_monitor

    # ------------------------------------------------------------------ locks
    @asynccontextmanager
    async def _entity_locks(self, customer_id: str, counterparty_id: str) -> AsyncIterator[None]:
        """Locks por entidade, sempre em ordem lexicográfica (sem deadlock)."""
        names = sorted({f"lock:c:{customer_id}", f"lock:cp:{counterparty_id}"})
        async with AsyncExitStack() as stack:
            for name in names:
                lock = self._redis.lock(
                    name,
                    timeout=self._settings.lock_timeout_s,
                    blocking_timeout=self._settings.lock_wait_s,
                )
                try:
                    await stack.enter_async_context(lock)
                except LockError as exc:
                    raise LockTimeout(name) from exc
            yield

    # ------------------------------------------------------------------ estado
    async def _load_states(
        self, session: AsyncSession, event: TxEvent
    ) -> tuple[CustomerState, CounterpartyState]:
        cs, cps = await self._store.load(event.customer_id, event.counterparty_id)
        metrics.STATE_CACHE.labels("customer", "hit" if cs else "miss").inc()
        metrics.STATE_CACHE.labels("counterparty", "hit" if cps else "miss").inc()
        if cs is None:
            cs = CustomerState()
            for past in await self._history(session, event, by_customer=True):
                update_customer(cs, past)
        if cps is None:
            cps = CounterpartyState()
            for past in await self._history(session, event, by_customer=False):
                update_counterparty(cps, past)
        return cs, cps

    async def _history(
        self, session: AsyncSession, event: TxEvent, *, by_customer: bool
    ) -> list[TxEvent]:
        """Cache miss: transações já decididas da entidade, da mais antiga para a mais nova.

        Na imensa maioria dos casos a entidade é nova e a consulta volta vazia (um index
        scan). Quando o Redis foi reiniciado, é isso que impede o modelo de ficar cego.
        """
        t = Transaction
        key = (
            t.customer_id == event.customer_id
            if by_customer
            else (t.counterparty_id == event.counterparty_id)
        )
        stmt = (
            select(
                t.id, t.customer_id, t.counterparty_id, t.amount, t.channel, t.category,
                t.device_id, t.latitude, t.longitude, t.occurred_at,
            )
            .where(key, t.status == TransactionStatus.SCORED.value, t.id != uuid.UUID(event.tx_id))
            .order_by(t.occurred_at.desc())
            .limit(self._settings.rebuild_history_limit)
        )  # fmt: skip
        rows = (await session.execute(stmt)).all()
        return [
            TxEvent(
                tx_id=str(r.id),
                customer_id=r.customer_id,
                counterparty_id=r.counterparty_id,
                amount=float(r.amount),
                channel=r.channel,
                category=r.category,
                device_id=r.device_id,
                lat=r.latitude,
                lon=r.longitude,
                ts=r.occurred_at.timestamp(),
            )
            for r in reversed(rows)
        ]

    # ------------------------------------------------------------------ processamento
    async def process(self, transaction_id: str) -> ProcessResult:
        try:
            tx_uuid = uuid.UUID(transaction_id)
        except ValueError as exc:
            raise PermanentError(f"id inválido: {transaction_id!r}") from exc

        started = time.perf_counter()
        async with self._sessions() as session:
            # Commit assíncrono só nesta transação: economiza o fsync (a etapa mais cara do
            # worker). Se o PostgreSQL cair e perder o commit, a linha volta a `pending` e o
            # sweeper a republica; nada é decidido em silêncio nem perdido.
            await session.execute(text("SET LOCAL synchronous_commit = off"))
            row = await session.get(Transaction, tx_uuid)
            if row is None:
                raise PermanentError(f"transação {transaction_id} não existe no banco")

            lock_started = time.perf_counter()
            async with self._entity_locks(row.customer_id, row.counterparty_id):
                metrics.STAGE_DURATION.labels(stage="lock").observe(
                    time.perf_counter() - lock_started
                )
                # relê com lock de linha: outro worker pode ter decidido nesse meio-tempo
                await session.refresh(row, with_for_update=True)
                event = row.to_event()
                with stage("state"):
                    cs, cps = await self._load_states(session, event)

                if row.status == TransactionStatus.SCORED.value:
                    # reentrega: nada a decidir, só garante que o estado contém a transação
                    changed_c = update_customer(cs, event)
                    changed_cp = update_counterparty(cps, event)
                    if changed_c or changed_cp:
                        await self._store.save(event.customer_id, cs, event.counterparty_id, cps)
                    await session.rollback()
                    return ProcessResult(transaction_id, "skipped")

                with stage("features"):
                    feats = compute_features(event, cs, cps)
                with stage("inference"):
                    scored = self._model.score_one(feats)

                with stage("persist"):
                    now = datetime.now(UTC)
                    reasons = [c.to_dict() for c in scored.contributions]
                    row.status = TransactionStatus.SCORED.value
                    row.score = scored.score
                    row.decision = scored.decision.value
                    row.model_version = self._model.version
                    row.features = (
                        feats
                        if scored.anomaly_score is None
                        else {**feats, "anomaly_score": scored.anomaly_score}
                    )
                    row.explanation = reasons
                    row.error = None
                    row.processed_at = now
                    severity: AlertSeverity | None = None
                    if scored.decision is not Decision.APPROVE:
                        severity = (
                            AlertSeverity.HIGH
                            if scored.decision is Decision.BLOCK
                            else AlertSeverity.MEDIUM
                        )
                        await session.execute(
                            pg_insert(Alert)
                            .values(
                                transaction_id=row.id,
                                customer_id=row.customer_id,
                                counterparty_id=row.counterparty_id,
                                amount=row.amount,
                                score=scored.score,
                                severity=severity.value,
                                reasons=[r for r in reasons if r["contribution"] > 0],
                                model_version=self._model.version,
                            )
                            .on_conflict_do_nothing(index_elements=["transaction_id"])
                        )
                    await session.commit()

                with stage("state_write"):
                    apply_transaction(event, cs, cps)
                    await self._store.save(event.customer_id, cs, event.counterparty_id, cps)

        metrics.TX_SCORED.labels(decision=scored.decision.value).inc()
        metrics.SCORE.observe(scored.score)
        if severity is not None:
            metrics.ALERTS.labels(severity=severity.value).inc()
        metrics.STAGE_DURATION.labels(stage="total").observe(time.perf_counter() - started)
        if row.created_at is not None:
            metrics.END_TO_END.observe(max((now - row.created_at).total_seconds(), 0.0))
        if self._drift is not None:
            self._drift.observe(scored.score)
        log.info(
            "transaction_scored",
            transaction_id=transaction_id,
            customer_id=event.customer_id,
            score=round(scored.score, 5),
            decision=scored.decision.value,
            model_version=self._model.version,
        )
        return ProcessResult(transaction_id, "scored", scored.decision, scored.score)
