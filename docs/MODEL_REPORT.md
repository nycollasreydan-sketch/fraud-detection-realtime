# Relatório do modelo

> Gerado por `python -m fraud.ml.train --report docs/MODEL_REPORT.md`. Não edite à mão.

- **Versão:** `hybrid-f2-5a3e189e`
- **Conjunto de features:** v2 (46 colunas, incluindo o score de anomalia)
- **Dados:** simulador (fraud.simulation.world); 121.295 transações de treino, 40.432 de validação e 40.432 de teste, em blocos temporais consecutivos; taxa de fraude 1.30%
- **Limiares** (definidos na validação): revisão ≥ `0.0757` (orçamento de 0.3% de falso positivo); bloqueio ≥ `0.5416` (precision ≥ 95%)

## Resultado no bloco de teste (o futuro)

| Métrica | Valor |
|---|---:|
| PR-AUC (average precision) | 0.9684 |
| ROC-AUC | 0.9991 |
| Brier score | 0.00119 |
| Precision no limiar de revisão | 77.2% |
| Recall no limiar de revisão | 95.3% |
| Falso positivo no limiar de revisão | 0.28% |
| Precision no limiar de bloqueio | 95.2% |
| Recall no limiar de bloqueio | 88.6% |

## Ablação: o que cada peça acrescenta

Mesmos dados, mesmos blocos temporais. O intervalo é um bootstrap de 95% do PR-AUC.

| Modelo | PR-AUC | IC 95% | ROC-AUC | Precision (revisão) | Recall (revisão) |
|---|---:|---:|---:|---:|---:|
| 1. Regressão logística (baseline) | 0.8048 | 0.762 a 0.834 | 0.9921 | 52.0% | 80.0% |
| 2. LightGBM só com colunas da transação | 0.0692 | 0.052 a 0.090 | 0.8160 | 18.9% | 7.9% |
| 3. LightGBM + features comportamentais | 0.9731 | 0.962 a 0.984 | 0.9988 | 75.3% | 96.5% |
| 4. Híbrido: + score de anomalia (proposto) | 0.9684 | 0.957 a 0.979 | 0.9991 | 77.2% | 95.3% |

## Por cenário (limiar de revisão, bloco de teste)

| Cenário | Transações | Sinalizadas |
|---|---:|---:|
| `account_takeover` | 24 | 95.83% (recall) |
| `card_testing` | 42 | 97.62% (recall) |
| `gradual_drift` | 59 | 91.53% (recall) |
| `legit` | 40013 | 0.27% (falso positivo) |
| `legit_collector` | 14 | 50.00% (falso positivo) |
| `mule_account` | 60 | 98.33% (recall) |
| `off_hours_burst` | 48 | 93.75% (recall) |
| `replay` | 37 | 100.00% (recall) |
| `stolen_card` | 68 | 91.18% (recall) |
| `structuring` | 67 | 97.01% (recall) |

## Features mais importantes (ganho)

| Feature | Participação no ganho |
|---|---:|
| `anomaly_score` | 48.7% |
| `cp_cnt_1h` | 8.7% |
| `cp_age_days` | 8.6% |
| `amount` | 5.9% |
| `channel` | 4.1% |
| `c_hour_prob` | 2.5% |
| `c_dist_home_km` | 1.7% |
| `c_cp_cnt_24h` | 1.6% |
| `c_secs_since_last` | 1.5% |
| `cp_tx_count` | 1.4% |
| `cp_cnt_24h` | 1.4% |
| `c_secs_since_dup` | 1.1% |
| `hour_sin` | 1.1% |
| `c_dup_count_24h` | 1.1% |
| `c_new_device` | 1.0% |

## Robustez adversarial (bateria padrão)

Outra população e outras sementes; análise completa em [ADVERSARIAL.md](ADVERSARIAL.md).
Falso positivo em legítimas nesta população: 0.41%.

| Ataque | Episódios | Detecção por episódio | Valor evitado | Recall por transação |
|---|---:|---:|---:|---:|
| `gradual_drift` | 60 | 100.0% | 94.6% | 92.4% |
| `mule_account` | 60 | 100.0% | 96.4% | 97.1% |
| `off_hours_burst` | 60 | 100.0% | 91.3% | 91.5% |
| `replay` | 60 | 100.0% | 100.0% | 99.7% |
| `structuring` | 60 | 100.0% | 96.7% | 97.0% |

## Latência de inferência

Uma transação, uma thread, incluindo score de anomalia, boosting e explicação TreeSHAP.

- p50: 5.79 ms
- p95: 11.45 ms
- p99: 13.63 ms
