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
