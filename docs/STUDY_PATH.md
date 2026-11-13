# Roteiro de estudo do código

A ordem em que ler o repositório para entender tudo, arquivo por arquivo. Cada passo tem o
que ler, o conceito a estudar e uma pergunta de verificação. Só avance quando conseguir
responder em voz alta, sem olhar o código. O gabarito está no fim.

A ordem segue o caminho de uma transação (entrada, fila, features, worker), depois volta
para como o modelo é treinado e atacado, e termina na infraestrutura. São 30 passos; em
ritmo de estudo, de 12 a 16 horas.

**Antes de começar:** rode `make up` e `make demo` e deixe o painel (porta 8501) e o Jaeger
(porta 16686) abertos. Ler código de um sistema que você viu funcionar é muito mais fácil.

## Parte 1: visão geral e vocabulário

| # | Leia | Conceito a estudar | Pergunta de verificação |
|---|---|---|---|
| 1 | `README.md` e a saída do `make demo` | O que o sistema promete e como se mede | Por que a "latência sob rajada" (20 s) e a "latência em regime" (15 ms) são tão diferentes, e qual delas mede o tempo de decisão? |
| 2 | `docs/ARCHITECTURE.md`, seções 1 a 3 | Arquitetura orientada a eventos; fonte da verdade contra cache | Quais são os três lugares onde existe estado, e qual deles pode ser apagado sem perda? |
| 3 | `src/fraud/domain.py` | Tipos de domínio independentes de framework | Por que `TxEvent` é um dataclass simples e não um modelo Pydantic ou SQLAlchemy? |
| 4 | `src/fraud/config.py` | Configuração por ambiente (12-factor), `pydantic-settings` | Qual é a diferença entre `worker_max_attempts` e `worker_max_deliveries`? |

## Parte 2: a entrada (API e banco)

| # | Leia | Conceito a estudar | Pergunta de verificação |
|---|---|---|---|
| 5 | `src/fraud/db/models.py`, `alembic/versions/0001_initial.py` e `0002_*` | Modelagem relacional, índices compostos, enums como `VARCHAR + CHECK`, migrações | Que consulta cada um dos quatro índices de `transactions` atende? |
| 6 | `src/fraud/api/schemas.py`, `tests/unit/test_schemas.py` | Validação na borda com Pydantic v2; `Decimal` para dinheiro; `AwareDatetime` | Por que `amount` é `Decimal` na API e no banco, mas `float` em `TxEvent`? |
| 7 | `src/fraud/api/errors.py`, `src/fraud/api/app.py` | Envelope único de erro; middleware; ciclo de vida (`lifespan`); cardinalidade de rótulos de métrica | Por que a métrica HTTP usa o *template* da rota e não o caminho real? |
| 8 | `src/fraud/api/routes.py` (com atenção ao `create_transaction`) | Idempotência de requisição, condição de corrida, escrita dupla (banco e fila) | Duas requisições com a mesma `Idempotency-Key` chegam no mesmo milissegundo. Siga o código: o que cada uma recebe? |
| 9 | `tests/integration/test_api.py` | Teste de integração com dependências reais | O que o teste `test_queue_down_returns_503_but_keeps_transaction` prova sobre a escrita dupla? |

## Parte 3: a fila

| # | Leia | Conceito a estudar | Pergunta de verificação |
|---|---|---|---|
| 10 | `src/fraud/streams.py`, `docs/decisions/002-*.md` | Redis Streams: `XADD`, consumer group, lista de pendentes (PEL), `XACK`; at-least-once | Qual é a diferença entre `lag` e `pending`, e o que cada um diz sobre a saúde do sistema? |

## Parte 4: features (o coração do projeto)

| # | Leia | Conceito a estudar | Pergunta de verificação |
|---|---|---|---|
| 11 | `src/fraud/features/state.py`, `docs/decisions/004-*.md` | Agregados com decaimento exponencial; estado limitado e serializável | Um contador com meia-vida de 1 h vale 4,0. Passam 2 h e chega uma transação. Quanto ele vale depois? |
| 12 | `src/fraud/features/compute.py`, primeira metade (`FEATURE_SPECS`, `compute_features`) | Features RFM, novidade, geografia (haversine), z-score com média e variância decaídas | Por que as features de um cliente sem histórico recebem valores "neutros" em vez de zero ou `NaN`? |
| 13 | `src/fraud/features/compute.py`, segunda metade (`_customer_v2`, `_counterparty_v2`, `update_*`) | Linha de base lenta contra rápida; estado do recebedor (fan-in); idempotência por `applied` | Qual feature v2 foi criada contra cada um dos cinco ataques? |
| 14 | `tests/unit/test_features.py`, `docs/decisions/005-*.md` | Vazamento de informação; *training-serving skew*; teste como especificação | O que exatamente `test_transaction_never_sees_itself` garante, e o que aconteceria com o modelo se ele falhasse? |
| 15 | `src/fraud/features/store.py`, `docs/decisions/008-*.md` | Cache-aside; ordem de escritas entre banco e cache | Por que o worker grava no banco antes do Redis, e não o contrário? |

## Parte 5: o worker

| # | Leia | Conceito a estudar | Pergunta de verificação |
|---|---|---|---|
| 16 | `src/fraud/worker/processor.py` | Seção crítica; lock distribuído; ordenação de locks; `SELECT ... FOR UPDATE`; `ON CONFLICT DO NOTHING` | Liste, em ordem, tudo o que acontece entre adquirir os locks e soltá-los. Em qual ponto uma queda do worker deixa o estado sem a transação? |
| 17 | `src/fraud/worker/main.py`, `docs/decisions/009-*.md` | Retry com backoff; dead-letter; `XAUTOCLAIM`; sweeper; desligamento gracioso | Para cada falha (erro transitório, id inválido, worker morto, publicação que falhou), qual mecanismo atua? |
| 18 | `tests/integration/test_worker.py`, `tests/integration/conftest.py` | Como testar caminhos de falha de forma determinística | Como o teste `test_stale_message_is_claimed_from_dead_consumer` simula um worker morto sem matar processo nenhum? |

## Parte 6: dados e modelo

| # | Leia | Conceito a estudar | Pergunta de verificação |
|---|---|---|---|
| 19 | `src/fraud/simulation/world.py`, `docs/decisions/003-*.md` | Dados sintéticos; negativos difíceis; sementes e reprodutibilidade | Cite três comportamentos legítimos do simulador que parecem fraude, e diga por que eles precisam existir. |
| 20 | `src/fraud/ml/dataset.py` | Replay offline; divisão temporal; período de aquecimento | Por que uma divisão aleatória entre treino e teste inflaria as métricas deste modelo? |
| 21 | `src/fraud/ml/evaluate.py`, `docs/decisions/007-*.md`, `docs/RESEARCH.md` §2 | PR-AUC contra ROC-AUC; limiar por orçamento; bootstrap | O que `threshold_for_fpr(y, scores, 0.003)` devolve, em palavras? |
| 22 | `src/fraud/ml/model.py` | Gradient boosting; `scale_pos_weight`; early stopping; calibração de Platt; TreeSHAP | Em `score_one`, por que a soma das contribuições é igual à margem, e por que isso dispensa uma segunda predição? |
| 23 | `src/fraud/ml/fast_iforest.py`, `docs/decisions/012-*.md` | Como uma Isolation Forest pontua (comprimento médio do caminho); otimização guiada por medição | Por que uma anomalia tem caminho *curto* nas árvores? |
| 24 | `src/fraud/ml/train.py`, `docs/MODEL_REPORT.md`, `docs/decisions/006-*.md` | Ablação; hipótese com critério de falha | Na tabela de ablação, qual comparação mostra de onde vem o ganho, e qual mostra que a Isolation Forest não o explica? |
| 25 | `src/fraud/ml/drift.py` | PSI; monitoramento sem rótulo | Por que monitorar a distribuição dos scores serve como alarme mesmo sem saber quais transações eram fraude? |

## Parte 7: o ciclo adversarial

| # | Leia | Conceito a estudar | Pergunta de verificação |
|---|---|---|---|
| 26 | `src/fraud/simulation/attacks.py`, `docs/ADVERSARIAL.md` §1 a §3 | Modelo de ameaça (caixa-cinza); ataque no espaço do problema, não no vetor de features | O que o parâmetro `stealth` muda em cada ataque, e por que a avaliação usa uma faixa que o treino nunca viu? |
| 27 | `src/fraud/ml/adversarial.py`, `src/fraud/ml/adversarial_report.py`, `tests/adversarial/test_attack_suite.py`, `docs/ADVERSARIAL.md` §4 a §8 | Métrica por episódio; *leave-one-attack-out*; atalhos do simulador | Por que a detecção por episódio sozinha é generosa demais com a conta-laranja, e qual métrica corrige isso? |

## Parte 8: observabilidade, ferramentas e infraestrutura

| # | Leia | Conceito a estudar | Pergunta de verificação |
|---|---|---|---|
| 28 | `src/fraud/observability/` (três arquivos), `docs/decisions/010-*.md` | Logs estruturados; métricas RED; tracing distribuído e propagação de contexto (W3C `traceparent`) | Como o span do worker vira filho do span da API se entre os dois existe uma fila? |
| 29 | `src/fraud/simulation/generator.py`, `src/fraud/simulation/demo.py`, `src/fraud/dashboard/app.py` | Teste de ponta a ponta; cliente HTTP assíncrono | Por que `batches()` nunca coloca duas transações do mesmo cliente ou recebedor no mesmo lote? |
| 30 | `Dockerfile`, `docker-compose.yml`, `Makefile`, `.github/workflows/ci.yml`, `pyproject.toml`, depois `docs/KNOWN_ISSUES.md` | Cache de camadas Docker; ordem de subida com `depends_on`; pipeline de CI | Em que ordem os serviços sobem no `make up`, e o que impede a API de subir antes das migrações? |

## Exercícios de fixação

Depois dos 30 passos, três exercícios que obrigam a ligar as partes:

1. **Siga um trace.** Envie uma transação com `curl`, abra o trace no Jaeger e associe cada
   span a uma linha de código.
2. **Quebre de propósito.** Em `processor.py`, troque a ordem de `compute_features` e
   `apply_transaction` e rode `make test-unit`. Veja quais testes falham e por quê. Desfaça.
3. **Explique um alerta.** Pegue um alerta de severidade alta no painel, leia as razões e
   reconstrua à mão, a partir das fórmulas em `compute.py`, o valor da feature mais
   importante.

## Gabarito

1. A rajada mede tempo de espera na fila (a vazão do worker, cerca de 55 tx/s); a de regime
   mede o tempo de decisão com a fila vazia, que é o que importa.
2. PostgreSQL (transações, decisões, alertas), Redis (fila e estados) e `artifacts/` (o
   modelo). O Redis pode ser apagado: o estado é reconstruído do PostgreSQL e os pendentes
   são republicados pelo sweeper.
3. Porque o cálculo de features o consome milhões de vezes no replay offline e também no
   worker; não pode depender de web nem de banco.
4. `max_attempts` são tentativas dentro do mesmo processo, com backoff, para erro
   transitório. `max_deliveries` é quantas vezes a mensagem pode ser reentregue pelo Redis
   (worker morto) antes de ir para a DLQ.
5. `(customer_id, occurred_at)` e `(counterparty_id, occurred_at)`: reconstrução do estado
   no cache miss. `(status, created_at)`: o sweeper buscando `pending` antigos.
   `(created_at)`: as janelas de tempo do `/stats`.
6. Dinheiro armazenado e trafegado precisa de exatidão decimal. As features são estatísticas
   (logaritmo, média, razão), onde `float` basta e é muito mais rápido.
7. O caminho real tem o id da transação: cada requisição criaria uma série temporal nova e a
   cardinalidade explodiria.
8. Ambas não encontram linha no `SELECT`. As duas tentam o `INSERT`; o índice único deixa
   uma passar (`202`) e a outra recebe `IntegrityError`, faz rollback, relê e devolve a
   linha da primeira com `200`.
9. Que a transação fica gravada como `pending` mesmo com a fila fora: o banco é a fonte da
   verdade e a API avisa o cliente com `503` em vez de fingir sucesso.
10. `lag`: mensagens que ninguém leu ainda (falta capacidade de consumo). `pending`: lidas e
    sem ACK (em processamento, ou presas com um consumidor morto).
11. 4,0 × 0,5² + 1 = 2,0.
12. Porque zero também é um valor informativo (zero segundos desde a última transação é
    rajada). O neutro evita que "sem histórico" se confunda com "comportamento extremo", e
    `NaN` quebraria a regressão logística usada como baseline.
13. Mudança gradual: `c_slow_amount_z`, `c_fast_slow_shift`, `c_cp_amount_vs_first`,
    `c_cp_increase_streak`. Fracionamento: `c_spend_24h_ratio`, `c_cnt_24h_ratio`,
    `c_cp_cnt_24h`, `c_cp_amt_24h_ratio`, `c_recent_amount_cv`, `c_new_cp_24h`. Replay:
    `c_dup_count_24h`, `c_secs_since_dup`. Horário atípico: `c_hour_prob`. Conta-laranja: as
    oito features `cp_*`.
14. Que as features são calculadas com o estado anterior à transação. Se falhasse, o modelo
    treinaria com informação que inclui a própria transação e teria métricas offline
    infladas que não se repetem em produção.
15. Se morrer entre as duas escritas, a transação está decidida e o estado não a contém; a
    reentrega encontra `scored` e só completa o estado. Na ordem inversa, o estado já
    conteria a transação e o reprocessamento a enxergaria no próprio histórico.
16. Relê a linha com lock, carrega os estados, (se já `scored`: completa o estado e sai),
    calcula features, infere, atualiza a linha e insere o alerta, commit, aplica a
    transação ao estado, grava no Redis. Uma queda depois do commit e antes da gravação no
    Redis deixa o estado sem a transação.
17. Erro transitório: retry em processo com backoff. Id inválido: `PermanentError`, direto
    para a DLQ. Worker morto: `XAUTOCLAIM` depois de 30 s ociosos. Publicação que falhou:
    sweeper republica `pending` com mais de 60 s, com a fila drenada.
18. Lê a mensagem com o nome de outro consumidor e nunca dá ACK; com
    `worker_claim_idle_ms` de 50 ms nos testes, o worker de teste a assume com
    `claim_stale()`.
19. Viagens (outra cidade), troca de aparelho, compras grandes ocasionais, transferências
    para contas novas e vaquinhas que recebem de muita gente. Sem eles, qualquer modelo
    teria precision perto de 1 e a avaliação não distinguiria modelo bom de ruim.
20. Transações do mesmo cliente e do mesmo episódio de fraude cairiam nos dois lados; o
    modelo veria o futuro do cliente no treino.
21. O menor score tal que no máximo 0,3% das transações legítimas ficam nele ou acima.
22. O TreeSHAP decompõe a margem (log-odds) em uma parcela por feature mais um termo base;
    a soma reconstrói a margem. Uma chamada com `pred_contrib=True` já entrega score e
    explicação.
23. Cortes aleatórios isolam rapidamente um ponto que está longe dos demais; pontos em
    regiões densas precisam de muitos cortes.
24. Linha 2 contra linha 3 (PR-AUC de 0,07 para 0,97): o ganho vem das features de
    comportamento. Linha 3 contra linha 4: intervalos de bootstrap sobrepostos, sem ganho
    demonstrado.
25. Porque o limiar foi fixado para uma distribuição de scores. Se ela muda (população
    nova, sistema frio, ataque em massa), a taxa de alerta e o falso positivo mudam junto,
    antes de qualquer rótulo chegar.
26. Torna o ataque mais lento, menor e com menos vítimas. A faixa nunca vista mede se o
    modelo generaliza a estrutura do ataque ou decorou os parâmetros do gerador.
27. Com até 30 vítimas por episódio, um único falso positivo ao acaso já "detecta" o
    episódio (o v1 teve 68% assim, com 6% de recall por transação). O valor evitado exige
    que o alerta venha cedo.
28. A API injeta o contexto (`traceparent`) como campo da mensagem do stream; o worker o
    extrai e abre o span com esse contexto como pai.
29. Dentro de um lote as requisições são paralelas e a ordem de chegada é arbitrária; as
    features dependem da ordem das transações de cada entidade.
30. PostgreSQL e Redis; depois `migrate` e `trainer` (que rodam e terminam); depois API e
    worker; por fim o painel. `depends_on` com `service_completed_successfully` segura API
    e worker até migrações e treino acabarem.
