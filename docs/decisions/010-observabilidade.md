# 010 — Logs JSON, métricas Prometheus e trace propagado pela fila

**Status:** aceita

## Decisão

Três sinais, cada um para uma pergunta diferente, e amarrados entre si:

**Logs (structlog, JSON em stdout).** "O que aconteceu com esta transação?" Cada linha tem
`service`, `request_id`, `trace_id` e `span_id`. O `request_id` vem do header
`X-Request-ID` (ou é gerado) e volta na resposta e no corpo de qualquer erro.

**Métricas (Prometheus).** "O sistema está saudável agora?" API em `/metrics`, worker em
`:9100/metrics`. As que importam:

| Métrica | Pergunta |
|---|---|
| `fraud_decision_latency_seconds` | Quanto tempo entre a entrada na API e a decisão? |
| `fraud_worker_stage_duration_seconds{stage}` | Onde o worker gasta tempo (lock, estado, features, inferência, banco)? |
| `fraud_stream_lag`, `fraud_stream_pending` | A fila está acumulando? |
| `fraud_worker_failures_total{outcome}` | Há retries, reentregas ou dead-letters? |
| `fraud_transactions_scored_total{decision}`, `fraud_alerts_total` | A taxa de alerta mudou? |
| `fraud_score` (histograma), `fraud_score_psi` | A distribuição de scores derivou em relação ao treino? |
| `fraud_state_cache_total{entity,result}` | O cache de estado está funcionando? |
| `fraud_http_request_duration_seconds{route}` | Latência por rota |

**Traces (OpenTelemetry → OTLP/HTTP → Jaeger).** "Por que esta transação demorou?" FastAPI,
SQLAlchemy e Redis são instrumentados automaticamente; as etapas do worker são spans
manuais.

## O ponto não óbvio: o trace atravessa a fila

A instrumentação automática para no `XADD`. Para o trace continuar, a API injeta o contexto
W3C (`traceparent`) como campo da mensagem do stream, e o worker o extrai e abre o span
`worker.process_transaction` como filho. Resultado: **um único trace** mostra
`POST /transactions` → `INSERT` → `XADD` → espera na fila → lock → estado → inferência →
`UPDATE`. Há teste de que a mensagem carrega o `traceparent`.

## Alternativas descartadas

- **Logs de texto.** Não dá para filtrar por transação nem correlacionar com o trace.
- **Tracing só na API.** Mostraria os 5 ms menos interessantes do fluxo.
- **Rótulo com o caminho real da requisição.** `/transactions/{id}` com o id no rótulo cria
  uma série por transação; o middleware usa o template da rota.
- **Grafana com dashboards provisionados.** Ficaria bonito, mas é configuração, não
  decisão; Prometheus e Jaeger já respondem às perguntas acima.

## Consequências

- O PSI de scores é calculado em memória por worker, numa janela de 2.000 decisões. Com
  vários workers cada um vê uma amostra; para um número global seria preciso agregar os
  histogramas no Prometheus.
- O worker de exemplo processa uma mensagem por vez; por isso `stage="lock"` é quase zero.
  Com vários workers, é essa métrica que mostra contenção em recebedores populares.
