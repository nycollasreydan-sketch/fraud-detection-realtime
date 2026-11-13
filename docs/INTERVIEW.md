# Guia de entrevista

Três partes: o resumo de dois minutos, 25 perguntas prováveis com resposta modelo e os
trade-offs de cada decisão. As respostas estão em primeira pessoa e no tamanho em que
devem ser ditas (30 a 60 segundos). Os números vêm de [MODEL_REPORT.md](MODEL_REPORT.md),
[ADVERSARIAL.md](ADVERSARIAL.md) e do README; confira se ainda batem depois de retreinar.

Regra geral: quando a pergunta tocar num ponto fraco, diga o ponto fraco antes que o
entrevistador o encontre. A lista está em [KNOWN_ISSUES.md](KNOWN_ISSUES.md).

## 1. O projeto em dois minutos

> Construí um sistema de detecção de fraude em tempo real, de ponta a ponta. Uma API FastAPI
> recebe a transação, grava no PostgreSQL e publica o id em um Redis Stream. Um worker
> consome, carrega do Redis o histórico do cliente e do recebedor, calcula 45 features de
> comportamento, roda um LightGBM calibrado e grava a decisão (aprovar, revisar ou bloquear)
> com a explicação feature por feature. Com a fila vazia, a decisão sai em cerca de 15 ms na
> mediana e abaixo de 60 ms no p95, em 2 vCPUs.
>
> Três decisões sustentam o projeto. Primeira: existe uma única função de features, usada no
> treino e no serviço, então não há divergência entre o que o modelo aprendeu e o que ele
> recebe em produção. Segunda: o histórico é mantido como agregados com decaimento
> exponencial, com custo constante por transação, e o Redis é só um cache do que o
> PostgreSQL sabe; ele pode ser apagado. Terceira: tratamento de falhas explícito, com
> retry, dead-letter, reentrega de mensagens órfãs e processamento idempotente, tudo com
> teste de integração contra banco e fila reais.
>
> A parte de que mais me orgulho é o ciclo adversarial. Escrevi cinco ataques desenhados
> para passar pelo modelo, usando o aparelho, o local e os valores normais da vítima. A
> primeira versão deixava passar quatro dos cinco. Criei features a partir de cada falha,
> incluí os ataques no treino e transformei a bateria em suíte de testes com alvo numérico.
> No caminho, quatro resultados estavam bons demais porque o modelo tinha aprendido um
> defeito do meu simulador, e achei os quatro lendo as explicações.
>
> O que eu não afirmo: os dados são sintéticos, então os números absolutos não se
> transferem. A Isolation Forest que propus como parte do modelo não mostrou ganho, e isso
> está registrado. E contra um ataque que o modelo nunca viu, dois dos cinco ficam abaixo do
> alvo.

## 2. Vinte e cinco perguntas

### Arquitetura e sistemas distribuídos

**1. Me conte o caminho de uma transação, do `POST` à decisão.**
O Pydantic valida. A API grava a linha como `pending` e só depois publica o id no stream,
com o contexto de trace; responde `202`. O worker lê pelo consumer group, pega locks do
cliente e do recebedor em ordem lexicográfica, relê a linha com `FOR UPDATE`, carrega os
dois estados com um `MGET` (ou reconstrói do PostgreSQL), calcula as features com o estado
de antes da transação, infere, grava decisão e alerta em uma transação de banco, grava o
estado novo no Redis e dá `XACK`. A ordem banco, Redis, ACK é deliberada: cada falha no
meio deixa um estado que a reentrega conserta.

**2. Por que a decisão é assíncrona? Um cartão não espera.**
Certo, e digo isso no ADR 002: para autorização de cartão a decisão teria de ser síncrona.
O assíncrono desacopla o pico de entrada da capacidade de inferência e me deixou exercitar
fila, reentrega e idempotência. O custo é que o `block` chega depois que a transação foi
aceita, então ele serve para congelar conta e alertar, não para negar a compra. O núcleo
(`Processor.process`) é uma função isolada; em produção eu exporia um caminho síncrono para
a decisão e manteria a fila para alerta e enriquecimento.

**3. Por que Redis Streams e não Kafka?**
Streams com consumer group me dão o que o problema exige: entrega at-least-once com ACK,
lista de pendentes e `XAUTOCLAIM`. E o Redis já estava no stack como cache. O que perco em
relação ao Kafka: particionamento por chave, que daria ordenação por cliente sem lock, e
retenção para reprocessar o histórico. Acima de alguns milhares de transações por segundo,
ou se a ordem por entidade virasse requisito forte, eu trocaria.

**4. A API grava no banco e depois publica. E se morrer no meio?**
A linha fica `pending` sem mensagem. Um sweeper no worker republica pendentes com mais de
60 segundos. Não usei outbox transacional porque o sweeper cobre o mesmo caso com uma
consulta periódica. O limite, que achei revisando o código: o sweeper só age com a fila
drenada, para não republicar o que apenas espera a vez; sob carga contínua um órfão
esperaria indefinidamente. O outbox é a correção rigorosa.

**5. Você garante exactly-once?**
Não, e ninguém garante de ponta a ponta. Garanto at-least-once com efeito idempotente, em
três níveis. Na entrada, `Idempotency-Key` com índice único: repetição devolve a mesma
transação, chave igual com conteúdo diferente dá `409`, e a corrida entre duas requisições
é resolvida pelo `IntegrityError`. Na decisão: transação já `scored` não é decidida de
novo, e o alerta entra com `ON CONFLICT DO NOTHING`. No estado: cada estado guarda os ids
aplicados. Esse terceiro nível é o mais frágil: a lista tem 6 ids, e um recebedor muito
movimentado pode contar uma reentrega em dobro.

**6. Dois workers pegam duas transações do mesmo cliente ao mesmo tempo. O que acontece?**
O segundo espera o lock do cliente. Os locks são sempre adquiridos em ordem lexicográfica,
então não há deadlock entre cliente e recebedor. Há teste com dois workers concorrentes no
mesmo cliente. O que o lock não garante é ordem: se a segunda transação for processada
antes, ela entra no estado antes. O estado usa o tempo do evento e não decai com intervalo
negativo, mas é uma diferença em relação ao treino, que vê tudo ordenado.

**7. Alguém roda `FLUSHALL` no Redis. E agora?**
Perde-se a fila e o cache, e nada mais. As transações estão no PostgreSQL: as pendentes são
republicadas pelo sweeper, e cada estado é reconstruído no primeiro acesso, reaplicando as
últimas 500 transações decididas daquela entidade. A reconstrução é aproximada para quem
tem mais de 500, e todas as entidades ativas reconstroem ao mesmo tempo. Em produção eu
guardaria snapshots do estado no banco.

**8. E se o PostgreSQL ficar dois minutos fora?**
Esse é o pior comportamento do sistema hoje. O worker trata toda exceção como falha da
mensagem: três tentativas em 0,6 segundo e dead-letter. Durante a queda, a fila inteira vai
para a DLQ. Nada se perde, porque as linhas continuam `pending` e o sweeper republica
depois, mas a DLQ fica cheia de mensagens sãs e as decisões atrasam minutos. A correção é
distinguir falha de infraestrutura de falha de mensagem e pausar o consumo em vez de
descartar.

**9. O que é uma mensagem envenenada e como você trata?**
É a mensagem que falha sempre, ou que derruba quem a processa. Três defesas: erro
permanente (id inválido, transação inexistente) vai direto para a DLQ; erro transitório tem
três tentativas com backoff; e mensagem reentregue mais de cinco vezes vai para a DLQ sem
ser processada, o que cobre o caso em que ela mata o worker. A transação fica `failed`, não
some: uma transação sem decisão é o que um fraudador quer.

**10. Como você escalaria isto para 10 mil transações por segundo?**
Primeiro mediria, porque hoje são cerca de 55 por segundo com duas réplicas em 2 vCPUs e o
worker processa uma mensagem por vez. Na ordem: concorrência dentro do worker com a
inferência fora do laço de eventos; particionar a fila por cliente, o que elimina o lock do
cliente; trocar o lock do recebedor por atualização atômica do estado, porque um grande
lojista serializa tudo o que vai para ele; particionar a tabela de transações por mês; e
separar o Redis da fila do Redis do estado. A API não tem estado e escala por réplica.

**11. Vi um `synchronous_commit = off` no worker. Você está perdendo decisões?**
Só naquela transação, e o risco é limitado: se o banco cair antes do flush, a linha volta a
`pending` e é decidida de novo; nada fica decidido em silêncio. Fiz porque o fsync era a
etapa mais cara do worker. O efeito colateral que encontrei depois: o estado no Redis já
contém a transação, e a segunda decisão a enxerga no próprio histórico. É raro, mas viola
um contrato do sistema, e para dinheiro de verdade eu voltaria ao commit síncrono.

**12. Onde o tempo é gasto? Como você chegou a 15 ms?**
Medindo por etapa, com um histograma por estágio do worker. O primeiro benchmark de
inferência deu 11,7 ms, e 90% era a Isolation Forest do scikit-learn, que é otimizada para
lote. Reescrevi o percurso das árvores para uma linha, com teste de equivalência a 1e-12, e
passei a tirar score e explicação da mesma chamada do LightGBM. Caiu para cerca de 4 ms; o
que resta é TreeSHAP. Depois disso o mais caro era o fsync, de onde veio o commit
assíncrono.

### Features e modelo

**13. O que é training-serving skew e como você evita?**
É o modelo receber em produção features calculadas de um jeito diferente do treino, sem
nada quebrar de forma visível. Evito por construção: `compute_features` e
`apply_transaction` são funções puras, sem I/O. O treino as chama num replay ordenado por
tempo com estados em memória; o worker as chama com estados vindos do Redis. Um teste de
integração manda 150 transações por HTTP e compara o vetor gravado pelo worker com o do
replay. Onde ainda vaza: ordem de chegada diferente da ordem do evento, e reconstrução
truncada do estado.

**14. Por que decaimento exponencial e não janela deslizante?**
Janela exata exige guardar os eventos, e o custo cresce com o volume do cliente. Com
decaimento, o contador é um número e a atualização é O(1): multiplico pelo fator do tempo
decorrido e somo um. O estado fica pequeno e serializável. O custo é semântico: "contagem
com meia-vida de uma hora" não é "contagem da última hora", e duplicata exata não sai de
agregado, por isso guardo também as últimas 8 transações.

**15. Como você garante que não há vazamento de informação?**
Em quatro pontos. A feature é calculada com o estado de antes da transação, e só depois o
estado é atualizado; há teste para isso. A divisão é temporal: treino no passado, validação
no meio, teste no futuro. O período de aquecimento é descartado, senão a idade do cliente
vira um relógio da simulação. E o bloco de teste não participa de nenhuma escolha. O
vazamento que existe e está declarado: o mesmo bloco de validação serve para early
stopping, calibração e limiares.

**16. Por que PR-AUC e não ROC-AUC ou acurácia?**
Com 1% de fraude, aprovar tudo dá 99% de acurácia. O ROC-AUC usa a taxa de falso positivo,
que divide por um número enorme de legítimas e esconde o problema: o modelo só com as
colunas da transação tem ROC-AUC de 0,82 e PR-AUC de 0,07. O PR-AUC olha precision e
recall, que é o que a operação sente. Reporto com intervalo de bootstrap porque há poucas
centenas de fraudes no teste.

**17. Como você escolheu os limiares? Por que não 0,5?**
0,5 não significa nada com classe rara e peso de classe. Uso dois limiares, definidos na
validação e gravados no artefato. O de revisão vem de um orçamento: no máximo 0,3% das
legítimas viram alerta, porque o recurso escasso é a atenção dos analistas. O de bloqueio é
o menor score em que a precision passa de 95%. No teste isso dá 95,3% de recall com 77% de
precision na revisão. O passo seguinte seria limiar por custo esperado, usando o valor da
transação.

**18. Por que calibrar, e por que Platt?**
O peso de classe desloca as probabilidades, e eu queria um score interpretável e limiares
estáveis entre retreinos. Platt é uma logística de uma variável sobre a margem: monótona,
não muda a ordenação. A pesquisa inicial previa isotônica, mas com poucas centenas de
fraudes na validação ela cria degraus e empates que deixam o limiar instável. Ressalva: a
calibração vale para a taxa-base do simulador, não para dados reais.

**19. A Isolation Forest ajuda?**
Não demonstrei que ajuda. A hipótese era que um score de anomalia treinado só em legítimas
ajudaria contra ataques que ninguém rotulou. Medi: em distribuição, a diferença está dentro
do intervalo de bootstrap; em ataque inédito, ajuda no replay (80% contra 55%) e atrapalha
na mudança gradual (63% contra 75%). Com 60 episódios e uma semente, isso é ruído. Mantive
porque custa pouco e é desligável por flag, e escrevi no ADR 006 que não a defenderia como
responsável pelo resultado. O ganho vem das features de estado.

**20. Como você lidou com o desbalanceamento?**
Sem reamostragem sintética: SMOTE interpola pontos, e interpolar entre duas fraudes em
features de contagem e de categoria não gera uma fraude plausível. Usei peso de classe
moderado, a raiz da razão de desbalanceamento em vez da razão inteira, para não distorcer
demais as probabilidades, e corrigi o resto com calibração. E escolhi a métrica e os
limiares pensando na classe rara.

**21. Como funciona a explicação, e ela não é um risco?**
`predict` com `pred_contrib=True` devolve as contribuições TreeSHAP em log-odds, e a soma é
a margem do modelo; guardo as cinco maiores em cada transação. Foi a principal ferramenta
de depuração do projeto. O risco é real: hoje a explicação volta no
`GET /transactions/{id}` para quem enviou a transação, o que diz ao atacante exatamente o
que disparou. Em produção ela iria só para o analista.

**22. Me fale do ciclo adversarial. O que você aprendeu?**
Defini o alvo antes de medir: em cada ataque, 90% dos episódios detectados, 70% do valor
evitado e falso positivo de até 0,5%. O v1 falhava em quatro de cinco. Features novas
resolveram conta-laranja sem nenhum exemplo rotulado, porque passei a olhar o recebedor;
replay e mudança gradual só se resolveram com exemplos no treino. O aprendizado maior foi
desconfiar de resultado bom: quatro vezes o modelo tinha aprendido um defeito do simulador,
como "distância zero da última transação significa fraude". E o controle honesto é o
leave-one-attack-out: contra ataque nunca visto, três de cinco atingem o alvo.

**23. Os dados são sintéticos. O que isso invalida?**
Os números absolutos e a calibração. O que se sustenta é o método e as comparações
relativas dentro do mesmo simulador. Para ter uma âncora real, rodei o mesmo código de
treino no dataset da ULB, que não tem cliente nem recebedor: PR-AUC entre 0,74 e 0,77. Com
dados reais, o primeiro problema seria o atraso de rótulos, porque o chargeback chega
semanas depois: treinaria com corte de maturação e avaliaria em janelas já maduras.

**24. Como você detecta drift e o que faz quando ele aparece?**
Sem rótulo, monitoro a distribuição dos scores com PSI contra a referência do treino,
exposto como métrica. Acima de 0,25, a população mudou e o limiar por orçamento deixou de
valer. A resposta é investigar o que mudou, recalibrar o limiar e retreinar. O que falta:
o PSI é por réplica e em memória, não há PSI por feature e não há regra de alerta
configurada. O retreino é um comando, não um processo.

**25. Qual é a parte mais fraca? O que você faria com mais um mês?**
Três coisas, nesta ordem. Segurança: não há autenticação e a explicação volta para o
emissor. Resiliência: a queda do banco manda a fila para a DLQ, e o sweeper não age sob
carga. Ciclo de vida do modelo: não há rótulos de analista, registro de versões nem modo
sombra. Com um mês eu fecharia o ciclo de rótulos e o modo sombra, porque é o que
transforma o projeto de um modelo treinado uma vez em um sistema que melhora.

## 3. Trade-offs de cada decisão

| Decisão (ADR) | O que ganhei | O que paguei | Quando eu mudaria |
|---|---|---|---|
| Monólito modular, uma imagem (001) | Um build, uma versão; API e worker nunca divergem; features sem dependência de web ou banco | Imagem pesada em todo processo; deploy de tudo junto | Equipes separadas para ML e plataforma |
| Decisão assíncrona (002) | Pico de entrada desacoplado da inferência; falha do worker não derruba a API | A decisão não está na resposta; `block` é posterior; o cliente precisa consultar | Autorização em linha: caminho síncrono para decidir, fila para o resto |
| Redis Streams (002) | ACK, pendentes e reentrega sem mais uma peça de infraestrutura | Sem partição por chave (ordem exige lock), sem retenção longa, `MAXLEN` pode descartar | Volume alto ou necessidade de reprocessar histórico: Kafka |
| Mensagem só com o id (002) | O banco é a única verdade; perder a fila não perde transação | Uma leitura a mais no banco por mensagem | Dificilmente |
| Simulador próprio (003) | Cliente, recebedor, aparelho e local; ataques controlados; gabarito perfeito | Números não se transferem; o modelo aprende defeitos do simulador | Assim que houver dado real |
| Decaimento exponencial (004) | O(1) por transação, estado pequeno e limitado | Semântica aproximada; duplicata exige lista à parte (8 itens) | Regra regulatória que exija janela exata |
| Uma função de features (005) | Sem skew por construção; paridade testada | Replay em Python puro no treino (cerca de 20 s para 270 mil transações) | Vários modelos e equipes: feature store |
| LightGBM (006) | Forte em dado tabular, rápido em CPU, TreeSHAP exato | Não modela sequência diretamente; depende de features feitas à mão | Sinal sequencial que as features não capturem |
| Isolation Forest como feature (006) | Um sinal não supervisionado, desligável | Segundo artefato, segundo caminho de inferência, ganho não demonstrado | Sem ganho com várias sementes: remover |
| Limiar por orçamento de alertas (007) | Custo operacional previsível; comparação justa entre modelos | Ignora o valor da transação; vale só para a população da validação | Com matriz de custos: limiar por custo esperado |
| Calibração de Platt (007) | Monótona e estável com poucos positivos | Forma fixa; pode calibrar mal nos extremos | Muitos rótulos: isotônica |
| Estado em Redis como cache (008) | Leitura em um `MGET`; Redis descartável | Reconstrução aproximada; duas escritas para ordenar | Estado caro de reconstruir: snapshots no banco |
| Lock por entidade (ARCHITECTURE §6) | Simples; sem corrida no estado | Recebedor popular vira gargalo; sem *fencing* | Escala: partição por chave ou operação atômica |
| Retry em processo + DLQ (009) | Erro transitório não espera 30 s; envenenada não trava a fila | O `sleep` bloqueia o consumidor; queda do banco enche a DLQ | Agora: disjuntor para falha de infraestrutura |
| Sweeper em vez de outbox (009) | Sem tabela nova nem processo de relay | Janela de 60 s; não age sob carga | Produção: outbox |
| Commit assíncrono na decisão (processor) | Remove o fsync do caminho crítico | Decisão pode ser refeita, com o estado já contaminado | Dinheiro real: commit síncrono |
| Trace propagado pela fila (010) | Um trace cobre API, fila e worker | Campo extra na mensagem; instrumentação manual no worker | Nunca |
| PSI de scores em memória (010) | Drift sem rótulo, sem infraestrutura | Por réplica, zera no restart, sem alerta | Agregar no Prometheus |
| Suíte adversarial como teste (011) | Robustez vira critério de regressão | Minutos de CI; o atacante é o próprio autor | Sempre manter; somar red team externo |
| Caminho rápido de inferência (012) | De 11,7 ms para cerca de 4 ms | Dois caminhos mantidos equivalentes por teste, não por construção | ONNX ou treelite, se a latência apertar |
| Enums como `VARCHAR + CHECK` | Migração simples para acrescentar valor | O teste de migração não enxerga `CHECK` | Teste dedicado para as constraints |
| Features e explicação em JSONB na linha | Auditoria e dataset de retreino sem recomputar o passado | Linha de cerca de 2 kB; tabela cresce rápido | Particionar e mover para armazenamento frio |

## 4. Perguntas para devolver ao entrevistador

- Qual é o atraso típico dos rótulos de fraude de vocês, e como avaliam um modelo antes de
  eles amadurecerem?
- A decisão é em linha com a autorização ou posterior? Qual é o orçamento de latência?
- Como vocês separam regra de modelo, e quem é dono de cada um?
- Quanto do trabalho do time é modelo e quanto é feature e qualidade de dado?
