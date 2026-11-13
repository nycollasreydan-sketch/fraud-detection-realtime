# 009 — Retry, dead-letter, XAUTOCLAIM, sweeper e idempotência

**Status:** aceita

## Contexto

Entrega at-least-once significa que toda mensagem pode falhar no meio ou chegar duas vezes.
Cada tipo de falha pede um mecanismo diferente; um só não cobre todos.

## Decisão

| O que deu errado | Mecanismo | Onde |
|---|---|---|
| Erro transitório no processamento (banco piscou, lock ocupado) | Novas tentativas em processo com backoff exponencial (0,2 s, 0,4 s, ...), até 3 | `Worker.handle` |
| Tentativas esgotadas | Mensagem vai para o stream `transactions.dlq` com erro e contagem; transação vira `failed` com o erro gravado; original recebe ACK | `Worker._dead_letter` |
| Erro que nunca vai passar (id inválido, transação inexistente, mensagem sem campo) | `PermanentError`: direto para a DLQ, sem retry | `Processor.process` |
| Worker morreu com mensagens lidas e sem ACK | Outro worker assume com `XAUTOCLAIM` depois de 30 s ociosas | `Worker.claim_stale` |
| Mensagem que derruba quem a processa | Contagem de entregas (`XPENDING`); acima de 5, DLQ | `Worker.handle` |
| API gravou no banco e não conseguiu publicar | API responde 503; o sweeper republica transações `pending` com mais de 60 s, **só quando a fila está drenada** | `Worker.sweep_pending` |
| Cliente HTTP repetiu a requisição | `Idempotency-Key` com índice único: devolve a mesma transação (200); mesma chave com outro conteúdo é 409 | `POST /transactions` |

**Idempotência do processamento**, em dois níveis:

1. Transação já `scored` não é decidida de novo: a reentrega só confere o estado e dá ACK.
   A linha é relida com `SELECT ... FOR UPDATE` depois de adquirir os locks, e o alerta é
   inserido com `ON CONFLICT DO NOTHING`.
2. Cada estado guarda os ids das últimas transações aplicadas; reaplicar a mesma transação
   não conta em dobro.

## Alternativas descartadas

- **Não dar ACK e esperar a reentrega como único retry.** Mistura dois problemas (erro
  transitório e worker morto) e atrasa o retry em 30 s.
- **Retry infinito.** Uma mensagem envenenada trava a fila para sempre.
- **Descartar o que falha.** Uma transação sem decisão é exatamente o que um fraudador
  quer; a DLQ mantém a evidência e a transação fica marcada como `failed`.
- **Outbox transacional** para a escrita dupla banco + fila. É a solução rigorosa: grava a
  intenção de publicar na mesma transação do banco e um relay publica. O sweeper cobre o
  mesmo caso (linha `pending` sem mensagem) com uma consulta periódica e sem tabela nova.
  O custo é a janela de 60 s e a possibilidade de publicar em duplicidade, que a
  idempotência absorve.

## Consequências

- Todos esses caminhos têm teste de integração com Redis e PostgreSQL reais
  (`tests/integration/test_worker.py`), inclusive dois workers concorrentes no mesmo cliente.
- O sweeper só age com `lag = 0`. A primeira versão republicava qualquer `pending` com mais
  de 60 s, e a demo em lote mostrou o defeito: sob carga, transações que só esperavam a vez
  eram republicadas (15% de reentregas inúteis, absorvidas pela idempotência, mas pagas em
  latência). Com a fila drenada, `pending` antigo é órfão de verdade.
- Reprocessar a DLQ é manual (não há comando de *redrive*). É a próxima coisa que eu faria.
- O contador `fraud_worker_failures_total{outcome}` separa `retry`, `dlq` e `claimed`; um
  alerta em `dlq > 0` é o sinal operacional mais importante do sistema.
