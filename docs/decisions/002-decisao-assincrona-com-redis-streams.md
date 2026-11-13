# 002 — Decisão assíncrona com Redis Streams

**Status:** aceita

## Contexto

Há dois desenhos possíveis para decidir uma transação:

1. **Síncrono:** a API calcula features, infere e responde a decisão na mesma requisição.
   É o que a autorização de um pagamento exige (orçamento da ordem de 100 ms).
2. **Assíncrono:** a API aceita, enfileira e responde `202`; um worker decide.

O enunciado pede fila e worker, então o desenho é o assíncrono. Vale registrar o que se
ganha e o que se perde.

## Decisão

`POST /transactions` grava a transação como `pending`, publica o id em um Redis Stream e
responde `202`. Um worker em um *consumer group* decide e grava o resultado. O cliente
consulta `GET /transactions/{id}`.

A mensagem carrega **só o id** (mais o contexto de trace): o PostgreSQL é a fonte da
verdade e a fila é apenas um aviso de "há trabalho". Perder o Redis não perde transações.

**Por que Redis Streams:**

- *Consumer groups* dão entrega at-least-once com ACK explícito, lista de pendentes por
  consumidor e reentrega (`XAUTOCLAIM`), que é exatamente o que retry e dead-letter
  precisam ([ADR 009](009-falhas-retry-dlq-idempotencia.md)).
- O Redis já está no stack como cache; não entra mais uma peça de infraestrutura.
- Com AOF ligado, a fila sobrevive a um restart.

## Alternativas descartadas

- **Decisão síncrona na API.** Latência menor e contrato mais simples, mas acopla o pico de
  entrada à capacidade de inferência e deixa a API com estado de modelo. Em produção, para
  autorização de cartão, eu usaria o síncrono para a decisão e a fila para tudo que pode
  esperar (alertas, enriquecimento, retreino). O núcleo (`Processor.process`) já é uma
  função isolada, então expor um endpoint síncrono seria uma mudança pequena.
- **Kafka.** Particionamento por chave resolveria a ordenação por cliente sem locks, e a
  retenção permitiria reprocessar o histórico. O custo operacional (brokers, tópicos,
  offsets) não se paga em um projeto que precisa subir com `make up` em um laptop.
- **Redis List (`BRPOP`) ou Pub/Sub.** Sem ACK: mensagem entregue a um worker que morre é
  mensagem perdida.
- **RabbitMQ / Celery.** Resolveriam, mas com mais um serviço e, no caso do Celery, um
  modelo de worker que esconde justamente o que o projeto quer mostrar.

## Consequências

- A decisão não está na resposta do `POST`. Medido no `make demo`: ver README.
- Ordenação por cliente não vem de graça como viria com partições; é garantida por locks
  por entidade no worker ([ARCHITECTURE.md §6](../ARCHITECTURE.md)).
- Escrita dupla (banco, depois fila): coberta pelo sweeper do worker em vez de um outbox
  transacional ([ADR 009](009-falhas-retry-dlq-idempotencia.md)).
