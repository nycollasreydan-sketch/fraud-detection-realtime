# Fase D — Auto-verificação adversarial

Este é o documento do ciclo "atacar o próprio sistema, medir o que passou, corrigir, medir
de novo". Ele registra também os resultados que estavam errados por culpa do simulador,
porque foi desconfiando deles que o trabalho ficou confiável.

**Resumo em cinco linhas**

1. O modelo v1 (features clássicas da literatura) deixava passar 4 dos 5 ataques.
2. Novas features (v2) resolveram conta-laranja sozinhas; fracionamento ficou perto do alvo.
3. Com exemplos dos ataques no treino, o modelo final atinge o alvo nos 5 ataques, inclusive
   em versões mais discretas do que as vistas no treino.
4. Contra um tipo de ataque **nunca visto**, 3 de 5 atingem o alvo; replay e mudança gradual
   não. Esse é o risco residual declarado.
5. No caminho, o simulador ensinou quatro atalhos falsos ao modelo. Todos foram achados
   lendo as explicações por feature de resultados bons demais.

## 1. Modelo de ameaça

Atacante de **caixa-cinza**: não vê o modelo, mas conhece o perfil da vítima e sabe o que
detectores costumam olhar. Por isso todos os ataques usam o aparelho da vítima, a
localização habitual e valores dentro da faixa normal dela. Nenhum dos sinais "de manual"
(aparelho novo, viagem impossível, valor absurdo) dispara.

Os ataques são sequências de transações que um fraudador consegue emitir de fato, não
perturbações matemáticas no vetor de features (RESEARCH.md §7).

| Ataque | O que o atacante faz | Por que deveria passar |
|---|---|---|
| **Fracionamento** | Esvazia a conta em 15 a 40 transferências de valor típico para uma conta nova | Nenhuma transação isolada é grande |
| **Mudança gradual** | Ao longo de dias, paga a mesma conta nova com valores que sobem 10 a 20% por vez, até 8 a 15 vezes o ticket típico | A linha de base "aprende" o novo normal junto com o atacante |
| **Burst em horário atípico** | 6 a 15 compras online na madrugada da vítima, valores comuns | Cada compra parece rotina |
| **Conta-laranja** | Uma conta nova recebe de 10 a 30 vítimas, cada uma transferindo do próprio aparelho (golpe por engenharia social) | Do lado de quem paga, tudo é legítimo |
| **Replay** | Reenvia de 3 a 8 cópias exatas de uma transação legítima recente | Cada cópia é uma transação que o modelo já aprovou |

Cada ataque tem um parâmetro `stealth` (discrição) de 0 a 1: mais discreto significa mais
lento, menor e com menos vítimas. Código em `src/fraud/simulation/attacks.py`.

## 2. Como se mede

A bateria usa **outra população e outras sementes** (800 clientes que o treino nunca viu),
60 episódios por ataque, misturados a cerca de 65 mil transações legítimas. O limiar é o do
modelo, fixado na validação do treino; nada é reajustado na bateria.

- **Detecção por episódio:** o ataque gerou ao menos um alerta?
- **Valor evitado:** fração do valor do episódio do primeiro alerta em diante. Supõe que um
  alerta congela a conta (a pagadora ou, na conta-laranja, a recebedora).
- **Recall por transação:** reportado, mas não é alvo. A primeira transação de um ataque
  bem-feito é indistinguível de uma legítima por construção.
- **Falso positivo em legítimas:** a guarda. Detectar tudo alertando tudo não vale.

Duas baterias: **padrão** (discrição 0 a 0,7, a mesma faixa do treino adversarial) e
**furtiva** (0,7 a 1,0, nunca vista no treino).

## 3. O alvo, definido antes de medir o modelo final

| Critério | Alvo | Por quê |
|---|---|---|
| Detecção por episódio, **em cada ataque** | ≥ 90% | Paridade com a fraude "de manual": o modelo sinaliza mais de 90% dela no teste. Um ataque adaptativo não pode ser mais fácil de passar que o golpe comum. |
| Valor evitado, **em cada ataque** | ≥ 70% | Detectar depois que o dinheiro saiu não serve. 70% exige o alerta no primeiro terço do valor do episódio. |
| Falso positivo em legítimas | ≤ 0,5% | O orçamento de projeto é 0,3% (ADR 007); a folga cobre a mudança de população. |

O alvo vale por ataque, não na média: o atacante escolhe o ponto mais fraco.

Não escolhi 100% porque seria desonesto. As primeiras transações de mudança gradual e de
conta-laranja são, de propósito, idênticas a transações legítimas; qualquer modelo que
"acerte" todas está lendo um defeito do simulador (seção 5).

## 4. O ciclo

Estágios, todos reproduzíveis com `make adversarial-report`:

| Estágio | O que muda |
|---|---|
| `v1` | Ponto de partida: 23 features clássicas (RFM, novidade, geografia), só fraudes de manual no treino |
| `v2_features` | + 22 features criadas a partir das falhas do v1; **nenhum** ataque no treino |
| `v2_adversarial` | + 20 episódios de cada ataque no treino (outra população, outra semente). **Modelo final.** |
| `v2_sem_anomalia` | Modelo final sem o score de anomalia (ablação) |
| `v2_ataque_inedito` | *Leave-one-attack-out*: cada ataque medido por um modelo treinado com os outros quatro |
| `v2_ataque_inedito_sem_anomalia` | O mesmo, sem o score de anomalia |

### 4.1 Rodada 1: o v1 contra os ataques

Bateria padrão: fracionamento 40% de detecção, mudança gradual 12%, conta-laranja 68% (com
só 48% do valor evitado), replay 17%. Apenas o burst (95%) atingiu o alvo, porque as
features de velocidade já existiam.

Leitura: o v1 mede o cliente contra ele mesmo em janelas curtas. Não tem memória longa
(mudança gradual), não acumula por recebedor (fracionamento), não olha quem recebe
(conta-laranja) e não reconhece repetição exata (replay).

### 4.2 Rodada 2: features v2, uma família por falha

| Falha | Features novas | Ideia |
|---|---|---|
| Mudança gradual | `c_slow_amount_z`, `c_fast_slow_shift`, `c_cp_amount_vs_first`, `c_cp_increase_streak` | Uma segunda linha de base com meia-vida de 90 dias, que demora a ser contaminada; e a trajetória de valores para o mesmo recebedor |
| Fracionamento | `c_spend_24h_ratio`, `c_cnt_24h_ratio`, `c_cp_cnt_24h`, `c_cp_amt_24h_ratio`, `c_recent_amount_cv`, `c_new_cp_24h` | Acumulado contra o gasto diário habitual; acumulado para o mesmo recebedor; valores uniformes demais |
| Conta-laranja | `cp_age_days`, `cp_tx_count`, `cp_cnt_1h`, `cp_cnt_24h`, `cp_new_senders_24h`, `cp_new_sender_ratio`, `cp_amt_24h`, `cp_amount_ratio` | Estado do **recebedor**: conta nova com muitos pagadores inéditos |
| Replay | `c_dup_count_24h`, `c_secs_since_dup` | Mesma dupla recebedor + valor nas últimas transações |
| Horário atípico | `c_hour_prob` | Perfil de horário do próprio cliente |

Resultado sem nenhum exemplo de ataque no treino: conta-laranja foi de 68% para **100%**
(82% do valor evitado) e fracionamento de 40% para 85%. Replay (40%) e mudança gradual (5%)
quase não se mexeram.

Isso separa duas situações. Conta-laranja e fracionamento se parecem com fraudes que o
modelo já conhecia (transferências para conta nova), então bastou dar olhos ao modelo.
Replay e mudança gradual não se parecem com nada rotulado: a feature existe, mas o modelo
supervisionado não tem por que associá-la a fraude.

### 4.3 Rodada 3: treino adversarial

Com 20 episódios de cada ataque no treino, o modelo final atinge o alvo nos cinco ataques
nas duas baterias: detecção de 100% e valor evitado entre 88% e 100%, com falso positivo de
0,41% (abaixo da guarda de 0,5%, acima do orçamento de projeto de 0,3%).

**Este número superestima a robustez real**, e é importante dizer por quê: treinar e testar
com o mesmo gerador de ataques mede em parte a capacidade de reconhecer o gerador. O recall
por transação acima de 90% em mudança gradual e conta-laranja é o sintoma: o modelo sinaliza
até as primeiras transações, que deveriam ser indistinguíveis. A bateria furtiva atenua o
problema (parâmetros fora da faixa de treino), mas a estrutura do ataque é a mesma.

### 4.4 O controle honesto: ataque nunca visto

Para cada ataque, um modelo treinado com os outros quatro. Bateria padrão:

| Ataque (nunca visto no treino) | Detecção | Valor evitado | Alvo |
|---|---:|---:|:---:|
| Fracionamento | 100,0% | 96,4% | atingido |
| Conta-laranja | 100,0% | 88,9% | atingido |
| Burst em horário atípico | 96,7% | 80,1% | atingido |
| Mudança gradual | 63,3% | 59,2% | **não** |
| Replay | 80,0% | 46,9% | **não** |

Na bateria furtiva o replay inédito cai para 37% de detecção.

Fracionamento e conta-laranja generalizam porque compartilham estrutura com os ataques que
ficaram no treino (pagamentos repetidos a uma conta recém-criada); não são tão "inéditos"
quanto o nome sugere. Replay e mudança gradual têm assinatura própria, e sem exemplos o
modelo só os pega parcialmente.

**Conclusão operacional:** o que protege contra um golpe novo são as features certas
*mais* a capacidade de rotular e retreinar rápido. O sistema tem a segunda parte como
comando (`make train`), não como processo automático.

## 5. Quatro vezes em que o resultado estava bom demais

Todo dado sintético carrega regularidades que o mundo real não tem, e um modelo de árvores
as encontra. Cada item abaixo foi achado do mesmo jeito: um número suspeito, seguido da
leitura das explicações TreeSHAP das transações sinalizadas.

| # | Sintoma | O atalho | Correção |
|---|---|---|---|
| 1 | v1 pegava **100% dos replays** sem ter nenhuma feature de duplicata | `c_dist_last_km == 0`. No simulador, clientes tinham coordenada com ruído a cada transação e fraudadores repetiam a coordenada exata. O replay copia a coordenada, logo distância zero, logo "fraude". | Clientes e fraudadores passam a usar o mesmo processo de geolocalização. Replay no v1 caiu para 23%: o número verdadeiro. |
| 2 | Modelo final com **100% em tudo**, inclusive na primeira transação de cada ataque | `cp_age_days == 0` em transferência. Contas recebedoras inéditas só existiam em fraude; todo recebedor legítimo já tinha histórico. | Transferências legítimas para contas nunca vistas (outro banco, prestador), parte delas virando contato recorrente. |
| 3 | Primeiras transações ainda sinalizadas em excesso | `c_age_days` era um relógio da simulação (todos os clientes "nasciam" no dia 0 e os ataques só começavam depois do aquecimento); e as contas-laranja recebiam a primeira transferência perto da meia-noite. | Período de aquecimento descartado, 12% dos clientes entrando ao longo do tempo, vítimas pagando no próprio horário habitual, pagamentos parcelados legítimos a contas novas. |
| 4 | 45 a 67% de alerta já na primeira transação dos ataques | Taxa-base: no simulador, uma fração irreal das transferências para conta nova era fraude, e o modelo aprendeu esse prior corretamente. | Mais pagamentos legítimos a contas novas, menos episódios de ataque no treino. |

Os resultados dessas rodadas intermediárias estão em `docs/adversarial_runs/00_*` a `04_*`.
Eles não são reproduzíveis a partir do código atual (o simulador mudou); ficam como
registro. O que é reproduzível é a tabela da seção 6.

Dois aprendizados que valem fora deste projeto:

- **Explicação por feature é ferramenta de depuração, não só de auditoria.** Os quatro
  atalhos apareceram como "a feature mais importante deste alerta não faz sentido".
- **A métrica de detecção por episódio é generosa com conta-laranja.** Com 30 vítimas por
  episódio, basta um falso positivo ao acaso para o episódio contar como detectado (foi o
  que deu 68% ao v1, com recall por transação de 6%). Por isso o alvo exige também o valor
  evitado.

## 6. Resultados completos

Gerado por `make adversarial-report` (cerca de 20 minutos; 14 treinos). Fonte:
`docs/adversarial_runs/latest.json`.

### Bateria padrão (discrição 0,0 a 0,7)

**Detecção por episódio**

| Ataque | v1 | v2_features | v2_adversarial | v2_sem_anomalia | v2_ataque_inedito | v2_ataque_inedito_sem_anomalia |
|---|---:|---:|---:|---:|---:|---:|
| Fracionamento de valores | 40.0% | 85.0% | 100.0% | 100.0% | 100.0% | 100.0% |
| Mudança gradual de comportamento | 11.7% | 5.0% | 100.0% | 100.0% | 63.3% | 75.0% |
| Burst em horário atípico | 95.0% | 98.3% | 100.0% | 100.0% | 96.7% | 98.3% |
| Conta-laranja | 68.3% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% |
| Replay de transação legítima | 16.7% | 40.0% | 100.0% | 100.0% | 80.0% | 55.0% |
| *Falso positivo em legítimas* | 0.31% | 0.19% | 0.41% | 0.47% | 0.42% | 0.48% |

**Valor evitado**

| Ataque | v1 | v2_features | v2_adversarial | v2_sem_anomalia | v2_ataque_inedito | v2_ataque_inedito_sem_anomalia |
|---|---:|---:|---:|---:|---:|---:|
| Fracionamento de valores | 22.5% | 76.3% | 96.7% | 96.7% | 96.4% | 96.3% |
| Mudança gradual de comportamento | 6.4% | 3.9% | 94.6% | 95.2% | 59.2% | 68.8% |
| Burst em horário atípico | 77.2% | 80.5% | 91.3% | 91.7% | 80.1% | 81.0% |
| Conta-laranja | 47.8% | 82.4% | 96.4% | 96.5% | 88.9% | 90.8% |
| Replay de transação legítima | 10.1% | 26.1% | 100.0% | 99.7% | 46.9% | 32.7% |

**Recall por transação**

| Ataque | v1 | v2_features | v2_adversarial | v2_sem_anomalia | v2_ataque_inedito | v2_ataque_inedito_sem_anomalia |
|---|---:|---:|---:|---:|---:|---:|
| Fracionamento de valores | 7.6% | 16.8% | 97.0% | 96.9% | 96.7% | 96.7% |
| Mudança gradual de comportamento | 1.0% | 0.3% | 92.4% | 93.2% | 5.9% | 7.8% |
| Burst em horário atípico | 70.6% | 71.7% | 91.5% | 92.2% | 76.3% | 76.2% |
| Conta-laranja | 5.7% | 21.6% | 97.1% | 97.1% | 28.7% | 44.8% |
| Replay de transação legítima | 6.3% | 18.7% | 99.7% | 99.5% | 42.1% | 29.5% |

### Bateria furtiva (discrição 0,7 a 1,0, nunca vista no treino)

**Detecção por episódio**

| Ataque | v1 | v2_features | v2_adversarial | v2_sem_anomalia | v2_ataque_inedito | v2_ataque_inedito_sem_anomalia |
|---|---:|---:|---:|---:|---:|---:|
| Fracionamento de valores | 43.3% | 60.0% | 100.0% | 100.0% | 100.0% | 100.0% |
| Mudança gradual de comportamento | 15.0% | 3.3% | 100.0% | 100.0% | 66.7% | 75.0% |
| Burst em horário atípico | 95.0% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% |
| Conta-laranja | 30.0% | 80.0% | 100.0% | 100.0% | 98.3% | 100.0% |
| Replay de transação legítima | 6.7% | 25.0% | 100.0% | 100.0% | 36.7% | 28.3% |
| *Falso positivo em legítimas* | 0.36% | 0.19% | 0.40% | 0.47% | 0.41% | 0.47% |

**Valor evitado**

| Ataque | v1 | v2_features | v2_adversarial | v2_sem_anomalia | v2_ataque_inedito | v2_ataque_inedito_sem_anomalia |
|---|---:|---:|---:|---:|---:|---:|
| Fracionamento de valores | 23.2% | 54.1% | 97.5% | 97.4% | 97.2% | 97.4% |
| Mudança gradual de comportamento | 7.1% | 3.3% | 94.2% | 94.7% | 65.1% | 71.5% |
| Burst em horário atípico | 69.9% | 72.3% | 87.7% | 88.3% | 75.3% | 77.7% |
| Conta-laranja | 19.7% | 56.0% | 91.7% | 92.7% | 76.6% | 84.0% |
| Replay de transação legítima | 4.8% | 14.6% | 100.0% | 98.9% | 19.3% | 18.4% |

**Recall por transação**

| Ataque | v1 | v2_features | v2_adversarial | v2_sem_anomalia | v2_ataque_inedito | v2_ataque_inedito_sem_anomalia |
|---|---:|---:|---:|---:|---:|---:|
| Fracionamento de valores | 3.0% | 4.3% | 97.8% | 97.6% | 97.3% | 97.7% |
| Mudança gradual de comportamento | 1.0% | 0.2% | 89.1% | 93.9% | 4.4% | 5.9% |
| Burst em horário atípico | 64.7% | 63.5% | 86.8% | 88.5% | 71.4% | 73.0% |
| Conta-laranja | 3.3% | 15.6% | 92.5% | 93.3% | 26.9% | 42.5% |
| Replay de transação legítima | 4.1% | 10.0% | 99.5% | 98.6% | 14.6% | 12.8% |

## 7. Como isso vira teste automático

`tests/adversarial/test_attack_suite.py` treina o modelo com a configuração padrão e roda as
duas baterias. Há um teste por ataque: se uma mudança em features, simulador ou treino
derrubar a detecção de um ataque abaixo do alvo, é aquele teste que falha, com o número na
mensagem. A guarda de falso positivo é um teste à parte. Roda em `make test`, em
`make test-adversarial` e em um job próprio do CI.

O mesmo resultado é gravado no artefato do modelo a cada treino e aparece no painel e no
[MODEL_REPORT.md](MODEL_REPORT.md).

## 8. Limites e o que eu faria em seguida

- **O atacante é o meu.** Os cinco ataques são os que eu soube imaginar. Um red team
  independente, ou ataques extraídos de casos reais, valeriam mais que qualquer refinamento
  daqui.
- **Uma semente por estágio.** As diferenças entre estágios são grandes (dezenas de pontos),
  mas diferenças pequenas, como as da ablação do score de anomalia, estão dentro do ruído
  de 60 episódios.
- **Replay inédito.** Duplicata exata é uma assinatura determinística; uma regra explícita
  ao lado do modelo cobriria o caso sem depender de rótulos. Não fiz porque no mundo real
  duplicatas legítimas são comuns (duas passagens de ônibus) e o simulador não tem como
  calibrar essa regra.
- **Mudança gradual inédita.** Detecção de ponto de mudança por cliente (CUSUM sobre o
  log-valor) é o candidato natural.
- **Anéis de laranjas.** O estado do recebedor enxerga um salto. Laranjas que repassam para
  outras laranjas exigem features de grafo.
- **Atacante que conhece as features v2.** Fracionar para várias contas-laranja em vez de
  uma, ou intercalar mais de 8 transações entre original e cópia, contorna sinais
  específicos. O ciclo teria de rodar de novo, e é para isso que ele está automatizado.
