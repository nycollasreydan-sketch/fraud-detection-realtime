# Fase A — Pesquisa: detecção de fraude em tempo real

Este documento resume o que a literatura e a prática da indústria dizem sobre os quatro
problemas centrais do domínio (desbalanceamento, features comportamentais, drift e latência)
e termina com a proposta de modelo usada no projeto.

> **Como ler as referências.** Os artigos foram localizados por busca e os pontos citados vêm
> dos resumos e das páginas oficiais, não de uma releitura integral de cada PDF. Onde um
> número vem de fonte secundária ou de um único estudo, isso está dito no texto.

## 1. O problema, em uma frase

Decidir em milissegundos, com rótulos que chegam dias depois, sobre um evento que ocorre em
menos de 1% dos casos, contra um adversário que muda de comportamento quando é detectado.
Cada uma dessas quatro restrições quebra uma suposição do aprendizado supervisionado padrão.

## 2. Desbalanceamento de classes

**O que se sabe**

- No dataset público mais usado (ULB/Kaggle, [R1]) há 492 fraudes em 284.807 transações
  (0,172%). Acurácia é inútil: prever "legítima" sempre dá 99,83%.
- **Métrica.** Com classes muito desbalanceadas, a curva ROC é otimista porque a taxa de
  falsos positivos é diluída pela massa de negativos; a curva Precision-Recall expõe o que
  importa para a operação (quantos alertas são fraude de verdade) [R2, R3]. Por isso o
  projeto usa **PR-AUC (average precision)** como métrica principal e reporta precision e
  recall no limiar de operação.
- **Reamostragem vs. custo.** Não existe vencedor geral: a revisão de [R4] conclui que o
  resultado depende do classificador, da métrica e da razão de desbalanceamento, e o estudo
  de [R5] em dados reais de pagamento online mostra ganhos pequenos e inconsistentes de
  reamostragem. Gradient boosting é relativamente robusto ao desbalanceamento [R6].
  SMOTE [R7] cria pontos sintéticos por interpolação, o que faz pouco sentido para features
  categóricas e de contagem, e distorce as probabilidades previstas.
- **Calibração.** Tanto undersampling quanto pesos de classe deslocam as probabilidades
  [R1]; se o score vai virar decisão por limiar de custo, ele precisa ser recalibrado
  [R8].
- **Validação.** Split aleatório estratificado vaza o futuro para o treino e superestima a
  performance; o Fraud Detection Handbook [R9] recomenda validação prequential (treino no
  passado, teste no futuro, com um intervalo de atraso entre os dois).

**Decisão para o projeto**

1. Sem reamostragem sintética. Peso de classe moderado (`scale_pos_weight`) no boosting.
2. Calibração em um bloco temporal separado. O plano inicial era isotônica; na
   implementação virou Platt (sigmoide), pelo motivo registrado no
   [ADR 007](decisions/007-limiares-por-orcamento-de-alertas.md).
3. Limiar definido por **orçamento de alertas** (taxa de falso positivo máxima), não por 0,5.
4. Split temporal treino → validação → teste; nunca aleatório.

## 3. Features de comportamento do cliente

**O que se sabe**

- Uma transação isolada diz pouco; o sinal está no desvio em relação ao histórico do próprio
  cliente. A estratégia clássica é agregar transações por cliente em janelas de tempo
  [R10], formalizada como RFM (recência, frequência, valor monetário) e estendida com
  features de periodicidade de horário por [R11].
- O Handbook [R9] usa exatamente isso como baseline: contagem e valor médio por cliente em
  janelas de 1, 7 e 30 dias, mais risco por terminal com atraso de rótulo.
- Na indústria, a mesma ideia aparece como *feature store* online com agregados de
  velocidade. A Feedzai [R12] mostra a alternativa de aprender o estado direto da sequência
  com GRUs, guardando o estado recorrente por cartão, e relata ganho de recall sobre modelos
  com features manuais.
- Fraude por conta-laranja (money mule) não aparece no histórico do pagador: o sinal está no
  **recebedor** (muitos remetentes distintos em pouco tempo, conta nova ou dormente que
  passa a receber volume) [R13]. Esse ponto foi incorporado depois, na Fase D.

**Decisão para o projeto**

- Estado por cliente mantido de forma incremental, com **agregados de decaimento
  exponencial** em vez de janelas exatas: memória e custo O(1) por transação, sem guardar a
  lista de eventos (ver [ADR 004](decisions/004-agregados-com-decaimento-exponencial.md)).
- **Uma única implementação** do cálculo de features, usada no treino (replay offline) e no
  worker (online), para eliminar o *training-serving skew*
  ([ADR 005](decisions/005-paridade-treino-servico.md)).

## 4. Drift de modelo

**O que se sabe**

- Fraude é não estacionária por construção: hábitos de consumo mudam (drift de covariáveis)
  e fraudadores se adaptam ao detector (drift de conceito adversarial) [R14].
- Dal Pozzolo et al. [R15, R16] mostram que o problema real tem **latência de verificação**:
  só uma pequena parte das transações é verificada rápido por analistas (feedback), e o
  restante só ganha rótulo confiável depois de dias ou semanas (chargeback). A estratégia
  vencedora do artigo é treinar um classificador para cada fonte de rótulo e combinar as
  saídas.
- Como os rótulos atrasam, o monitoramento em produção precisa de sinais **sem rótulo**:
  estabilidade da distribuição de scores e das features. O Population Stability Index (PSI)
  é o padrão em risco de crédito; os cortes usuais de 0,1 (atenção) e 0,25 (mudança
  relevante) são regras de bolso, não testes estatísticos, e são criticados por isso [R17].

**Decisão para o projeto**

- O artefato do modelo guarda a distribuição de referência dos scores; o worker calcula o
  **PSI dos scores em janela deslizante** e exporta como métrica Prometheus.
- Um componente **não supervisionado** entra no modelo (seção 6) justamente porque não
  depende de rótulo e reage a padrões nunca vistos.
- Retreino é um comando (`make train`) com versão de modelo gravada em cada predição, o que
  permite comparar versões. Retreino automático e o esquema de dois classificadores de
  [R16] ficaram fora do escopo (ver "Limitações").

## 5. Latência

**O que se sabe**

- A autorização de um pagamento tem orçamento total da ordem de 100 ms. A Stripe afirma que
  o Radar decide em menos de 100 ms [R18]; a Feedzai projeta para requisitos de milissegundos
  [R12].
- A maior parte do orçamento vai para **buscar estado** (features históricas), não para a
  inferência. Por isso os sistemas reais pré-computam agregados e os leem de um store em
  memória.
- A Stripe migrou em 2022 de um ensemble XGBoost + DNN para DNN pura, motivada por escala de
  retreino e por embeddings/transfer learning, e relata que simplesmente remover o XGBoost
  custaria 1,5% de recall [R18]. Ou seja: mesmo na fronteira, árvores continuaram
  competitivas em dados tabulares, e a razão da troca foi operacional.

**Decisão para o projeto**

- Estado em Redis (1 leitura + 1 escrita por entidade), modelo carregado em memória no
  worker, inferência e explicação medidas por histograma Prometheus.
- A decisão é **assíncrona** (API responde 202 e o worker decide), por exigência do stack
  pedido (fila + worker). O trade-off contra decisão síncrona está em
  [ADR 002](decisions/002-decisao-assincrona-com-redis-streams.md).

## 6. Proposta de modelo: ensemble híbrido comportamental

### 6.1 Por que não parar no gradient boosting padrão

"Gradient boosting padrão" aqui significa: GBDT treinado nas colunas da transação, com
limiar 0,5 e sem tratamento de probabilidade. Ele tem três fraquezas neste domínio:

1. **Só enxerga o que foi rotulado.** Um padrão de ataque novo, sem exemplos no treino, cai
   em folhas dominadas por legítimas.
2. **Não tem memória.** Sem features de estado, cada transação é julgada isolada.
3. **Score não é probabilidade.** Com peso de classe, o score sai deslocado, e o limiar vira
   um número mágico.

### 6.2 Por que não uma rede neural de sequência

É a alternativa mais forte da literatura [R12] e foi descartada conscientemente:

- Em dados tabulares de porte médio, árvores ainda superam ou empatam com deep learning
  com muito menos ajuste [R19, R20].
- Exige servir e versionar estado recorrente por cliente, o que multiplica a complexidade
  operacional, e o treino sai da faixa de "roda em CPU no `make demo`".
- Explicação por feature, um requisito do projeto, é exata e barata em árvores (TreeSHAP)
  e aproximada e cara em RNNs.

### 6.3 A proposta

Quatro peças, cada uma respondendo a uma fraqueza:

| # | Peça | Fraqueza que ataca | Fundamentação |
|---|------|--------------------|---------------|
| 1 | Features de estado em streaming, por cliente e por recebedor, com decaimento exponencial | Falta de memória | [R9, R10, R11, R13] |
| 2 | Isolation Forest treinada só em transações legítimas; o score de anomalia entra como feature do boosting (*stacking*) | Cegueira a padrões não rotulados | [R21]; híbridos supervisionado + não supervisionado em [R22] |
| 3 | LightGBM com peso de classe moderado, calibração de probabilidade e limiares por orçamento de alertas | Score sem significado | [R1, R8, R23] |
| 4 | Explicação por predição via TreeSHAP nativo do LightGBM (`pred_contrib`) | Opacidade para o analista | [R24] |

**Por que empilhar o score de anomalia em vez de fazer média de scores.** Uma média com
peso fixo (por exemplo 70/30) trata "anômalo" como "fraudulento", o que gera falso positivo
em qualquer cliente atípico. Como feature, o boosting aprende **em que contexto** a anomalia
importa (anômalo + dispositivo novo é diferente de anômalo + cliente antigo no mesmo
aparelho).

**Hipótese a testar, com critério de falha.** A peça 2 deve (a) não piorar o PR-AUC no
teste em distribuição e (b) melhorar a detecção em ataques ausentes do treino. Se a ablação
não mostrar isso, o resultado será reportado como está.

> **O que os dados disseram depois.** A hipótese foi testada de três formas (ablação no
> simulador, leave-one-attack-out com e sem a peça 2, e benchmark em dados reais). O
> veredito, com os números, está no [ADR 006](decisions/006-modelo-hibrido.md).

### 6.4 Avaliação planejada

- **Ablação em quatro degraus:** regressão logística (baseline) → LightGBM só com colunas da
  transação (boosting padrão) → LightGBM + features comportamentais → modelo proposto.
- **Dados:** simulador próprio com clientes, recebedores e cenários de fraude (necessário
  porque nenhum dataset público tem identificador de cliente e de recebedor), mais um
  benchmark no dataset real da ULB para checar se a peça 2 se sustenta fora do simulador
  ([ADR 003](decisions/003-dados-sinteticos-e-benchmark-real.md)).
- **Robustez:** suíte adversarial da Fase D.

## 7. Robustez adversarial

- Ataques de evasão em dados tabulares têm restrições que imagens não têm: nem todo campo é
  editável pelo atacante e cada mudança tem custo. Cartella et al. [R25] adaptam ataques de
  caixa-preta para fraude e relatam 13,6% de exemplos adversariais aceitos por um sistema
  real de produção. [R26] argumenta que a restrição correta é o **custo para o adversário**,
  não a similaridade.
- Consequência para a Fase D: os ataques simulados são definidos no **espaço do problema**
  (sequências de transações que um fraudador consegue de fato emitir), não como perturbação
  de vetores de features.

## 8. Limitações conhecidas desta pesquisa

- O modelo é validado principalmente em dados sintéticos. Números absolutos de PR-AUC não se
  transferem para produção; o que se transfere são o método, a arquitetura e as comparações
  relativas.
- Rótulos chegam instantaneamente no simulador. O atraso de verificação [R16] está descrito,
  mas não simulado.
- Features de grafo além de um salto (anéis de laranjas, comunidades) não foram
  implementadas.

## Referências

- **[R1]** Dal Pozzolo, Caelen, Johnson, Bontempi. *Calibrating Probability with
  Undersampling for Unbalanced Classification.* IEEE SSCI, 2015. Origem do dataset
  [Credit Card Fraud Detection (ULB/Kaggle)](https://www.kaggle.com/datasets/mlg-ulb/creditcardfraud).
- **[R2]** Davis, Goadrich. *The Relationship Between Precision-Recall and ROC Curves.*
  ICML, 2006.
- **[R3]** Saito, Rehmsmeier. *The Precision-Recall Plot Is More Informative than the ROC
  Plot When Evaluating Binary Classifiers on Imbalanced Datasets.* PLoS ONE, 2015.
- **[R4]** Feng, Zhou, Tong. *Imbalanced classification: a paradigm-based review.* 2020.
  <https://arxiv.org/abs/2002.04592>
- **[R5]** *Evaluating resampling methods on a real-life highly imbalanced online credit
  card payments dataset.* 2022. <https://arxiv.org/abs/2206.13152>
- **[R6]** *Fraud Detection Using Optimized Machine Learning Tools Under Imbalance Classes.*
  2022. <https://arxiv.org/abs/2209.01642>
- **[R7]** Chawla, Bowyer, Hall, Kegelmeyer. *SMOTE: Synthetic Minority Over-sampling
  Technique.* JAIR, 2002.
- **[R8]** Niculescu-Mizil, Caruana. *Predicting Good Probabilities with Supervised
  Learning.* ICML, 2005.
- **[R9]** Le Borgne, Siblini, Lebichot, Bontempi. *Reproducible Machine Learning for Credit
  Card Fraud Detection: Practical Handbook.* ULB, 2022.
  <https://fraud-detection-handbook.github.io/fraud-detection-handbook/>
- **[R10]** Whitrow, Hand, Juszczak, Weston, Adams. *Transaction aggregation as a strategy
  for credit card fraud detection.* Data Mining and Knowledge Discovery, 2009.
- **[R11]** Bahnsen, Aouada, Stojanovic, Ottersten. *Feature engineering strategies for
  credit card fraud detection.* Expert Systems with Applications, 2016.
- **[R12]** Branco, Abreu, Gomes, Almeida, Ascensão, Bizarro. *Interleaved Sequence RNNs for
  Fraud Detection.* KDD, 2020. <https://arxiv.org/abs/2002.05988>
- **[R13]** Material de fornecedores sobre detecção de contas-laranja (padrão fan-in,
  conta dormente que passa a movimentar): [Didit](https://didit.me/blog/mule-account-detection/),
  [LexisNexis / Metro Bank](https://risk.lexisnexis.co.uk/-/media/files/business/case-study/lnrs_metro_bank_cs_uk.pdf).
  Fontes comerciais: usadas para a tipologia, não para números.
- **[R14]** Gama, Žliobaitė, Bifet, Pechenizkiy, Bouchachia. *A Survey on Concept Drift
  Adaptation.* ACM Computing Surveys, 2014.
- **[R15]** Dal Pozzolo, Boracchi, Caelen, Alippi, Bontempi. *Credit card fraud detection
  and concept-drift adaptation with delayed supervised information.* IJCNN, 2015.
  <https://boracchi.faculty.polimi.it/docs/2015_04_Credit_Card_Fraud_Detection_DalPozzolo_Boracchi_Caelen_Alippi_Bontempi.pdf>
- **[R16]** Dal Pozzolo, Boracchi, Caelen, Alippi, Bontempi. *Credit Card Fraud Detection: A
  Realistic Modeling and a Novel Learning Strategy.* IEEE TNNLS, 2017.
  <https://boracchi.faculty.polimi.it/docs/2017_FraudsTNNLS.pdf>
- **[R17]** *A critical review of existing and new population stability testing procedures
  in credit risk scoring.* 2023. <https://arxiv.org/abs/2303.01227>
- **[R18]** Stripe. *How we built it: Stripe Radar.* 2023.
  <https://stripe.com/blog/how-we-built-it-stripe-radar>
- **[R19]** Grinsztajn, Oyallon, Varoquaux. *Why do tree-based models still outperform deep
  learning on typical tabular data?* NeurIPS, 2022.
- **[R20]** Shwartz-Ziv, Armon. *Tabular data: Deep learning is not all you need.*
  Information Fusion, 2022.
- **[R21]** Liu, Ting, Zhou. *Isolation Forest.* ICDM, 2008.
- **[R22]** Exemplos de híbridos: *ChequeMark* (XGBoost + Isolation Forest),
  <https://arxiv.org/abs/2608.21629>; autoencoder + XGBoost,
  <https://f1000research.com/articles/14-664/v2>. Estudos isolados, citados como evidência
  de que o desenho é usado, não de que é superior.
- **[R23]** Ke et al. *LightGBM: A Highly Efficient Gradient Boosting Decision Tree.*
  NeurIPS, 2017.
- **[R24]** Lundberg et al. *From local explanations to global understanding with
  explainable AI for trees.* Nature Machine Intelligence, 2020.
- **[R25]** Cartella, Anunciação, Funabiki, Yamaguchi, Miyao, Abe. *Adversarial Attacks for
  Tabular Data: Application to Fraud Detection and Imbalanced Data.* SafeAI, 2021.
  <https://arxiv.org/abs/2101.08030>
- **[R26]** Kireev, Kulynych, Troncoso. *Adversarial Robustness for Tabular Data through
  Cost and Utility Awareness.* NDSS, 2023. <https://arxiv.org/abs/2208.13058>
