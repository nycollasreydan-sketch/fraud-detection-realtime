# 007 — Limiares por orçamento de alertas; calibração de Platt

**Status:** aceita

## Contexto

Um classificador devolve um número; o sistema precisa de uma decisão. O limiar 0,5 não tem
significado nenhum quando a classe positiva é 0,3% dos dados e o treino usou peso de classe.

## Decisão

**Três decisões, dois limiares**, ambos escolhidos no bloco de validação (posterior ao
treino no tempo) e gravados no artefato do modelo:

| Decisão | Regra | Como o limiar é escolhido |
|---|---|---|
| `approve` | score abaixo do limiar de revisão | — |
| `review` | score entre os dois limiares; gera alerta de severidade média | **Orçamento de falso positivo:** o menor limiar em que no máximo 0,3% das transações legítimas viram alerta |
| `block` | score acima do limiar de bloqueio; gera alerta de severidade alta | O menor limiar em que a precision na validação é pelo menos 95% |

**Por que orçamento de falso positivo.** O recurso escasso de uma operação antifraude é a
atenção dos analistas, e o custo de atrito com cliente legítimo cresce com o número de
alertas, não com o F1. Fixar o orçamento torna a pergunta "quanto recall eu compro com N
alertas por dia?" explícita e comparável entre modelos. Com cerca de 2,2 transações por
cliente por dia, 0,3% equivale a uns 7 falsos alertas por mil clientes por dia.

**Calibração de Platt.** O peso de classe desloca as probabilidades. O score passa por uma
regressão logística de uma variável ajustada na validação (`p = σ(a·margem + b)`). É
monótona, então não muda a ordenação (PR-AUC idêntico), e devolve um número interpretável
como probabilidade.

## Alternativas descartadas

- **Limiar 0,5 ou máximo F1.** O primeiro é arbitrário; o segundo otimiza uma média que
  ninguém na operação pediu.
- **Limiar por custo esperado (valor da transação × probabilidade).** É o passo seguinte
  correto: bloquear R$ 5 mil com 20% de chance vale mais que R$ 20 com 60%. Exige uma
  matriz de custos que eu teria de inventar, então ficou fora.
- **Calibração isotônica.** Mais flexível, mas é uma função em degraus: com poucas centenas
  de fraudes na validação ela cria empates de score e limiares instáveis. A recomendação
  de Niculescu-Mizil e Caruana (RESEARCH.md, R8) é justamente Platt quando há poucos dados
  de calibração. A pesquisa inicial previa isotônica; a escolha mudou na implementação.

## Consequências

- O orçamento vale para a distribuição da validação. Na bateria adversarial, com outra
  população, o falso positivo medido foi de cerca de 0,4%, acima dos 0,3% de projeto. Por
  isso a suíte adversarial tem uma guarda própria (0,5%) e o worker exporta o PSI dos
  scores: o limiar precisa ser revisto quando a população muda.
- Usar o mesmo bloco de validação para early stopping, calibração e limiares é um
  vazamento pequeno e conhecido. O bloco de teste não participa de nenhuma dessas escolhas.
