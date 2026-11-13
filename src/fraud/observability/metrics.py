"""Métricas Prometheus. Um único módulo para que nomes e labels fiquem em um lugar só."""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram, Info

LATENCY_BUCKETS = (0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

# ---- API ------------------------------------------------------------------------------
HTTP_REQUESTS = Counter(
    "fraud_http_requests_total", "Requisições HTTP", ["method", "route", "status"]
)
HTTP_DURATION = Histogram(
    "fraud_http_request_duration_seconds",
    "Duração das requisições HTTP",
    ["method", "route"],
    buckets=LATENCY_BUCKETS,
)
TX_INGESTED = Counter(
    "fraud_transactions_ingested_total", "Transações aceitas pela API", ["result"]
)

# ---- worker ---------------------------------------------------------------------------
TX_SCORED = Counter("fraud_transactions_scored_total", "Transações decididas", ["decision"])
ALERTS = Counter("fraud_alerts_total", "Alertas gerados", ["severity"])
SCORE = Histogram(
    "fraud_score",
    "Distribuição do score de fraude",
    buckets=(0.001, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.3, 0.5, 0.7, 0.9, 0.99),
)
STAGE_DURATION = Histogram(
    "fraud_worker_stage_duration_seconds",
    "Duração de cada etapa do processamento",
    ["stage"],
    buckets=LATENCY_BUCKETS,
)
END_TO_END = Histogram(
    "fraud_decision_latency_seconds",
    "Tempo entre a entrada na API e a decisão gravada",
    buckets=LATENCY_BUCKETS,
)
WORKER_FAILURES = Counter(
    "fraud_worker_failures_total", "Falhas no worker", ["outcome"]
)  # outcome: retry | dlq | claimed
STATE_CACHE = Counter(
    "fraud_state_cache_total", "Leituras de estado no Redis", ["entity", "result"]
)  # result: hit | miss
STREAM_LAG = Gauge("fraud_stream_lag", "Mensagens ainda não entregues ao grupo")
STREAM_PENDING = Gauge("fraud_stream_pending", "Mensagens entregues e sem ACK")
SWEEPER_REPUBLISHED = Counter(
    "fraud_sweeper_republished_total", "Transações pendentes republicadas pelo sweeper"
)
SCORE_PSI = Gauge("fraud_score_psi", "PSI dos scores recentes contra a referência do treino")
MODEL_INFO = Info("fraud_model", "Modelo carregado")
