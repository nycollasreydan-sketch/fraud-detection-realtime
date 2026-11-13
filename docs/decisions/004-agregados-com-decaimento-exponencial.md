# 004 — Agregados com decaimento exponencial em vez de janelas exatas

**Status:** aceita

## Contexto

As features clássicas de fraude são contagens e somas por cliente em janelas ("transações
na última hora", "valor nas últimas 24h"). Uma janela exata exige guardar os eventos da
janela.

## Decisão

Cada agregado é um único número com decaimento exponencial:

    c(t) = c(t0) · 2^(-(t - t0)/h) + x

onde `h` é a meia-vida (1h, 24h, 7d, 30d, 90d). O estado de um cliente é um JSON pequeno
de tamanho limitado: os agregados, um histograma de 24 horas, as últimas 8 transações e
no máximo 48 recebedores e 8 aparelhos (os mais antigos são descartados).

## Por quê

- **O(1) em tempo e memória**, independentemente de quantas transações o cliente faz. Um
  cliente com 10 mil transações por dia custa o mesmo que um com duas.
- **Estado serializável e pequeno** (teste garante menos de 8 kB): uma leitura e uma
  escrita no Redis por entidade.
- **Média e desvio também saem de graça**: guardando contagem, soma e soma dos quadrados
  com a mesma meia-vida, a razão entre eles é uma média móvel exponencial, que é a linha
  de base contra a qual o valor da transação é comparado.
- **Não há efeito de borda**: numa janela exata, uma transação de 59 minutos atrás conta
  inteira e uma de 61 não conta nada.

## Alternativas descartadas

- **Sorted sets no Redis com `ZRANGEBYSCORE`.** Janela exata, mas memória e custo crescem
  com o volume do cliente e é preciso podar. Vale a pena quando a regra precisa ser exata
  por razão regulatória ("mais de 3 em 24h").
- **Consultar o PostgreSQL a cada transação.** Simples, mas põe uma agregação SQL no
  caminho crítico de latência.
- **Um motor de streaming (Flink, Kafka Streams).** A resposta certa em grande escala e
  desproporcional aqui.

## Consequências

- "Contagem com meia-vida de 1h" não é "contagem na última hora". O modelo aprende sobre a
  grandeza que recebe; para um analista lendo a explicação, a descrição da feature diz
  "meia-vida".
- Detecção de duplicata exata (replay) não se faz com agregado; por isso o estado guarda
  também as últimas 8 transações. Um atacante que intercale mais de 8 transações entre a
  original e a cópia escapa desse sinal (limite conhecido).
- Eventos fora de ordem não podem gerar decaimento negativo: `decay` ignora `dt <= 0` e o
  relógio do estado nunca volta (há teste para isso).
