# Problemas conhecidos

Resultado de uma revisão do repositório inteiro feita com o olhar de um entrevistador sênior
de fintech: o que quebra em produção, o que não escala e o que um revisor atento
perguntaria. Complementa a seção "Limitações conhecidas" do README e as "Consequências" de
cada ADR; o que já está explicado lá aparece aqui só como referência.

Cada item diz **onde** está, **o que acontece**, **como corrigir** e o **esforço** (P: horas,
M: dias, G: semana ou mais). A ordem dentro de cada seção é de gravidade.

## 0. O que esta revisão já corrigiu

| Problema | Correção | Onde |
|---|---|---|
| A `Idempotency-Key` só comparava 4 campos: a mesma chave com outra moeda, canal, categoria, coordenada ou data era aceita como repetição e devolvia `200` | Compara o payload inteiro; `occurred_at` entra quando o cliente o enviou | `api/routes.py::_same_payload`, 5 testes novos |
| Um artefato de modelo treinado com outra versão do código de features carregava sem erro e mandava **toda** transação para a DLQ com `KeyError` | `FraudModel.load` falha na partida com a lista das features que faltam | `ml/model.py`, 1 teste novo |
| `GET /stats` filtrava só por `created_at`, sem índice que servisse: varredura da tabela a cada atualização do painel | Índice `ix_transactions_created` | migração `0002` |
| PostgreSQL (`fraud:fraud`) e Redis (sem senha) publicados em todas as interfaces | Portas presas a `127.0.0.1` | `docker-compose.yml` |
| Se o laço de consumo saísse por exceção, o worker ficava preso esperando a tarefa de manutenção | `stop.set()` antes do `await` | `worker/main.py::run` |
| `X-Request-ID` do cliente ia para logs e respostas sem limite de tamanho | Truncado em 64 caracteres | `api/app.py` |
| Docstring apontava para um arquivo de teste que não existe | Corrigida | `ml/fast_iforest.py` |

Depois das correções: 107 testes unitários e de integração e os 12 da suíte adversarial
passam; ruff e mypy estrito limpos. A migração `0002` só chega ao banco de desenvolvimento
no próximo `make up`.

## 1. Corretude e consistência

### 1.1 PostgreSQL fora do ar transforma a fila inteira em dead-letter (M)

**Onde:** `worker/main.py::handle`. **O que acontece:** qualquer exceção é tratada como
erro da mensagem. Com o banco fora, cada mensagem gasta três tentativas em 0,6 s e vai para
a DLQ; marcar a linha como `failed` também falha, então ela continua `pending`. O sistema
se recupera sozinho (o sweeper republica quando a fila drena), mas a DLQ enche de mensagens
sãs, o alerta "dlq > 0" dispara em massa e as decisões atrasam minutos. **Correção:**
separar falha de infraestrutura de falha de mensagem. Em erro de conexão com banco ou Redis
o worker para de consumir e espera (disjuntor com backoff), sem ACK e sem DLQ. Ver extensão
4 em [EXTENSIONS.md](EXTENSIONS.md).

### 1.2 O sweeper nunca age sob carga contínua; o stream pode descartar mensagens (M)

**Onde:** `worker/main.py::sweep_pending`, `streams.py::publish`. **O que acontece:** o
sweeper só republica com `lag == 0` (decisão justificada no ADR 009). Se a fila nunca
drena, uma transação cuja publicação falhou espera indefinidamente. Pior: o `XADD` usa
`MAXLEN ~ 200000`, e com mais de 200 mil mensagens acumuladas o Redis apaga as mais antigas
**ainda não entregues**, que caem no mesmo caso. **Correção:** outbox transacional (um
relay publica o que o banco registrou), ou sweeper que compara a idade do `pending` com a
da mensagem mais antiga ainda na fila. Enquanto isso, alertar em `fraud_stream_lag` perto
do `stream_maxlen`.

### 1.3 Decisão perdida pelo commit assíncrono é refeita com o estado contaminado (M)

**Onde:** `worker/processor.py::process` (`SET LOCAL synchronous_commit = off`). **O que
acontece:** se o PostgreSQL cair e perder o commit, a linha volta a `pending`, mas o estado
no Redis já contém a transação. No reprocessamento, `compute_features` recebe um histórico
que inclui a própria transação: `c_dup_count_24h` vira 1, a velocidade sobe, e o contrato
"a transação nunca se enxerga" (ADR 005) é violado sem nenhum erro. A própria ADR 008
descreve esse efeito como o pior caso da ordem inversa de escritas; ele também existe aqui,
só que restrito à queda do banco. **Correção:** commit síncrono (custa o fsync, a etapa
mais cara do worker), ou detectar `tx_id in cs.applied` com a linha ainda `pending` e
reconstruir o estado do PostgreSQL antes de calcular.

### 1.4 A idempotência do estado só lembra das últimas 6 transações (P)

**Onde:** `features/state.py::MAX_APPLIED`. **O que acontece:** se o worker morre entre
gravar o estado e dar `XACK`, a mensagem é reentregue 30 s depois. Um recebedor
movimentado recebe mais de 6 transações nesse intervalo, o id já saiu da lista e a
transação é contada duas vezes no estado dele. **Correção:** marcador por transação no
Redis (`applied:{tx_id}` com TTL de minutos, gravado no mesmo `MULTI` do estado) em vez de
uma lista dentro do estado.

### 1.5 Ordem de chegada não é ordem do evento (G)

**Onde:** `worker/main.py::poll_once`, `features/compute.py`. **O que acontece:** o treino
faz o replay em ordem de tempo do evento; em produção, dois workers processam na ordem em
que pegam as mensagens. Os locks impedem corrida, não inversão. Um evento atrasado é
aplicado sem decaimento (`dt <= 0`) e entra como "o mais recente" em `recent` e
`last_lat/lon`. É uma fonte pequena e real de *training-serving skew* que os testes de
paridade não cobrem, porque enviam cada entidade em ordem. **Correção:** particionar por
cliente (um stream por faixa de hash, ou Kafka com chave), de modo que cada entidade tenha
um único consumidor.

### 1.6 `occurred_at` é controlado pelo cliente e não tem limite no passado (P/M)

**Onde:** `api/routes.py::create_transaction` (só valida o futuro). **O que acontece:**
quem envia a transação escolhe a hora do evento. Datar a transação para o horário habitual
da vítima neutraliza `c_hour_prob`; datar no passado zera o decaimento e esconde a
velocidade. **Correção:** rejeitar eventos mais antigos que uma tolerância configurável
(a demo precisa de 31 dias, então é um parâmetro, não uma constante) e calcular as features
de velocidade com a hora de chegada no servidor.

### 1.7 Reconstrução do estado é aproximada e não tem proteção contra avalanche (M)

**Onde:** `worker/processor.py::_history`. **O que acontece:** depois de perder o Redis, o
estado é refeito com as últimas 500 transações. Para entidades com histórico maior,
`first_ts`, a linha de base de 90 dias e o perfil de horário ficam truncados (cliente
antigo parece mais novo, o que empurra o falso positivo para cima). E todas as entidades
ativas disparam a consulta ao mesmo tempo. **Correção:** snapshot periódico dos estados no
PostgreSQL (uma linha por entidade), usado como ponto de partida da reconstrução.

### 1.8 Lock Redis sem renovação nem *fencing token* (M)

**Onde:** `worker/processor.py::_entity_locks` (`lock_timeout_s = 10`). **O que acontece:**
se o processamento passar de 10 s (pausa de GC, banco lento), o lock expira e outro worker
entra na seção crítica. A decisão está protegida pelo `SELECT ... FOR UPDATE`; o estado no
Redis não, e uma das duas escritas é perdida. **Correção:** gravar o estado com
`WATCH`/versão (compare-and-set) em vez de confiar só no lock.

### 1.9 A moeda é aceita e ignorada (P)

**Onde:** `api/schemas.py::TransactionIn.currency`. **O que acontece:** `1000.00 JPY` e
`1000.00 BRL` produzem as mesmas features de valor. **Correção:** aceitar só `BRL` ou
converter antes de enfileirar.

### 1.10 O teste de migrações não enxerga `CHECK` (P)

**Onde:** `tests/integration/test_migrations.py`. **O que acontece:** os enums são
`VARCHAR + CHECK`, e o texto do `CHECK` no modelo é derivado do enum Python. Acrescentar um
valor a `Channel`, `Decision` ou `AlertStatus` muda o modelo, mas o `compare_metadata` do
Alembic não compara constraints de checagem: o teste passa sem migração e o banco real
rejeita o valor novo com `CheckViolation`. **Correção:** teste que lê `pg_constraint` e
compara com os enums.

### 1.11 Já documentados

Hora do dia em UTC (README); viés pequeno por usar o mesmo bloco de validação para early
stopping, calibração e limiares (ADR 007); Isolation Forest pontuada nas mesmas linhas em
que foi ajustada (ADR 006); duplicata exata só enxerga as últimas 8 transações (ADR 004).

## 2. Segurança e privacidade

| # | Problema | Onde | Correção | Esforço |
|---|---|---|---|---|
| 2.1 | Sem autenticação, autorização nem limite de requisições: qualquer um lê qualquer transação, lista alertas por cliente e injeta transações | `api/` | OAuth2 ou mTLS entre serviços, escopos de leitura separados dos de ingestão, rate limit por credencial | M |
| 2.2 | A explicação por feature volta para quem enviou a transação: é um oráculo para o atacante calibrar o golpe (ele vê exatamente o que disparou) | `GET /transactions/{id}` | Devolver só decisão e score ao emissor; razões apenas para analistas | P |
| 2.3 | O campo `error` da transação guarda `repr(exc)` e é devolvido pela API (pode expor host do banco, SQL, caminho de arquivo) | `worker/main.py::_dead_letter`, `TransactionOut.error` | Guardar um código estável; o detalhe fica no log e na DLQ | P |
| 2.4 | O modelo é um pickle (`joblib`): quem consegue escrever no volume `artifacts` executa código no worker. Também acopla o artefato às versões exatas de scikit-learn e LightGBM | `ml/model.py::load` | Booster em formato texto do LightGBM, o resto em JSON, hash conferido na carga | M |
| 2.5 | Credenciais de desenvolvimento são o *default* de `Settings`; nada impede subir com `FRAUD_ENV=prod` e `fraud:fraud` | `config.py` | Validador que recusa os defaults fora de `dev`; segredos por arquivo ou cofre | P |
| 2.6 | Dado pessoal (cliente, aparelho, coordenada) em tabela, logs e traces sem retenção, mascaramento nem trilha de acesso. Os dados são sintéticos, o desenho não | `db/models.py`, logs do worker | Pseudonimizar ids na borda, política de retenção, acesso auditado (LGPD) | M |
| 2.7 | `/metrics` da API na mesma porta pública das rotas | `api/app.py` | Porta interna separada, como já é no worker | P |

## 3. Escala e desempenho

| # | Problema | Onde | O que acontece ao crescer | Correção | Esforço |
|---|---|---|---|---|---|
| 3.1 | Worker processa uma mensagem por vez | `worker/main.py::poll_once` | Cerca de 55 tx/s com 2 réplicas em 2 vCPUs (README). Um retry com `sleep` segura a fila inteira daquele consumidor | Concorrência dentro do processo por entidade, com a inferência em `asyncio.to_thread`; inferência em lote | M |
| 3.2 | Um lock por recebedor serializa tudo o que vai para ele | `processor.py::_entity_locks` | Um grande lojista vira gargalo global: workers esperam até 5 s, estouram `LockTimeout`, tentam de novo e acabam na DLQ | Estado do recebedor atualizado por operações atômicas (script Lua com decaimento) em vez de ler, modificar e gravar sob lock; ou particionar por recebedor | G |
| 3.3 | `transactions` cresce sem limite, com dois JSONB por linha (da ordem de 2 kB) | `db/models.py` | Índices e vacuum degradam; `/stats` e a reconstrução ficam mais caros | Particionar por mês em `created_at`, mover `features` para armazenamento frio, política de retenção | M |
| 3.4 | Redis único para fila, locks e estado, sem `maxmemory` | `docker-compose.yml` | Ponto único de falha; o estado (alguns kB por entidade, TTL de 60 dias) disputa memória com a fila. Uma política de evicção genérica apagaria o stream | Instâncias separadas por função; réplica com Sentinel; AOF `always` só na fila | M |
| 3.5 | PostgreSQL único, sem réplica de leitura | `docker-compose.yml` | Painel e analistas competem com a escrita do worker | Réplica para `/alerts` e `/stats`; PgBouncer | M |
| 3.6 | `/alerts` usa `OFFSET` e um `COUNT(*)` por página; `/stats` calcula percentis na janela a cada chamada | `api/routes.py` | Custo cresce com a profundidade da página e com o tamanho da janela | Paginação por cursor (`created_at, id`); latência e contadores lidos do Prometheus | P |
| 3.7 | A migração `0002` cria o índice dentro de transação | `alembic/versions/0002_*` | Em tabela grande bloqueia escrita durante a criação | `CREATE INDEX CONCURRENTLY` em `autocommit_block()` | P |
| 3.8 | API em um único processo uvicorn; duas idas ao banco e uma ao Redis por `POST`; sem endpoint em lote | `Dockerfile`, `routes.py` | Teto de ingestão por instância | Mais réplicas atrás de um balanceador (a API não tem estado); ingestão em lote | P |

## 4. Modelo e ciclo de vida de ML

| # | Problema | Onde | Correção | Esforço |
|---|---|---|---|---|
| 4.1 | **Calibração e limiares valem para o simulador.** A taxa de fraude do treino é 1,3%; a real fica uma ordem de grandeza abaixo. A probabilidade "calibrada" e os dois limiares não se transferem | `ml/model.py::train_model` | Recalibrar e redefinir limiares em dados reais antes de qualquer uso; correção de *prior* como paliativo | M |
| 4.2 | **Não existe ciclo de rótulos.** Alertas nascem `open` e nunca mudam (não há endpoint); nada grava o desfecho real; o atraso de rótulos não é simulado | `api/routes.py`, `db/models.py` | Extensões 2 e 5 em [EXTENSIONS.md](EXTENSIONS.md) | M/G |
| 4.3 | **Sem registro de modelos.** Um diretório é sobrescrito a cada treino: não há histórico, rollback, modo sombra nem canário. A troca é um restart, com janela em que duas réplicas servem versões diferentes | `ml/model.py::save`, `Makefile::train` | Diretório por versão com ponteiro `current`, carga a quente, modo sombra | M |
| 4.4 | A versão do modelo é hash de features, parâmetros e tamanho do treino: não inclui o código nem as sementes. Duas lógicas de feature diferentes com os mesmos nomes geram a mesma versão | `ml/model.py::_version` | Incluir hash do commit e da configuração de dados | P |
| 4.5 | A checagem de compatibilidade na carga pega nome de feature ausente, não mudança de significado. Alterar o cálculo de uma feature sem renomear nem retreinar gera skew silencioso | `features/compute.py` | Disciplina de `version` nas `FeatureSpec` (ver CLAUDE.md) e hash do código de features no artefato | P |
| 4.6 | Drift: só o PSI dos scores, em memória e por réplica (zera no restart), sem drift por feature e **sem nenhuma regra de alerta** no Prometheus | `ml/drift.py`, `ops/prometheus.yml` | Regras de alerta (PSI, lag, DLQ, taxa de alerta); PSI por feature a partir do JSONB gravado | P/M |
| 4.7 | O score de anomalia responde por 48,7% do ganho do boosting e a ablação não mostra benefício: o modelo se apoia numa peça cujo valor não foi demonstrado, e que obriga a manter dois caminhos de inferência | ADR 006, `MODEL_REPORT.md` | Repetir a comparação com várias sementes; sem ganho, remover | M |
| 4.8 | Não há camada de regras nem custo na decisão: o limiar ignora o valor da transação, não há lista de bloqueio ou de confiança, e o `block` chega depois que a transação foi aceita (decisão assíncrona) | `ml/model.py::decide` | Limiar por custo esperado (ADR 007), regras determinísticas ao lado do modelo, caminho síncrono para autorização (ADR 002) | M |
| 4.9 | Vaquinhas legítimas com 50% de falso positivo; partida a frio; replay e mudança gradual inéditos abaixo do alvo; atacante e defensor do mesmo autor | README, ADVERSARIAL.md | Já documentados | — |

## 5. Operação e testes

| # | Problema | Correção | Esforço |
|---|---|---|---|
| 5.1 | Não há comando para reprocessar a DLQ | Extensão 1 em [EXTENSIONS.md](EXTENSIONS.md) | P |
| 5.2 | O healthcheck do worker só confere se a porta de métricas responde; um worker travado no laço continua "saudável" | Gauge com o instante do último ciclo de `poll_once` e healthcheck sobre ele | P |
| 5.3 | Sem regras de alerta, sem Alertmanager, sem SLO escrito; logs só em stdout | Regras mínimas: DLQ > 0, lag crescente, p95 de decisão, PSI > 0,25, taxa de alerta fora da faixa | P |
| 5.4 | No desligamento o worker termina o lote inteiro (até 32 mensagens, com retries); o Compose mata em 10 s. Nada se perde (reentrega em 30 s), mas a decisão atrasa | Checar `stop` entre mensagens do lote | P |
| 5.5 | Não há teste de carga nem de caos: o cenário 1.1 (banco fora) e o 3.2 (recebedor quente) não têm teste | Teste de integração que derruba o banco no meio; teste com 200 transações para o mesmo recebedor | M |
| 5.6 | CI sem cobertura mínima exigida, sem auditoria de dependências, sem varredura da imagem; a suíte adversarial retreina o modelo completo a cada execução | `--cov-fail-under`, `pip-audit`, Trivy; cache do modelo por hash do código de features e treino | P |
| 5.7 | A imagem única carrega pandas, scikit-learn completo e Streamlit em todos os processos (ADR 001) | Estágios de build por processo | P |
| 5.8 | `Settings.env` não é usado para nada além de log; `log_level` inválido cai em `INFO` sem aviso | Validar na carga | P |

## 6. Por onde eu começaria

Se o objetivo fosse levar isto a produção, a ordem seria: **2.1** (autenticação) e **2.2**
(parar de devolver a explicação ao emissor), porque são exploráveis hoje; **1.1** e **1.2**,
porque transformam um incidente de infraestrutura em perda de decisões; **4.1** e **4.2**,
porque sem rótulos reais nenhum número do modelo significa nada; e só então escala (3.1 e
3.2), medindo antes com um teste de carga.
