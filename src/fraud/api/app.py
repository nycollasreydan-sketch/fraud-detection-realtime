"""Fábrica da aplicação FastAPI."""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request, Response
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from redis.asyncio import Redis

from fraud import streams
from fraud.api.errors import register_error_handlers
from fraud.api.routes import router
from fraud.config import Settings, get_settings
from fraud.db.session import create_engine, create_session_factory
from fraud.observability import metrics
from fraud.observability.logging import configure_logging, get_logger
from fraud.observability.tracing import configure_tracing, instrument_engine, instrument_redis

log = get_logger(__name__)
QUIET_PATHS = {"/metrics", "/health"}


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.service_name, settings.log_level, settings.log_json)
    configure_tracing(settings.service_name, settings.otlp_endpoint)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings)
        instrument_engine(engine)
        redis = Redis.from_url(settings.redis_url, decode_responses=True)
        app.state.settings = settings
        app.state.engine = engine
        app.state.sessions = create_session_factory(engine)
        app.state.redis = redis
        try:
            await streams.ensure_group(redis, settings)
        except Exception as exc:  # sobe mesmo sem Redis; /health mostra o problema
            log.warning("ensure_group_failed", error=repr(exc))
        log.info("api_started", env=settings.env)
        try:
            yield
        finally:
            await redis.aclose()
            await engine.dispose()

    app = FastAPI(
        title="Fraud Detection API",
        version="0.1.0",
        description="Ingestão de transações e consulta de decisões e alertas de fraude.",
        lifespan=lifespan,
    )
    register_error_handlers(app)
    app.include_router(router)

    @app.get("/metrics", include_in_schema=False)
    async def prometheus_metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.middleware("http")
    async def observability(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        structlog.contextvars.bind_contextvars(request_id=request_id)
        start = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers["x-request-id"] = request_id
            return response
        finally:
            elapsed = time.perf_counter() - start
            route = request.scope.get("route")
            # template da rota (não o caminho real) para não explodir a cardinalidade
            path = getattr(route, "path", "unmatched")
            metrics.HTTP_REQUESTS.labels(request.method, path, str(status)).inc()
            metrics.HTTP_DURATION.labels(request.method, path).observe(elapsed)
            if request.url.path not in QUIET_PATHS:
                log.info(
                    "http_request",
                    method=request.method,
                    path=request.url.path,
                    status=status,
                    duration_ms=round(elapsed * 1000, 2),
                )
            structlog.contextvars.unbind_contextvars("request_id")

    FastAPIInstrumentor.instrument_app(app, excluded_urls="metrics,health")
    instrument_redis()
    return app
