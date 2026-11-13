"""Configuração por variáveis de ambiente (prefixo FRAUD_), com defaults para dev local."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FRAUD_", env_file=".env", extra="ignore")

    env: str = "dev"
    service_name: str = "fraud-api"
    log_level: str = "INFO"
    log_json: bool = True

    database_url: str = "postgresql+asyncpg://fraud:fraud@localhost:5432/fraud"
    redis_url: str = "redis://localhost:6379/0"
    model_dir: str = "artifacts/model"
    api_url: str = "http://localhost:8000"

    # fila
    stream_name: str = "transactions"
    dlq_stream: str = "transactions.dlq"
    consumer_group: str = "scoring"
    stream_maxlen: int = 200_000

    # worker
    worker_batch_size: int = 32
    worker_block_ms: int = 1000
    worker_max_attempts: int = 3  # tentativas em processo, com backoff
    worker_backoff_base_s: float = 0.2
    worker_claim_idle_ms: int = 30_000  # mensagem parada com outro consumidor há mais que isso
    worker_max_deliveries: int = 5  # entregas antes de ir para a DLQ
    worker_maintenance_interval_s: float = 10.0
    worker_metrics_port: int = 9100
    sweeper_pending_age_s: float = 60.0
    sweeper_batch: int = 500

    # estado / cache
    state_ttl_s: int = 60 * 24 * 3600
    lock_timeout_s: float = 10.0
    lock_wait_s: float = 5.0
    rebuild_history_limit: int = 500

    # tracing
    otlp_endpoint: str | None = None  # ex.: http://jaeger:4318/v1/traces

    # API
    max_future_skew_s: float = 300.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
