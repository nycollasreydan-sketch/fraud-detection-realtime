# 006 — Modelo híbrido: o que foi proposto e o que os dados disseram

**Status:** aceita, com uma peça sob ressalva

## Contexto

A Fase A propôs ir além do gradient boosting padrão com quatro peças (RESEARCH.md §6):

1. features de estado em streaming, por cliente e por recebedor;
2. score de uma Isolation Forest (treinada só em legítimas) empilhado como feature;
3. peso de classe moderado, calibração e limiares por orçamento de alertas;
4. explicação por predição com TreeSHAP.

A proposta trazia uma hipótese com critério de falha para a peça 2: não piorar o PR-AUC em
distribuição e melhorar a detecção de ataques ausentes do treino.

## O que foi medido

**Peça 1 (features de estado): é de onde vem quase todo o ganho.** Na ablação
([MODEL_REPORT.md](../MODEL_REPORT.md)), o LightGBM só com as colunas da transação fica
muito abaixo até da regressão logística com features comportamentais. E no ciclo adversarial
foram as features do lado do recebedor que levaram conta-laranja de 68% para 100% sem nenhum
exemplo rotulado ([ADVERSARIAL.md](../ADVERSARIAL.md)).

**Peça 2 (score de anomalia): evidência mista, sem ganho demonstrado.**

| Teste | Sem anomalia | Com anomalia | Leitura |
|---|---|---|---|
| PR-AUC em distribuição (simulador) | ver MODEL_REPORT | ver MODEL_REPORT | Diferença dentro do intervalo de bootstrap |
| Ataques vistos no treino (5 ataques) | 100% de detecção | 100% de detecção | Indistinguível |
| Replay nunca visto, detecção | 55% | **80%** | A favor |
| Mudança gradual nunca vista, detecção | **75%** | 63% | Contra |
| Conta-laranja nunca vista, valor evitado | **91%** | 89% | Empate |
| Dados reais (ULB), PR-AUC em 3 sementes | 0,741 / 0,659 / 0,783 | 0,766 / 0,755 / 0,779 | Média maior e menos variação, com intervalos sobrepostos (75 fraudes no teste) |

A hipótese **não foi confirmada**: o score ajuda em um ataque inédito, atrapalha em outro, e
as diferenças vêm de 60 episódios e uma semente. O dado real sugere menos variância entre
sementes, o que é um indício e não uma prova.

**Peças 3 e 4:** funcionam como planejado; detalhes em [ADR 007](007-limiares-por-orcamento-de-alertas.md)
e [ADR 012](012-inferencia-de-uma-linha.md). O TreeSHAP acabou sendo também a principal
ferramenta de depuração do projeto.

## Decisão

O modelo servido por padrão mantém as quatro peças. A peça 2 fica porque custa pouco depois
do caminho rápido de inferência, não piora nenhuma métrica do modelo final e é desligável
com uma flag (`--no-anomaly`), o que mantém a comparação barata de repetir.

Dito isso, a posição honesta em uma revisão técnica é: **com a evidência atual, eu não
defenderia a Isolation Forest como responsável pelo desempenho do modelo.** O que sustenta o
resultado são as features de estado e o treino adversarial. Antes de levar a peça 2 para
produção eu repetiria a comparação com várias sementes e, de preferência, com rótulos reais.

## Alternativas descartadas

- **RNN de sequência (GRU por cartão).** A alternativa mais forte da literatura. Descartada
  por custo operacional (servir estado recorrente), treino fora da faixa de CPU e
  explicação aproximada. Ver RESEARCH.md §6.2.
- **Média ponderada dos scores supervisionado e de anomalia.** Trata "atípico" como
  "fraudulento" com peso fixo; como feature, o boosting decide em que contexto a anomalia
  importa.
- **Remover a peça 2 de vez.** Defensável pelos números acima. Mantive para que a ablação
  continue sendo um comando, e registrei aqui que o ganho não está demonstrado.

## Consequências

- Dois artefatos no modelo (floresta e boosting) e dois caminhos de inferência a manter
  equivalentes (ADR 012).
- A Isolation Forest é ajustada nas mesmas linhas legítimas em que depois pontua para o
  treino do boosting. O viés é pequeno (256 amostras por árvore), mas existe; o correto
  seria pontuar fora da amostra.
