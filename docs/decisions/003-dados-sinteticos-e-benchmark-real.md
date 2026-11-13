# 003 — Simulador próprio para treinar; dataset real só como benchmark

**Status:** aceita

## Contexto

O repositório veio com `data/creditcard.csv` (ULB/Kaggle): 284.807 transações reais, 492
fraudes. É o dataset público padrão, mas as colunas são componentes PCA anônimas
(`V1..V28`), mais `Time` e `Amount`. **Não há identificador de cliente, de recebedor, de
dispositivo nem localização.**

O sistema pedido calcula features de histórico do cliente em tempo real, e a Fase D exige
ataques comportamentais (conta-laranja, mudança gradual, replay). Nada disso é possível
sem saber quem pagou a quem.

## Decisão

1. **Treino e demonstração** usam um simulador próprio (`fraud.simulation.world`), com
   clientes, lojistas, contas, dispositivos, cidades, perfis de horário e três cenários de
   fraude "de manual". O tráfego legítimo inclui de propósito comportamentos que parecem
   fraude: viagens, troca de aparelho, compras grandes ocasionais, pagamentos parcelados a
   contas novas e vaquinhas que recebem de muita gente.
2. **O dataset real** é usado em `make benchmark` para a única pergunta que ele consegue
   responder: a parte do modelo que não depende do simulador (boosting calibrado, com e
   sem o score de anomalia) se sustenta em dados reais? Resultado em
   [BENCHMARK_KAGGLE.md](../BENCHMARK_KAGGLE.md).

## Alternativas descartadas

- **Treinar no dataset da ULB e inventar um `customer_id`.** Atribuir clientes ao acaso
  criaria históricos sem relação com as transações; as features de comportamento seriam
  ruído e a demonstração, uma encenação.
- **IEEE-CIS ou PaySim.** O IEEE-CIS tem identidade só parcialmente recuperável e exige
  login e download manual; o PaySim é sintético e não tem dispositivo nem localização.
  Nenhum dos dois permite gerar os ataques adaptativos da Fase D.

## Consequências

- **Números absolutos não se transferem.** Um PR-AUC medido no simulador diz respeito ao
  simulador. O que se transfere é o método (split temporal, paridade de features,
  limiares por orçamento) e as comparações relativas.
- **O modelo pode aprender atalhos do simulador.** Isso aconteceu quatro vezes durante a
  Fase D e está documentado em [ADVERSARIAL.md](../ADVERSARIAL.md). É o principal risco de
  dados sintéticos, e a forma de reduzi-lo foi desconfiar de todo resultado bom demais e
  ler as explicações do modelo.
- O dataset real fica fora do git (`data/` está no `.gitignore`); `make benchmark` avisa
  onde baixá-lo.
