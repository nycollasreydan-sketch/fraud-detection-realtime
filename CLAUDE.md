# CLAUDE.md

Detecção de fraude em tempo real: API FastAPI → PostgreSQL + Redis Streams → worker que
calcula features de comportamento, roda LightGBM calibrado e grava decisão com explicação.
Python 3.12, `uv`, mypy estrito. Código em inglês; docstrings, comentários, docs e commits
em português (`feat: ...`, `fix: ...`, `docs: ...`, `test: ...`).

## Comandos

```bash
make test-unit          # sem Docker, ~30 s (treina um modelo pequeno uma vez)
make test-integration   # sobe PostgreSQL e Redis; usa banco fraud_test e Redis db 15
make test-adversarial   # treina o modelo completo, ~1 a 2 min
make test               # as três suítes (119 testes)
make lint typecheck     # ruff + ruff format --check + mypy estrito
make format             # corrige formatação
make up / make demo     # sobe tudo (com build) / demonstração de ponta a ponta
make train              # retreina no volume Docker e reinicia api e worker
make docs               # regenera docs/FEATURES.md e docs/MODEL_REPORT.md (retreina local)
```

Antes de dar uma tarefa por concluída: `make lint typecheck` e as suítes afetadas. Mexeu em
`features/`, `simulation/` ou `ml/`: rode também `make test-adversarial`.

## Mapa

| Caminho | Papel |
|---|---|
| `src/fraud/domain.py` | `TxEvent` e enums; não importa web nem banco |
| `src/fraud/api/` | rotas, schemas Pydantic, envelope de erro |
| `src/fraud/streams.py` | fila (Redis Streams, consumer group, DLQ) |
| `src/fraud/features/` | `state.py` (estado por entidade), `compute.py` (**única** implementação de features), `store.py` (Redis) |
| `src/fraud/worker/` | `processor.py` (uma transação), `main.py` (consumo, retry, DLQ, claim, sweeper) |
| `src/fraud/ml/` | dataset por replay, treino, avaliação, modelo, drift, relatórios, avaliação adversarial |
| `src/fraud/simulation/` | mundo sintético, ataques, gerador HTTP, demo |
| `src/fraud/db/`, `alembic/` | modelos SQLAlchemy e migrações |
| `docs/decisions/` | 12 ADRs; leia o ADR do assunto antes de mudar o desenho |

Fluxo: `POST` grava `pending` → `XADD` do id → worker: locks (cliente, recebedor) → relê
linha `FOR UPDATE` → estado do Redis (ou reconstrói do PostgreSQL) → `compute_features` →
`score_one` → commit de decisão e alerta → `apply_transaction` e grava estado → `XACK`.

## Regras que nunca se quebram

1. **Uma implementação de features.** Todo cálculo fica em `features/compute.py` e é usado
   pelo replay de treino e pelo worker. Nunca recalcule feature em pandas, SQL ou no worker.
2. **Calcular antes de aplicar.** `compute_features` lê o estado de antes da transação;
   `apply_transaction` vem depois. A transação nunca entra no próprio histórico.
3. **Features são puras.** Sem I/O e sem `time.time()`: o tempo é `tx.ts`. Toda feature
   devolve número finito e tem valor neutro para entidade sem histórico.
4. **Estado limitado.** O(1) por transação, tamanho com teto, serializável em JSON (listas e
   dicts). Campo novo sempre com default.
5. **PostgreSQL é a verdade; o Redis pode ser apagado.** A mensagem da fila leva só o id.
6. **Ordem no worker:** commit no banco → estado no Redis → `XACK`. Não reordene.
7. **Processamento idempotente:** checagem de `scored` sob `FOR UPDATE`, alerta com
   `ON CONFLICT DO NOTHING`, ids aplicados no estado. Locks sempre em ordem lexicográfica.
8. **Divisão temporal, nunca aleatória.** O bloco de teste não escolhe nada. Limiares e
   calibração vêm da validação e vivem no artefato; nunca fixe limiar no código.
9. **Alvos adversariais não se afrouxam** (`TARGET_*`, `MAX_LEGIT_FPR`, pisos furtivos) para
   um teste passar. Populações de treino e de avaliação usam sementes diferentes.
10. **Resultado bom demais é suspeito.** Antes de comemorar, leia as explicações TreeSHAP das
    transações sinalizadas: o modelo já aprendeu quatro defeitos do simulador.
11. **Esquema só muda por migração Alembic**, com `downgrade`.
12. **Todo erro da API usa o envelope** de `api/errors.py` (subclasses de `AppError`).
13. **Métricas só em `observability/metrics.py`**, sem ids em rótulos.
14. **Não edite à mão** `docs/FEATURES.md`, `docs/MODEL_REPORT.md` nem
    `docs/adversarial_runs/latest.*` (gerados). `docs/adversarial_runs/00_*` a `04_*` são
    registro histórico: não regenere nem apague.
15. **Os testes de paridade ficam:** `test_online_features_match_offline_replay`,
    `test_online_and_batch_scores_agree`, `test_fast_iforest_matches_sklearn`,
    `test_transaction_never_sees_itself`.

## Armadilhas

- `make train` e `docker compose run` usam a imagem já construída. Mudou código? `make up`
  (reconstrói) e só então `make train`. O `make up` só treina se não houver modelo no volume.
- O modelo do Docker (volume `artifacts`) e o local (`artifacts/model`, usado por
  `make docs`) são arquivos diferentes.
- Worker e API carregam o modelo só na partida. `FraudModel.load` recusa artefato que pede
  feature que o código não calcula: remover ou renomear feature exige retreino.
- Mudar o **cálculo** de uma feature sem mudar o nome não é detectado por nada: retreine
  sempre e regenere os docs.
- Estados já gravados no Redis recebem o default nos campos novos. Depois de mudar
  `state.py` em um ambiente com dados, apague as chaves `state:*` para reconstruir.
- O teste de migrações **não enxerga `CHECK`**. Os enums viram `CHECK` no banco: acrescentar
  valor a um enum de `domain.py` exige migração que recria a constraint, e nenhum teste
  avisa se você esquecer.
- Testes de integração precisam de `make infra`; a fixture trunca `fraud_test` e dá
  `FLUSHDB` no db 15 (não toca nos dados de desenvolvimento). Laço de eventos é de sessão.
- Sistema recém-ligado não tem histórico: todo recebedor parece conta nova e o falso
  positivo dispara. Por isso demo e avaliação têm período de aquecimento.
- A hora do dia é UTC. O sweeper só age com `lag == 0`. O worker processa uma mensagem por
  vez e a inferência bloqueia o laço de eventos (com concorrência, use `asyncio.to_thread`).
- A demo varia entre execuções (dois workers). A imagem Docker não contém `tests/` nem
  `docs/`. O painel fica fora do mypy e da cobertura.
- `sqlalchemy<2.1` é proposital (instrumentação OpenTelemetry).
- Problemas conhecidos e ainda abertos: `docs/KNOWN_ISSUES.md`. Leia antes de "consertar"
  algo que parece bug; pode ser uma decisão registrada em ADR.

## Checklists

### adicionar-feature

1. Precisa de estado novo? Acrescente o campo em `features/state.py` com default, limitado
   e serializável, e atualize-o em `update_customer` ou `update_counterparty` (depois da
   checagem de `applied`, usando `decay` com o `dt` já calculado).
2. Calcule em `compute_features` (ou `_customer_v2` / `_counterparty_v2`), lendo o estado
   de antes. Proteja divisões; defina o valor neutro sem histórico.
3. Registre a `FeatureSpec` **no fim** de `FEATURE_SPECS` com `version` igual à versão
   atual + 1 (não reutilize 1 ou 2: os estágios do relatório adversarial dependem delas).
   `compute_features` precisa preencher todos os nomes de `ALL_FEATURES`.
4. Testes em `tests/unit/test_features.py`: o sinal aparece, o valor é neutro sem
   histórico, a transação não se enxerga. Ajuste `test_feature_registry_is_consistent` se
   criou versão nova.
5. `make test-unit test-integration` (paridade online e offline).
6. `make docs` e depois `make test-adversarial`. Compare com o `MODEL_REPORT.md` anterior:
   PR-AUC, falso positivo na revisão, recall por cenário e os cinco ataques.
7. Se algum número melhorou muito, aplique a regra 10.
8. Atualize as tabelas do README se os números mudaram; no ambiente Docker, `make up` e
   `make train`.

### retreinar-modelo

1. Código mudou? `make up` primeiro (reconstrói a imagem).
2. `make train` (Docker) ou `make docs` (local, também regenera o relatório).
3. Confira o resumo impresso e `GET /model/info`: versão, PR-AUC, limiares, latência.
   As sementes são fixas: com o mesmo código o resultado é idêntico. Se mudou sem você
   querer, algo em features, simulador ou parâmetros mudou.
4. `make test-adversarial`. Não promova modelo que falha em qualquer ataque ou na guarda de
   falso positivo.
5. Depois de reiniciar: `fraud_model_info` mostra a versão nova nas duas réplicas e
   `fraud_score_psi` fica abaixo de 0,25 após algumas centenas de decisões.
6. Atualize README (tabela do modelo e saída da demo) e, se o ciclo adversarial mudou,
   rode `make adversarial-report` (~20 min) e atualize `docs/ADVERSARIAL.md`.

### criar-migracao

1. Altere `src/fraud/db/models.py`.
2. Crie `alembic/versions/000N_descricao.py` copiando o formato de `0002_*`: `revision`
   sequencial ("0003"), `down_revision` apontando para a anterior, `upgrade` e `downgrade`.
   Para rascunhar: `make infra && uv run alembic upgrade head && uv run alembic revision
   --autogenerate -m "descricao"` (atenção: usa o banco de desenvolvimento), depois renomeie
   arquivo e id para o padrão sequencial e revise à mão.
3. Coluna nova em tabela existente: nula ou com `server_default`. Mexeu em enum: recrie a
   constraint `CHECK` na migração (ver armadilha).
4. Se o worker usa a coluna, atualize `Transaction.to_event` e `Processor._history`; se a
   API a expõe, `api/schemas.py`.
5. `make test-integration`: `test_migrations_match_models` precisa passar, e a fixture faz
   `downgrade base` + `upgrade head`, então `downgrade` quebrado derruba a suíte.
6. Atualize `docs/ARCHITECTURE.md` §4. Aplicar no ambiente: `make up` (serviço `migrate`).
7. Em produção: mudanças compatíveis com o código antigo (expandir, implantar, contrair) e
   índice em tabela grande com `CREATE INDEX CONCURRENTLY`.

## Documentos

`docs/ARCHITECTURE.md` (fluxo e concorrência), `docs/decisions/` (por quê),
`docs/ADVERSARIAL.md` (ciclo de ataques), `docs/KNOWN_ISSUES.md` (o que está aberto),
`docs/STUDY_PATH.md` (ordem de leitura do código), `docs/EXTENSIONS.md` (próximos passos),
`docs/INTERVIEW.md` (perguntas e trade-offs).
