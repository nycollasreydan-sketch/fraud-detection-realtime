from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from fraud import streams
from fraud.api.errors import (
    ConflictError,
    NotFoundError,
    ServiceUnavailableError,
    UnprocessableError,
)
from fraud.api.schemas import (
    AlertOut,
    AlertPage,
    ErrorResponse,
    HealthOut,
    StatsOut,
    TransactionIn,
    TransactionOut,
)
from fraud.config import Settings
from fraud.db.models import Alert, Transaction
from fraud.domain import AlertSeverity, AlertStatus
from fraud.ml.model import METADATA_FILE
from fraud.observability import metrics
from fraud.observability.logging import get_logger
from fraud.observability.tracing import inject_context

log = get_logger(__name__)
router = APIRouter()


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


def get_redis(request: Request) -> Redis:
    return request.app.state.redis  # type: ignore[no-any-return]


async def get_session(request: Request) -> Any:
    async with request.app.state.sessions() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]
RedisDep = Annotated[Redis, Depends(get_redis)]
SettingsDep = Annotated[Settings, Depends(get_settings_dep)]

ERRORS: dict[int | str, dict[str, Any]] = {
    422: {"model": ErrorResponse},
    503: {"model": ErrorResponse},
}


def _same_payload(row: Transaction, body: TransactionIn) -> bool:
    return (
        row.customer_id == body.customer_id
        and row.counterparty_id == body.counterparty_id
        and row.amount == body.amount
        and row.device_id == body.device_id
    )


@router.post(
    "/transactions",
    response_model=TransactionOut,
    status_code=202,
    responses={**ERRORS, 200: {"model": TransactionOut}, 409: {"model": ErrorResponse}},
    summary="Recebe uma transação e a coloca na fila de decisão",
)
async def create_transaction(
    body: TransactionIn,
    response: Response,
    session: SessionDep,
    redis: RedisDep,
    settings: SettingsDep,
    idempotency_key: Annotated[str | None, Header(min_length=1, max_length=128)] = None,
) -> Transaction:
    occurred_at = body.event_time()
    if occurred_at > datetime.now(UTC) + timedelta(seconds=settings.max_future_skew_s):
        raise UnprocessableError(
            "Requisição inválida",
            [{"field": "body.occurred_at", "message": "data no futuro", "type": "future_datetime"}],
        )

    async def existing() -> Transaction | None:
        if idempotency_key is None:
            return None
        return (
            await session.scalars(
                select(Transaction).where(Transaction.idempotency_key == idempotency_key)
            )
        ).first()

    def replay(row: Transaction) -> Transaction:
        if not _same_payload(row, body):
            raise ConflictError(
                "Idempotency-Key já usada com outro conteúdo", {"transaction_id": str(row.id)}
            )
        metrics.TX_INGESTED.labels(result="duplicate").inc()
        response.status_code = 200
        return row

    if (row := await existing()) is not None:
        return replay(row)

    row = Transaction(
        id=uuid.uuid4(),
        idempotency_key=idempotency_key,
        customer_id=body.customer_id,
        counterparty_id=body.counterparty_id,
        amount=body.amount,
        currency=body.currency,
        channel=body.channel.value,
        category=body.category,
        device_id=body.device_id,
        latitude=body.latitude,
        longitude=body.longitude,
        occurred_at=occurred_at,
    )
    session.add(row)
    try:
        await session.commit()
    except IntegrityError:  # corrida: duas requisições com a mesma chave
        await session.rollback()
        if (row_existing := await existing()) is not None:
            return replay(row_existing)
        raise

    try:
        await streams.publish(redis, settings, str(row.id), inject_context())
    except RedisError as exc:
        # a transação está no banco como `pending`; o sweeper do worker republica
        metrics.TX_INGESTED.labels(result="queue_unavailable").inc()
        log.error("publish_failed", transaction_id=str(row.id), error=repr(exc))
        raise ServiceUnavailableError(
            "Fila indisponível; a transação foi registrada e será processada",
            {"transaction_id": str(row.id)},
        ) from exc
    metrics.TX_INGESTED.labels(result="accepted").inc()
    return row


@router.get(
    "/transactions/{transaction_id}",
    response_model=TransactionOut,
    responses={404: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
    summary="Estado e decisão de uma transação",
)
async def get_transaction(transaction_id: uuid.UUID, session: SessionDep) -> Transaction:
    row = await session.get(Transaction, transaction_id)
    if row is None:
        raise NotFoundError("Transação não encontrada", {"transaction_id": str(transaction_id)})
    return row


@router.get(
    "/alerts",
    response_model=AlertPage,
    responses={422: {"model": ErrorResponse}},
    summary="Alertas, do mais recente para o mais antigo",
)
async def list_alerts(
    session: SessionDep,
    severity: AlertSeverity | None = None,
    status: AlertStatus | None = None,
    customer_id: Annotated[str | None, Query(max_length=64)] = None,
    min_score: Annotated[float | None, Query(ge=0, le=1)] = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AlertPage:
    filters = []
    if severity is not None:
        filters.append(Alert.severity == severity.value)
    if status is not None:
        filters.append(Alert.status == status.value)
    if customer_id is not None:
        filters.append(Alert.customer_id == customer_id)
    if min_score is not None:
        filters.append(Alert.score >= min_score)
    if since is not None:
        filters.append(Alert.created_at >= since)
    if until is not None:
        filters.append(Alert.created_at < until)

    total = await session.scalar(select(func.count()).select_from(Alert).where(*filters)) or 0
    rows = (
        await session.scalars(
            select(Alert)
            .where(*filters)
            .order_by(Alert.created_at.desc(), Alert.id)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return AlertPage(
        items=[AlertOut.model_validate(r) for r in rows], total=total, limit=limit, offset=offset
    )


@router.get(
    "/health",
    response_model=HealthOut,
    responses={503: {"model": HealthOut}},
    summary="Saúde das dependências",
)
async def health(
    response: Response, session: SessionDep, redis: RedisDep, settings: SettingsDep
) -> HealthOut:
    checks: dict[str, str] = {}
    try:
        await session.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"error: {type(exc).__name__}"
    try:
        await redis.ping()
        checks["redis"] = "ok"
    except Exception as exc:
        checks["redis"] = f"error: {type(exc).__name__}"
    checks["model"] = "ok" if (Path(settings.model_dir) / METADATA_FILE).exists() else "missing"
    healthy = checks["database"] == "ok" and checks["redis"] == "ok"
    if not healthy:
        response.status_code = 503
    return HealthOut(status="ok" if healthy else "unavailable", checks=checks)


@router.get("/model/info", summary="Metadados e métricas do modelo em uso")
async def model_info(settings: SettingsDep) -> dict[str, Any]:
    path = Path(settings.model_dir) / METADATA_FILE
    if not path.exists():
        raise NotFoundError("Nenhum modelo treinado encontrado", {"model_dir": settings.model_dir})
    data: dict[str, Any] = json.loads(path.read_text())
    return data


@router.get("/stats", response_model=StatsOut, summary="Contadores recentes para o painel")
async def stats(
    session: SessionDep,
    redis: RedisDep,
    settings: SettingsDep,
    window_minutes: Annotated[int, Query(ge=1, le=7 * 24 * 60)] = 60,
) -> StatsOut:
    since = datetime.now(UTC) - timedelta(minutes=window_minutes)
    t = Transaction
    by_status = (
        await session.execute(
            select(t.status, func.count()).where(t.created_at >= since).group_by(t.status)
        )
    ).all()
    by_decision = (
        await session.execute(
            select(t.decision, func.count())
            .where(t.created_at >= since, t.decision.is_not(None))
            .group_by(t.decision)
        )
    ).all()
    by_severity = (
        await session.execute(
            select(Alert.severity, func.count())
            .where(Alert.created_at >= since)
            .group_by(Alert.severity)
        )
    ).all()
    latency = func.extract("epoch", t.processed_at - t.created_at) * 1000
    p50, p95, p99 = (
        await session.execute(
            select(
                func.percentile_cont(0.5).within_group(latency),
                func.percentile_cont(0.95).within_group(latency),
                func.percentile_cont(0.99).within_group(latency),
            ).where(t.created_at >= since, t.processed_at.is_not(None))
        )
    ).one()
    queue = {"length": 0, "lag": 0, "pending": 0, "dead_letter": 0}
    try:
        queue.update(await streams.group_stats(redis, settings))
        queue["length"] = int(await redis.xlen(settings.stream_name))
        queue["dead_letter"] = int(await redis.xlen(settings.dlq_stream))
    except RedisError:
        pass
    return StatsOut(
        window_minutes=window_minutes,
        transactions={str(k): int(v) for k, v in by_status},
        decisions={str(k): int(v) for k, v in by_decision},
        alerts={str(k): int(v) for k, v in by_severity},
        decision_latency_ms={
            "p50": float(p50) if p50 is not None else None,
            "p95": float(p95) if p95 is not None else None,
            "p99": float(p99) if p99 is not None else None,
        },
        queue=queue,
    )
