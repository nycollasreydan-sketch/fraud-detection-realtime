# 005 — Uma única função de features para treino e serviço

**Status:** aceita

## Contexto

*Training-serving skew* é o defeito mais comum de sistemas de ML em produção: as features
são calculadas com pandas no treino e reescritas em outro código no serviço, e as duas
versões divergem em algum detalhe (um arredondamento, uma janela, uma ordem de eventos). O
modelo passa a receber em produção números que nunca viu no treino, e nada quebra de forma
visível.

## Decisão

Existe **uma** implementação, em `fraud.features.compute`:

- `compute_features(tx, estado_cliente, estado_recebedor) -> dict` lê os estados como
  estavam **antes** da transação;
- `apply_transaction(tx, estado_cliente, estado_recebedor)` atualiza os estados.

As duas são funções puras, sem I/O. O treino as chama em um *replay* do fluxo ordenado por
tempo, com estados em um dicionário (`ml.dataset.ReplayEngine`). O worker as chama com
estados lidos do Redis. O código do cálculo é literalmente o mesmo.

## Como isso é verificado

- `tests/unit/test_ml.py::test_replay_engine_matches_manual_state_handling`: replay offline
  contra o caminho "ler estado, calcular, gravar", com o estado passando por JSON a cada
  passo.
- `tests/integration/test_worker.py::test_online_features_match_offline_replay`: 150
  transações entram por HTTP, passam por banco, fila, worker e Redis, e o vetor de features
  gravado pelo worker é comparado ao do replay offline.
- `tests/unit/test_ml.py::test_online_and_batch_scores_agree`: o score do caminho online
  (`score_one`) é igual ao do caminho em lote (`predict_proba`).

## Alternativas descartadas

- **Pandas/SQL no treino, código próprio no serviço.** Mais rápido de escrever e de rodar
  no treino; é exatamente a origem do skew.
- **Feature store gerenciado (Feast etc.).** Resolve o mesmo problema com definição única
  de features e materialização offline/online; é a escolha natural com várias equipes e
  vários modelos. Para um modelo só, é infraestrutura demais.

## Consequências

- O treino paga o custo de um laço Python por transação (cerca de 20 s para 270 mil
  transações). Aceitável aqui; em escala, o replay seria paralelizado por cliente.
- A feature de uma transação nunca enxerga a própria transação. Isso é contrato da função
  e tem teste (`test_transaction_never_sees_itself`).
- O vetor de features usado em cada decisão fica gravado na transação (`features` JSONB):
  serve para auditoria e para montar datasets de retreino sem recomputar o passado.
