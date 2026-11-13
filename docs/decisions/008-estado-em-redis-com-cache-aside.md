# 008 — Estado em Redis como cache; PostgreSQL como fonte da verdade

**Status:** aceita

## Contexto

O worker precisa do estado do cliente e do recebedor a cada transação, em poucos
milissegundos. O estado é derivado: é o resultado de aplicar, em ordem, as transações já
decididas daquela entidade.

## Decisão

*Cache-aside* com reconstrução:

1. O worker lê os dois estados com um `MGET` (uma ida ao Redis).
2. **Cache hit:** usa.
3. **Cache miss:** consulta no PostgreSQL as últimas 500 transações já decididas da
   entidade (índices `(customer_id, occurred_at)` e `(counterparty_id, occurred_at)`),
   reaplica em ordem e segue. Para uma entidade nova a consulta volta vazia.
4. Depois de gravar a decisão no banco, grava os dois estados com TTL de 60 dias.

O Redis pode ser apagado inteiro sem perda de informação: o estado se reconstrói sob
demanda.

## Alternativas descartadas

- **Estado só no Redis.** Mais simples, mas um `FLUSHALL` ou a perda do nó deixaria o
  modelo cego para todos os clientes ao mesmo tempo: todo mundo viraria "cliente novo".
- **Estado só no PostgreSQL** (uma linha por entidade, `SELECT ... FOR UPDATE`).
  Consistência forte de graça, mas põe leitura e escrita de disco no caminho crítico e
  concentra contenção de linha nos recebedores populares.
- **Recalcular a partir do histórico sempre.** É o passo 3 em todas as transações.

## Consequências

- A reconstrução é aproximada para históricos com mais de 500 transações. Como os agregados
  decaem, o erro fica nas meias-vidas longas e é pequeno; há teste de que a reconstrução
  reproduz as contagens (`test_state_is_rebuilt_from_postgres_after_cache_loss`).
- **Ordem das escritas:** banco primeiro, Redis depois. Se o worker morrer entre as duas, a
  transação está decidida mas o estado não a contém. A reentrega da mensagem encontra a
  transação já `scored` e apenas completa o estado
  (`test_redelivery_repairs_state_lost_between_commit_and_cache_write`). A ordem inversa
  seria pior: o estado já conteria a transação e, no reprocessamento, ela enxergaria a si
  mesma no histórico.
- A taxa de hit e miss por entidade é métrica Prometheus (`fraud_state_cache_total`).
