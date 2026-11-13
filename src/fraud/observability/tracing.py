"""Tracing com OpenTelemetry.

O ponto não óbvio é a fila: o contexto do trace viaja dentro da mensagem do Redis Stream
(campo `traceparent`), então o span do worker é filho do span da API e um único trace cobre
a transação de ponta a ponta (ADR 010).
"""

from __future__ import annotations

from collections.abc import Mapping

from opentelemetry import propagate, trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from sqlalchemy.ext.asyncio import AsyncEngine

_configured = False


def configure_tracing(service_name: str, otlp_endpoint: str | None) -> None:
    """Idempotente. Sem endpoint, os spans existem (ids nos logs) mas não são exportados."""
    global _configured
    if _configured:
        return
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    if otlp_endpoint:
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint)))
    trace.set_tracer_provider(provider)
    _configured = True


def get_tracer(name: str) -> trace.Tracer:
    return trace.get_tracer(name)


def inject_context() -> dict[str, str]:
    """Contexto do span atual, pronto para ir como campos da mensagem."""
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier


def extract_context(fields: Mapping[str, str]) -> Context:
    return propagate.extract(dict(fields))


def instrument_redis() -> None:
    """Spans automáticos para comandos Redis (idempotente)."""
    if not RedisInstrumentor().is_instrumented_by_opentelemetry:
        RedisInstrumentor().instrument()


def instrument_engine(engine: AsyncEngine) -> None:
    """Spans automáticos para as consultas SQL deste engine."""
    instrumentor = SQLAlchemyInstrumentor()
    if not instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.instrument(engine=engine.sync_engine)
