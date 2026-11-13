# Cinco extensões para continuar o projeto

Da mais fácil à mais difícil. Cada uma fecha um item de
[KNOWN_ISSUES.md](KNOWN_ISSUES.md), dá para fazer sozinho e rende uma boa história de
entrevista. Todas seguem o mesmo formato: por que importa, o que construir, onde mexer,
critérios de aceite, armadilhas e o que você aprende.

Faça na ordem. As duas primeiras ensinam os caminhos do repositório (comando novo, rota
nova, migração); a terceira repete o ciclo adversarial com você no comando; a quarta e a
quinta mudam o desenho do sistema.

| # | Extensão | Dificuldade | Tempo estimado | Fecha |
|---|---|---|---|---|
| 1 | Comando de *redrive* da dead-letter queue | Fácil | meio dia | 5.1 |
| 2 | Feedback do analista nos alertas | Fácil a média | 1 a 2 dias | 4.2 (parte) |
| 3 | Sexto ataque e features v3 | Média | 3 a 4 dias | Risco residual de ADVERSARIAL.md §8 |
| 4 | Outbox transacional e pausa em falha de infraestrutura | Média a difícil | 1 semana | 1.1, 1.2 |
| 5 | Registro de modelos, modo sombra e retreino com rótulos atrasados | Difícil | 2 a 3 semanas | 4.2, 4.3 |

## 1. Comando de redrive da dead-letter queue

**Por que importa.** Hoje uma mensagem na DLQ fica lá para sempre e a transação fica
`failed`. O ADR 009 já diz que isso "é a próxima coisa que eu faria". Sem redrive, qualquer
incidente termina com alguém escrevendo um script às pressas.

**O que construir.** `python -m fraud.worker.redrive` e um alvo `make redrive`:

- lê o stream `transactions.dlq` com `XRANGE`, em páginas;
- para cada entrada com `transaction_id` válido: volta a linha de `failed` para `pending`
  (limpando `error`), publica de novo com `streams.publish` e remove a entrada com `XDEL`;
- entradas sem id válido (mensagem envenenada de verdade) são contadas e mantidas;
- opções `--dry-run` (só lista), `--limit N` e `--match TEXTO` (filtra pelo campo `error`).

**Onde mexer.** Arquivo novo `src/fraud/worker/redrive.py`; `Makefile`; um contador em
`observability/metrics.py`; testes em `tests/integration/test_worker.py`; uma linha no ADR
009 e no README.

**Critérios de aceite.**

- Teste: uma transação que foi para a DLQ por falha transitória é reprocessada e termina
  `scored`; a DLQ volta ao tamanho anterior.
- Teste: rodar o redrive duas vezes seguidas não decide nada em dobro.
- Teste: a mensagem envenenada continua na DLQ e o comando termina sem erro.
- `--dry-run` não altera banco nem Redis.

**Armadilhas.** O `UPDATE` precisa da condição `status = 'failed'`; sem ela você reabre uma
transação que alguém já reprocessou. Publique depois do commit, pelo mesmo motivo da API.

**O que você aprende.** Operação de filas: por que DLQ sem redrive é só um cemitério, e
como escrever uma ferramenta operacional idempotente.

## 2. Feedback do analista nos alertas

**Por que importa.** O alerta nasce `open` e nunca muda. Sem o veredito do analista não há
rótulo, e sem rótulo o modelo não pode ser avaliado nem retreinado com dados do próprio
sistema. É o primeiro passo do ciclo de vida de ML (e pré-requisito da extensão 5).

**O que construir.**

- `PATCH /alerts/{id}` com `{"status": "confirmed" | "dismissed", "note": "..."}`;
  transição só a partir de `open` (outra tentativa: `409`); `404` no envelope padrão;
- migração `0003`: colunas `resolved_at` (timestamp com fuso, nula) e `resolution_note`
  (texto, nulo) em `alerts`;
- `GET /stats` passa a devolver a precision observada dos alertas resolvidos, por versão de
  modelo;
- no painel, dois botões por alerta e um indicador de precision observada;
- métrica `fraud_alerts_resolved_total{status, severity}`.

**Onde mexer.** `db/models.py`, `alembic/versions/`, `api/schemas.py`, `api/routes.py`,
`dashboard/app.py`, `observability/metrics.py`, `tests/integration/test_api.py`,
`docs/ARCHITECTURE.md` (§4). Siga o checklist `criar-migracao` do CLAUDE.md.

**Critérios de aceite.**

- `test_migrations_match_models` passa; `downgrade` funciona (a fixture de integração faz
  `downgrade base` a cada sessão).
- Testes para: confirmar, descartar, tentar resolver duas vezes (`409`), id inexistente
  (`404`), status inválido (`422`); todos no envelope padrão de erro.
- A precision observada bate com uma conta feita à mão em um teste.

**Armadilhas.** Não acrescente valores a `AlertStatus`: os três já existem e o `CHECK` já
os cobre (ver item 1.10 de KNOWN_ISSUES sobre por que mexer em enum é traiçoeiro). Faça a
transição com `UPDATE ... WHERE status = 'open'` e confira as linhas afetadas; ler e depois
gravar abre uma corrida entre dois analistas.

**O que você aprende.** O caminho completo de uma mudança de API com migração, e o conceito
de rótulo com viés de seleção: só o que o modelo alertou é revisado, então a precision é
observável e o recall não é.

## 3. Sexto ataque e features v3

**Por que importa.** A seção 8 de ADVERSARIAL.md diz como um atacante que conhece as
features v2 as contorna: fracionar para **várias** contas-laranja em vez de uma. Cada conta
recebe pouco (`c_cp_cnt_24h` e `cp_new_senders_24h` ficam baixos) e o sinal por recebedor
some. Esta extensão é rodar o ciclo adversarial de novo, com você no comando.

**O que construir.**

1. O ataque `fan_out` em `simulation/attacks.py`: a vítima transfere valores típicos para
   de 5 a 12 contas novas diferentes, com `stealth` controlando ritmo e número de contas.
   Registre em `ATTACK_LABELS` e `_GENERATORS`.
2. **Meça antes de corrigir.** Rode a bateria contra o modelo atual e anote detecção e valor
   evitado. Espere um resultado ruim; se vier bom, desconfie (ver armadilhas).
3. Features v3 em `features/compute.py`, com `version=3`. Candidatas: valor recente enviado
   a recebedores inéditos em relação ao gasto diário habitual; número de recebedores
   distintos nas últimas transações; fração do gasto de 24 h que foi para contas com menos
   de N dias. Siga o checklist `adicionar-feature` do CLAUDE.md.
4. Meça de novo em três condições: features v3 sem o ataque no treino, com o ataque no
   treino e *leave-one-out* (o `adversarial_report.py` precisa conhecer o estágio v3).
5. Acrescente o ataque à suíte (`tests/adversarial`) e uma seção em ADVERSARIAL.md com os
   três números e o que você aprendeu.

**Onde mexer.** `simulation/attacks.py`, `features/state.py`, `features/compute.py`,
`ml/adversarial_report.py` (`STAGES`), `tests/unit/test_features.py`,
`tests/unit/test_simulation.py`, `docs/ADVERSARIAL.md`; regenere `docs/FEATURES.md` e
`docs/MODEL_REPORT.md` com `make docs`.

**Critérios de aceite.**

- O ataque novo atinge o alvo (90% dos episódios, 70% do valor) **sem** derrubar nenhum dos
  cinco antigos e com falso positivo em legítimas de até 0,5%.
- Há um teste unitário por feature nova mostrando o sinal e o valor neutro sem histórico.
- O número do *leave-one-out* está no documento, mesmo que seja ruim.
- `test_online_features_match_offline_replay` continua passando.

**Armadilhas.** A mais importante do projeto: resultado bom demais quase sempre é atalho do
simulador. Se o modelo pegar 100% na primeira transação do ataque, leia as explicações das
transações sinalizadas e procure a feature que não deveria estar lá. O tráfego legítimo
precisa ter o comportamento equivalente (alguém pagando várias pessoas novas no mesmo dia:
rateio de conta, pagamento de prestadores), senão o modelo aprende "muitos recebedores
novos é fraude" e o falso positivo real seria enorme. E não afrouxe os alvos em
`ml/adversarial.py` para o teste passar.

**O que você aprende.** O método inteiro: modelo de ameaça, medir antes de corrigir, feature
dirigida por falha, controle com ataque nunca visto, e a disciplina de desconfiar do
próprio resultado.

## 4. Outbox transacional e pausa em falha de infraestrutura

**Por que importa.** São os dois piores comportamentos de resiliência do sistema (itens 1.1
e 1.2 de KNOWN_ISSUES): com o banco fora, a fila inteira vai para a DLQ; e uma publicação
perdida só é recuperada quando a fila drena, o que sob carga contínua pode nunca acontecer.

**O que construir.**

*Parte A, outbox.*

- Tabela `outbox` (id, `transaction_id`, `traceparent`, `created_at`, `published_at` nulo),
  com índice parcial em `published_at IS NULL`.
- `POST /transactions` insere a transação e a linha de outbox **na mesma transação de
  banco** e não fala mais com o Redis. A resposta `503` por fila fora deixa de existir.
- Um *relay* (tarefa no worker ou processo próprio) lê linhas não publicadas com
  `SELECT ... FOR UPDATE SKIP LOCKED`, publica e marca `published_at`. Uma tarefa periódica
  apaga as antigas.
- O sweeper sai. A latência do relay precisa caber no orçamento: use `LISTEN/NOTIFY` do
  PostgreSQL para acordá-lo, com sondagem periódica como rede de segurança.

*Parte B, pausa em falha de infraestrutura.*

- Em `Worker.handle`, separe as exceções: erro de conexão com banco ou Redis não é falha da
  mensagem. O worker não dá ACK, não manda para a DLQ, espera com backoff e tenta de novo
  (disjuntor simples: fechado, aberto, meio aberto).
- Métrica `fraud_worker_paused` (gauge) e contador de aberturas do disjuntor.

**Onde mexer.** `db/models.py` e migração; `api/routes.py`; `worker/main.py`; arquivo novo
`worker/relay.py`; `streams.py`; `tests/integration/`; ADR novo (013) substituindo a parte
do sweeper no ADR 009; ARCHITECTURE.md §3 e §6.

**Critérios de aceite.**

- Teste: com o Redis fora, o `POST` responde `202`; quando o Redis volta, a transação é
  decidida sem intervenção.
- Teste: dois relays concorrentes não perdem linha (publicar duas vezes é aceitável e
  absorvido pela idempotência; perder não é).
- Teste: com o banco indisponível durante o processamento, **nenhuma** mensagem vai para a
  DLQ e todas são decididas depois que ele volta.
- A latência em regime do `make demo` não piora mais que alguns milissegundos; registre o
  antes e o depois.
- O trace continua atravessando a fila (o `traceparent` agora viaja pela tabela).

**Armadilhas.** O outbox garante at-least-once, não exactly-once: o relay pode publicar e
morrer antes de marcar. A idempotência do worker continua necessária. Distinguir "banco
fora" de "erro nesta linha" pelo tipo da exceção é mais difícil do que parece: uma violação
de constraint e uma conexão recusada são ambas `DBAPIError`; teste as duas. E cuidado com a
transação longa no relay segurando locks.

**O que você aprende.** O padrão mais cobrado em entrevistas de sistemas distribuídos
(escrita dupla e outbox), disjuntores, e a diferença entre falha de mensagem e falha de
dependência.

## 5. Registro de modelos, modo sombra e retreino com rótulos atrasados

**Por que importa.** Hoje o modelo é treinado uma vez, em dados com rótulo instantâneo, e
trocado por restart. Em produção nada disso existe: o rótulo de fraude chega semanas depois
(chargeback), um modelo novo precisa provar que é melhor antes de decidir, e é preciso
voltar atrás em segundos. É a maior distância entre este projeto e um sistema real, e fecha
os itens 4.2 e 4.3 de KNOWN_ISSUES.

**O que construir.**

*Parte A, registro e troca a quente.*

- Artefatos em `artifacts/models/<versão>/` e um ponteiro `current` (arquivo JSON com
  campeão e, opcionalmente, desafiante). Inclua na versão o hash do commit e da
  configuração de dados (item 4.4).
- O worker relê o ponteiro periodicamente e troca o modelo em memória sem reiniciar.
- `make promote VERSION=...` e `make rollback`.

*Parte B, modo sombra.*

- O desafiante pontua toda transação, mas só o campeão decide. Tabela `shadow_scores`
  (`transaction_id`, `model_version`, `score`, `decision`).
- Relatório de divergência: em quantas transações os dois discordam, e em que direção.

*Parte C, rótulos atrasados.*

- Tabela `labels` (`transaction_id`, `is_fraud`, `source` = analista ou chargeback,
  `labeled_at`). O simulador ganha um gerador de chargebacks com atraso (por exemplo,
  distribuição com mediana de 20 dias) e o veredito do analista da extensão 2 também vira
  rótulo.
- Montagem do dataset de retreino **a partir do banco**: as features já estão gravadas no
  JSONB de cada transação; junte com os rótulos disponíveis *na data do corte*. Transações
  recentes demais para ter rótulo maduro ficam fora do treino.
- Portão de promoção automático: o desafiante só é promovido se for melhor que o campeão em
  janelas maduras, respeitar o orçamento de falso positivo **e** passar na suíte
  adversarial.

**Onde mexer.** Quase tudo em `ml/`; `worker/processor.py` e `worker/main.py`;
`db/models.py` e migrações; `simulation/`; `dashboard/app.py`; `Makefile`; ADRs novos.

**Critérios de aceite.**

- Trocar de modelo não derruba nenhuma transação: teste de integração que promove no meio de
  um fluxo e confere que toda linha tem `model_version` de uma das duas versões.
- Rollback em um comando, com teste.
- O modo sombra não aumenta o p95 de decisão em mais de 30% (meça; se passar, a pontuação
  do desafiante vai para fora do caminho crítico).
- Teste que prova que o dataset de retreino não usa rótulo com `labeled_at` posterior ao
  corte.
- Um experimento escrito: simule uma mudança de comportamento dos fraudadores no meio do
  período e mostre, com números, o campeão degradando, o PSI acusando, o desafiante
  retreinado recuperando e o portão promovendo.

**Armadilhas.** Três clássicas. *Viés de seleção:* o analista só revê o que o campeão
alertou; treinar só com esses rótulos ensina o modelo novo a imitar o antigo (chargebacks
cobrem parte do que passou; discuta o resto). *Vazamento temporal:* usar um rótulo que só
ficou conhecido depois da data do corte é ver o futuro. *Semântica das features:* as
features gravadas no JSONB foram calculadas pelo código da época; se a fórmula mudou sem
mudar o nome, o dataset mistura duas definições (item 4.5).

**O que você aprende.** MLOps de verdade: versionamento, implantação segura, avaliação com
rótulos atrasados e os vieses que aparecem quando o modelo influencia os próprios dados de
treino. É o assunto que separa quem treinou um modelo de quem manteve um em produção.
