# Registro de decisões (ADRs)

Um arquivo por decisão relevante, no formato: contexto, decisão, alternativas descartadas,
consequências. A ideia é que cada escolha do projeto possa ser explicada, e contestada, a
partir do que está escrito aqui.

| # | Decisão |
|---|---|
| [001](001-stack-e-estrutura.md) | Monólito modular, uma imagem, três processos |
| [002](002-decisao-assincrona-com-redis-streams.md) | Decisão assíncrona com Redis Streams |
| [003](003-dados-sinteticos-e-benchmark-real.md) | Simulador para treinar, dataset real só como benchmark |
| [004](004-agregados-com-decaimento-exponencial.md) | Agregados com decaimento exponencial |
| [005](005-paridade-treino-servico.md) | Uma única função de features para treino e serviço |
| [006](006-modelo-hibrido.md) | Modelo híbrido: o que foi proposto, o que os dados disseram |
| [007](007-limiares-por-orcamento-de-alertas.md) | Limiares por orçamento de alertas, calibração de Platt |
| [008](008-estado-em-redis-com-cache-aside.md) | Estado em Redis como cache, PostgreSQL como verdade |
| [009](009-falhas-retry-dlq-idempotencia.md) | Retry, dead-letter, XAUTOCLAIM, sweeper e idempotência |
| [010](010-observabilidade.md) | Logs JSON, Prometheus e trace propagado pela fila |
| [011](011-ciclo-adversarial.md) | Suíte adversarial como teste automático com alvo numérico |
| [012](012-inferencia-de-uma-linha.md) | Caminho rápido de inferência para uma transação |
