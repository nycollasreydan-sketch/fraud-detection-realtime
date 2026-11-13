# Benchmark em dados reais (ULB / Kaggle)

> Gerado por `make benchmark`. Não edite à mão.

- 170.882 transações de treino, 56.963 de validação e 56.962 de teste, em blocos temporais consecutivos (nunca split aleatório)
- Taxa de fraude: 0.173%; apenas 75 fraudes no bloco de teste, por isso os intervalos são largos
- Mesmo código de treino do projeto (`train_model`): peso de classe moderado, calibração de Platt e limiar pelo orçamento de 0,3% de falso positivo

| Modelo | PR-AUC | IC 95% (bootstrap) | PR-AUC por semente | ROC-AUC | Precision | Recall |
|---|---:|---:|---|---:|---:|---:|
| Regressão logística (baseline) | 0.7524 | 0.644 a 0.851 | n/d | 0.9835 | 27.1% | 85.3% |
| LightGBM calibrado | 0.7412 | 0.627 a 0.838 | 0.741, 0.659, 0.783 | 0.9726 | 33.3% | 77.3% |
| Híbrido: + score de anomalia | 0.7658 | 0.677 a 0.849 | 0.766, 0.755, 0.779 | 0.9625 | 28.7% | 80.0% |

Precision e recall no limiar de revisão. Leitura dos números em [ADR 006](decisions/006-modelo-hibrido.md).
