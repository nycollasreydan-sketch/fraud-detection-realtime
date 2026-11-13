# Fase B — Arquitetura

## 1. Visão geral

```
                         ┌──────────────────────────────────────────────────────────┐
                         │                    Observabilidade                       │
                         │  Prometheus ◄── /metrics (api:8000, worker:9100)         │
                         │  Jaeger     ◄── OTLP/HTTP (traces api → fila → worker)   │
                         │  stdout     ◄── logs JSON (structlog, com trace_id)      │
                         └──────────────────────────────────────────────────────────┘

 ┌───────────────┐  POST /transactions   ┌─────────────────┐  XADD   ┌────────────────────┐
 │ Gerador /     │ ────────────────────► │   API FastAPI   │ ──────► │   Redis Streams    │
 │ cliente HTTP  │ ◄──── 202 + id ────── │   (stateless)   │         │ stream transactions│
 └───────────────┘                       └────────┬────────┘         │ grupo scoring      │
        │  GET /transactions/{id}                 │ INSERT pending   │ stream ...dlq      │
        │  GET /alerts                            ▼                  └─────────┬──────────┘
        │                                ┌─────────────────┐                   │ XREADGROUP
        │                                │   PostgreSQL    │                   ▼
        │                                │  transactions   │ UPDATE   ┌────────────────────┐
        │                                │  alerts         │ ◄─────── │  Worker assíncrono │
        │                                └────────▲────────┘ INSERT   │                    │
        │                                         │ cache miss        │ 1. lock entidades  │
 ┌──────▼────────┐  GET /alerts, /stats,          │ (replay)          │ 2. estado (Redis)  │
 │   Streamlit   │  /model/info                   │                   │ 3. features        │
 │   dashboard   │ ─────────► API        ┌────────┴────────┐  GET/SET │ 4. modelo + SHAP   │
 └───────────────┘                       │  Redis (cache)  │ ◄──────► │ 5. grava + alerta  │
                                         │ estado cliente  │          │ 6. XACK            │
                                         │ estado recebedor│          │ falha → retry → DLQ│
                                         └─────────────────┘          └─────────┬──────────┘
                                                                                │ carrega
                                                                       ┌────────▼──────────┐
                                                                       │ artifacts/model   │
                                                                       │ (joblib + JSON)   │
                                                                       └───────────────────┘
```

## 2. Componentes

| Componente | Código | Responsabilidade | Estado |
|---|---|---|---|
| API | `src/fraud/api` | Valida, persiste como `pending`, publica na fila, serve consultas | Nenhum (escala horizontal) |
| Fila | `src/fraud/streams.py` | Redis Streams com consumer group; DLQ em stream separado | Redis |
| Worker | `src/fraud/worker` | Consome, monta features, infere, grava decisão e alerta, trata falhas | Nenhum próprio; modelo em memória |
| Feature store online | `src/fraud/features` | Estado incremental por cliente e por recebedor; cache-aside sobre o PostgreSQL | Redis (TTL) |
| Pipeline de ML | `src/fraud/ml` | Simulação, features offline, treino, avaliação, serialização, inferência | `artifacts/` |
| Simuladores | `src/fraud/simulation` | Mundo sintético, gerador de tráfego via HTTP, ataques adversariais e demo | Nenhum |
| Banco | `src/fraud/db`, `alembic/` | Fonte da verdade de transações, decisões e alertas | PostgreSQL |
| Dashboard | `src/fraud/dashboard` | Alertas recentes e métricas do modelo, só leitura via API | Nenhum |
| Observabilidade | `src/fraud/observability` | Logs JSON, métricas Prometheus, tracing OpenTelemetry | Prometheus, Jaeger |

## 3. Fluxo de uma transação, do início ao fim

1. **Entrada.** O cliente envia `POST /transactions`. O Pydantic valida tipos, faixas e
   moeda. Erros voltam no formato padronizado (`{"error": {"code", "message", "details",
   "request_id"}}`).
2. **Idempotência.** Se vier o header `Idempotency-Key`, ele vira chave única no banco. Uma
   repetição devolve a mesma transação (200) em vez de criar outra. Isso é a primeira
   defesa contra replay de requisição.
3. **Persistência.** A API insere a linha com `status = pending`. O banco é a fonte da
   verdade desde o primeiro instante; nada existe só na fila.
4. **Publicação.** A API faz `XADD` no stream `transactions` com o id da transação e o
   contexto de trace (`traceparent`). Se o Redis estiver fora, a linha continua `pending`
   e a API responde 503; o *sweeper* do worker republica pendentes antigos (passo 11).
5. **Resposta.** `202 Accepted` com o id e `status = pending`.
6. **Consumo.** Um worker lê com `XREADGROUP` (grupo `scoring`). A mensagem fica na lista de
   pendentes do grupo até o `XACK`.
7. **Estado.** O worker pega locks curtos nas duas entidades (cliente e recebedor), lê o
   estado de cada uma no Redis e, se não houver (TTL expirado ou Redis reiniciado),
   reconstrói a partir das transações já processadas no PostgreSQL.
8. **Features.** `compute_features(transação, estado_cliente, estado_recebedor)` produz o
   vetor **antes** de atualizar o estado (a transação nunca entra no próprio histórico).
   É a mesma função usada no treino.
9. **Inferência.** Isolation Forest gera o score de anomalia, o LightGBM gera a
   probabilidade calibrada e as contribuições por feature (TreeSHAP). Dois limiares
   definem a decisão: `approve`, `review` ou `block`.
10. **Gravação.** Em uma única transação de banco: atualiza a linha (score, decisão, versão
    do modelo, features, explicação, latência) e, se a decisão não for `approve`, cria o
    alerta. Depois grava o novo estado no Redis e faz `XACK`.
11. **Falhas.** Exceção no processamento → novas tentativas com backoff exponencial. Esgotou
    → a mensagem vai para o stream `transactions.dlq` com o erro, a transação vira `failed`
    e a original recebe `XACK`. Worker que morreu no meio → outro worker assume a mensagem
    com `XAUTOCLAIM` depois do tempo ocioso. Transação `pending` há tempo demais sem
    mensagem → o sweeper republica.
12. **Consulta.** `GET /transactions/{id}` mostra o estado e, quando pronto, score, decisão e
    as principais razões. `GET /alerts` lista alertas com filtros e paginação.

Além dos quatro endpoints pedidos, a API expõe `GET /model/info` (metadados e métricas do
modelo em uso), `GET /stats` (contadores recentes) e `GET /metrics` (Prometheus). Os dois
primeiros existem para o painel não precisar de acesso direto ao banco.

Orçamento de latência (medido, ver README): a meta é p95 abaixo de 100 ms entre a entrada
na API e a decisão gravada, em uma máquina de 2 vCPUs.

## 4. Modelo de dados

```
transactions                                alerts
────────────                                ──────
id               uuid PK                    id              uuid PK
idempotency_key  text UNIQUE NULL           transaction_id  uuid FK UNIQUE
customer_id      text    ┐ índice           customer_id     text  (índice)
occurred_at      tstz    ┘ (cliente, ts)    counterparty_id text
counterparty_id  text      índice com ts    amount          numeric(14,2)
amount           numeric(14,2)  > 0         score           float
currency         char(3)                    severity        medium | high
channel          pos | ecommerce | transfer status          open | confirmed | dismissed
category         text                       reasons         jsonb  (features que
device_id        text                                       empurraram para fraude)
latitude, longitude  float                  model_version   text
status           pending | scored | failed  created_at      tstz  (índice)
score            float NULL
decision         approve | review | block NULL
model_version    text NULL
features         jsonb NULL   (vetor usado: auditoria e retreino)
explanation      jsonb NULL   (top contribuições TreeSHAP)
error            text NULL
created_at, processed_at  tstz  (índice em created_at: janelas do /stats)
```

Enums são `VARCHAR` com `CHECK` (migração simples de alterar). O alerta repete cliente,
recebedor e valor para a listagem não precisar de join. A predição fica na própria linha da
transação (relação 1:1, sempre lida junto). Guardar o
vetor de features usado é o que permite auditar uma decisão meses depois e montar datasets
de retreino sem recomputar estado histórico.

## 5. Decisões técnicas

Cada decisão tem um ADR em [`docs/decisions/`](decisions/). Resumo:

| # | Decisão | Por quê | Alternativa descartada |
|---|---|---|---|
| [001](decisions/001-stack-e-estrutura.md) | Monólito modular, uma imagem Docker, três processos | Um deploy, uma base de código, fronteiras por módulo | Microsserviços separados por repositório: custo operacional sem ganho nesta escala |
| [002](decisions/002-decisao-assincrona-com-redis-streams.md) | Decisão assíncrona via Redis Streams com consumer group | Desacopla pico de entrada da inferência; at-least-once com ack; mesmo Redis do cache | Kafka (operação pesada demais para o escopo); Redis List/PubSub (sem ack nem reentrega); decisão síncrona (ver ADR) |
| [003](decisions/003-dados-sinteticos-e-benchmark-real.md) | Simulador próprio para treinar; dataset ULB só como benchmark | O dataset real não tem cliente nem recebedor, logo não permite features de histórico | Treinar só no ULB: impossível demonstrar o sistema online |
| [004](decisions/004-agregados-com-decaimento-exponencial.md) | Agregados com decaimento exponencial | O(1) em memória e CPU, estado pequeno e serializável | Janelas exatas em sorted sets: custo cresce com o volume do cliente |
| [005](decisions/005-paridade-treino-servico.md) | Uma função de features para treino e serviço | Elimina training-serving skew por construção | Pandas no treino + código separado no worker |
| [006](decisions/006-modelo-hibrido.md) | LightGBM sobre features de estado + score de Isolation Forest + calibração | Ver RESEARCH.md §6; o ADR registra o que a medição confirmou e o que não | RNN de sequência; média de scores |
| [007](decisions/007-limiares-por-orcamento-de-alertas.md) | Limiares por orçamento de falso positivo | Capacidade de analistas é o recurso escasso | Limiar 0,5; maximizar F1 |
| [008](decisions/008-estado-em-redis-com-cache-aside.md) | Estado em Redis como cache, PostgreSQL como verdade | Redis pode ser apagado sem perda: o estado é reconstruível | Estado só no Redis (perda = modelo cego); estado só no PostgreSQL (latência) |
| [009](decisions/009-falhas-retry-dlq-idempotencia.md) | Retry com backoff, DLQ, XAUTOCLAIM, processamento idempotente | At-least-once exige idempotência; falha permanente não pode travar a fila | Descartar mensagens com erro; retry infinito |
| [010](decisions/010-observabilidade.md) | structlog JSON + Prometheus + OpenTelemetry com contexto propagado pela fila | Um trace cobre API → fila → worker | Logs de texto; tracing só na API |
| [011](decisions/011-ciclo-adversarial.md) | Suíte adversarial como teste automatizado com alvo numérico | Robustez vira critério de regressão, não opinião | Avaliar só no conjunto de teste |
| [012](decisions/012-inferencia-de-uma-linha.md) | Caminho rápido de inferência para uma transação | A Isolation Forest do scikit-learn custava 90% da latência por linha | ONNX/treelite; explicar só os alertas |

## 6. Concorrência, ordenação e consistência

- **Ordenação por entidade.** As features dependem da ordem das transações de um cliente.
  Com vários workers no mesmo grupo, duas transações do mesmo cliente podem ser processadas
  em paralelo. O worker serializa o trecho crítico (ler estado → inferir → gravar estado)
  com locks Redis por cliente e por recebedor, sempre adquiridos em ordem lexicográfica para
  não haver deadlock. O estado é atualizado por **tempo do evento**; eventos fora de ordem
  não geram decaimento negativo.
- **Entrega at-least-once.** Uma mensagem pode ser entregue duas vezes. O processamento é
  idempotente em dois níveis: transação já `scored` é só confirmada (`XACK`); e cada estado
  guarda os ids recentes aplicados, então reaplicar a mesma transação não conta em dobro.
- **Escrita dupla (banco + fila).** A API grava no banco e depois publica. Se a publicação
  falhar, a linha fica `pending` e o sweeper a republica. Um outbox transacional seria a
  solução rigorosa; o sweeper cobre o mesmo caso com muito menos código
  ([ADR 009](decisions/009-falhas-retry-dlq-idempotencia.md)).

## 7. Segurança e limites do escopo

- Sem autenticação na API: é uma demonstração local. Em produção haveria mTLS ou OAuth2
  entre serviços e controle de acesso nos endpoints de leitura.
- Dados 100% sintéticos; não há dado pessoal real no repositório.
- Fora do escopo: retreino automático, feedback de analista realimentando o modelo, features
  de grafo com mais de um salto, alta disponibilidade de Redis e PostgreSQL.
