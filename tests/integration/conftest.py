"""Fixtures de integração: PostgreSQL e Redis reais (`make test` sobe os dois).

Banco `fraud_test` e Redis db 15, separados dos dados de desenvolvimento.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import httpx
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine

from fraud import streams
from fraud.api.app import create_app
from fraud.config import Settings
from fraud.db.session import SessionFactory, create_engine, create_session_factory
from fraud.ml.model import FraudModel
from fraud.worker.main import Worker
from fraud.worker.processor import Processor

ROOT = Path(__file__).resolve().parents[2]
TEST_DATABASE_URL = os.environ.get(
    "FRAUD_TEST_DATABASE_URL", "postgresql+asyncpg://fraud:fraud@localhost:5432/fraud_test"
)
TEST_REDIS_URL = os.environ.get("FRAUD_TEST_REDIS_URL", "redis://localhost:6379/15")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "tests/integration" in str(item.fspath):
            item.add_marker(pytest.mark.integration)


async def _ensure_database() -> None:
    url = make_url(TEST_DATABASE_URL)
    conn = await asyncpg.connect(
        user=url.username, password=url.password, host=url.host, port=url.port, database="postgres"
    )
    try:
        exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", url.database)
        if not exists:
            await conn.execute(f'CREATE DATABASE "{url.database}"')
    finally:
        await conn.close()


def alembic_config() -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    return cfg


@pytest.fixture(scope="session")
def migrated_database() -> None:
    """Cria o banco de teste e aplica as migrações reais (as mesmas de produção)."""
    asyncio.run(_ensure_database())
    cfg = alembic_config()
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")


@pytest.fixture(scope="session")
def settings(model_dir: Path, migrated_database: None) -> Settings:
    return Settings(
        database_url=TEST_DATABASE_URL,
        redis_url=TEST_REDIS_URL,
        model_dir=str(model_dir),
        log_level="WARNING",
        log_json=False,
        otlp_endpoint=None,
        worker_block_ms=10,
        worker_backoff_base_s=0.001,
        worker_claim_idle_ms=50,
        worker_max_attempts=3,
        worker_max_deliveries=3,
        sweeper_pending_age_s=0.0,
        lock_wait_s=1.0,
    )


@pytest_asyncio.fixture(scope="session")
async def engine(settings: Settings) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(settings)
    yield engine
    await engine.dispose()


@pytest.fixture(scope="session")
def sessions(engine: AsyncEngine) -> SessionFactory:
    return create_session_factory(engine)


@pytest_asyncio.fixture(scope="session")
async def redis(settings: Settings) -> AsyncIterator[Redis]:
    client = Redis.from_url(settings.redis_url, decode_responses=True)
    yield client
    await client.aclose()


@pytest_asyncio.fixture(autouse=True)
async def clean_state(engine: AsyncEngine, redis: Redis, settings: Settings) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE alerts, transactions"))
    await redis.flushdb()
    await streams.ensure_group(redis, settings)


@pytest_asyncio.fixture
async def client(settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            yield http


@pytest.fixture
def processor(
    settings: Settings, redis: Redis, sessions: SessionFactory, model: FraudModel
) -> Processor:
    return Processor(settings, redis, sessions, model)


@pytest.fixture
def worker(
    settings: Settings, redis: Redis, sessions: SessionFactory, processor: Processor
) -> Worker:
    return Worker(settings, redis, sessions, processor, consumer_name="test-worker")


def payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "customer_id": "c1",
        "counterparty_id": "m1",
        "amount": "100.00",
        "channel": "ecommerce",
        "category": "grocery",
        "device_id": "d1",
        "latitude": -23.55,
        "longitude": -46.63,
    }
    return {**base, **overrides}
