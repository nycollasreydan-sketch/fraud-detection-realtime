# 011 — Suíte adversarial como teste automático, com alvo numérico

**Status:** aceita

## Contexto

Um conjunto de teste, mesmo temporal, só contém fraudes parecidas com as do treino. Ele
responde "o modelo reconhece o que já viu?", e fraude é um problema adversarial: o que
importa é o que acontece quando o fraudador muda de método.

## Decisão

1. Um **simulador de ataque** (`simulation/attacks.py`) gera cinco padrões adaptativos no
   espaço do problema: fracionamento, mudança gradual, burst em horário atípico,
   conta-laranja e replay. Todos evitam os sinais óbvios.
2. Os ataques rodam como **suíte de testes** (`tests/adversarial`), contra o modelo treinado
   com a configuração padrão, em outra população e com outras sementes.
3. Há um **alvo numérico por ataque**, fixado antes de medir o modelo final: detecção por
   episódio ≥ 90%, valor evitado ≥ 70%, falso positivo em legítimas ≤ 0,5%. Justificativa
   em [ADVERSARIAL.md §3](../ADVERSARIAL.md).
4. O ciclo completo é **reproduzível** com um comando (`make adversarial-report`), incluindo
   um controle *leave-one-attack-out*.

## Alternativas descartadas

- **Avaliar só no conjunto de teste.** Foi o que o v1 fez: PR-AUC alto e 4 de 5 ataques
  passando.
- **Ataques por perturbação de features (ZOO, HopSkipJump).** Padrão em visão computacional,
  mas em dados tabulares geram vetores que nenhum fraudador consegue produzir: as features
  de histórico não são editáveis diretamente, só por meio de transações.
- **Alvo na média dos ataques.** Esconderia um ataque com 40% atrás de quatro com 100%.
- **Só recall por transação.** Penaliza o modelo por não adivinhar a primeira transação de um
  ataque que, por construção, é idêntica a uma legítima; e incentiva exatamente os atalhos
  de simulador descritos em ADVERSARIAL.md §5.

## Consequências

- Robustez virou critério de regressão: o CI falha se um ataque cair abaixo do alvo.
- A suíte custa cerca de um minuto (treina o modelo completo). Fica em job separado no CI.
- O alvo é atingido pelo modelo final, mas com exemplos dos próprios ataques no treino. O
  número que mede generalização é o do controle com ataque inédito, em que 3 de 5 atingem o
  alvo. Os dois são reportados lado a lado para que um não seja lido como o outro.
