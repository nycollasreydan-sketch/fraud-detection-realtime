# 012 — Caminho rápido de inferência para uma transação

**Status:** aceita

## Contexto

O primeiro benchmark de `score_one` (uma transação, com explicação) deu **p50 de 11,7 ms**.
Medindo por etapa, cerca de 90% do tempo estava em
`IsolationForest.score_samples` do scikit-learn: a função é otimizada para lotes e, para
uma única linha, paga validação de entrada e orquestração de threads.

## Decisão

Duas mudanças no caminho online, ambas com teste de equivalência:

1. **`FastIsolationScorer`** (`ml/fast_iforest.py`): extrai as árvores da floresta treinada
   para listas Python e percorre cada uma diretamente (100 árvores × profundidade ~8).
   Reproduz a fórmula do scikit-learn, inclusive a comparação em `float32`. O teste
   `test_fast_iforest_matches_sklearn` exige diferença menor que 1e-12.
2. **Uma chamada ao LightGBM em vez de duas:** `predict(..., pred_contrib=True)` devolve as
   contribuições TreeSHAP, e a soma delas é a margem do modelo. Score e explicação saem da
   mesma chamada.

O treino e a avaliação em lote continuam usando o scikit-learn; `test_online_and_batch_scores_agree`
garante que os dois caminhos dão o mesmo score.

## Resultado

De 11,7 ms para cerca de 4 ms no p50 (números atuais em
[MODEL_REPORT.md](../MODEL_REPORT.md)). O que resta é quase todo TreeSHAP.

## Alternativas descartadas

- **Explicar só as transações alertadas.** Cortaria o TreeSHAP de 99,7% das transações. Não
  fiz porque a explicação de uma transação aprovada também é útil (auditoria, contestação)
  e a latência atual já cabe no orçamento.
- **Exportar para ONNX / treelite.** Ganho maior, mais uma dependência e mais um formato de
  artefato para versionar.
- **Reduzir o número de árvores.** Troca qualidade por latência sem necessidade.

## Consequências

- Existem dois caminhos de inferência, e a equivalência entre eles é garantida por teste, não
  por construção. É uma dívida assumida conscientemente.
- A inferência roda no laço de eventos do worker (bloqueia por ~4 ms). Como o worker
  processa uma mensagem por vez, não há o que ser bloqueado; com concorrência dentro do
  processo, iria para `asyncio.to_thread`.
