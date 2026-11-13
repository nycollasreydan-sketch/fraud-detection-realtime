# Feature engineering

> Tabelas geradas a partir de `FEATURE_SPECS` em `src/fraud/features/compute.py` por
> `python -m fraud.ml.report --features docs/FEATURES.md`. Não edite à mão.

## Princípios

1. **Sem vazamento do presente.** As features de uma transação são calculadas com o
   estado *anterior* a ela; só depois o estado é atualizado.
2. **Uma implementação.** `compute_features` roda igual no replay de treino e no
   worker ([ADR 005](decisions/005-paridade-treino-servico.md)).
3. **Custo constante.** Cada estado é atualizado em O(1) com agregados de decaimento
   exponencial ([ADR 004](decisions/004-agregados-com-decaimento-exponencial.md)).
   "Meia-vida de 24h" significa que um evento perde metade do peso a cada 24 horas.
4. **Relativo ao próprio cliente.** R$ 2.000 é rotina para um cliente e anomalia para
   outro; quase toda feature de valor é uma razão ou z-score contra a linha de base
   de quem paga.
5. **Valores neutros sem histórico.** Cliente novo recebe razão 1 e z-score 0, e
   `c_tx_count` diz ao modelo quanta evidência existe por trás dos números.

A versão indica quando a feature entrou: **v1** é o conjunto base da literatura (RFM,
novidade, geografia); **v2** foi criado depois de os ataques da Fase D passarem pelo
v1 ([ADVERSARIAL.md](ADVERSARIAL.md)).

## Da própria transação

| Feature | Versão | O que mede |
|---|:---:|---|
| `amount` | v1 | Valor da transação |
| `log_amount` | v1 | log(1 + valor) |
| `hour_sin` | v1 | Hora do dia, componente seno (codificação cíclica) |
| `hour_cos` | v1 | Hora do dia, componente cosseno |
| `day_of_week` | v1 | Dia da semana (0 = segunda) |
| `channel` | v1 | Canal: pos, ecommerce ou transfer |
| `category` | v1 | Categoria do recebedor |

## Histórico do cliente (quem paga)

| Feature | Versão | O que mede |
|---|:---:|---|
| `c_tx_count` | v1 | log(1 + nº de transações já vistas do cliente) |
| `c_age_days` | v1 | Dias desde a primeira transação do cliente |
| `c_secs_since_last` | v1 | log(1 + segundos desde a transação anterior) |
| `c_cnt_1h` | v1 | Transações recentes do cliente (meia-vida 1h) |
| `c_cnt_24h` | v1 | Transações recentes do cliente (meia-vida 24h) |
| `c_cnt_7d` | v1 | Transações recentes do cliente (meia-vida 7d) |
| `c_amt_1h` | v1 | Valor movimentado recente (meia-vida 1h) |
| `c_amt_24h` | v1 | Valor movimentado recente (meia-vida 24h) |
| `c_mean_amt_7d` | v1 | Ticket médio recente do cliente (meia-vida 7d) |
| `c_amount_ratio` | v1 | Valor / ticket médio recente do cliente |
| `c_amount_z` | v1 | Z-score do log-valor contra a base de 30 dias |
| `c_new_device` | v1 | 1 se o dispositivo nunca foi usado pelo cliente |
| `c_new_counterparty` | v1 | 1 se o cliente nunca pagou este recebedor |
| `c_dist_last_km` | v1 | Distância até a transação anterior (km) |
| `c_speed_kmh` | v1 | Velocidade implícita desde a anterior (km/h) |
| `c_dist_home_km` | v1 | Distância até o centro habitual do cliente (km) |
| `c_slow_amount_z` | v2 | Z-score do log-valor contra a base lenta (90 dias) |
| `c_fast_slow_shift` | v2 | Base de 30d menos base de 90d (log): o 'normal' recente está subindo? |
| `c_cp_amount_vs_first` | v2 | Valor / primeiro valor pago a este recebedor |
| `c_cp_increase_streak` | v2 | Aumentos consecutivos de valor para este recebedor |
| `c_spend_24h_ratio` | v2 | Gasto recente (24h) + esta transação / gasto diário habitual |
| `c_cnt_24h_ratio` | v2 | Transações recentes (24h) / frequência diária habitual |
| `c_cp_tx_count` | v2 | log(1 + nº de pagamentos anteriores a este recebedor) |
| `c_cp_cnt_24h` | v2 | Pagamentos recentes a este mesmo recebedor (meia-vida 24h) |
| `c_cp_amt_24h_ratio` | v2 | Valor recente a este recebedor (24h) / ticket típico do cliente |
| `c_recent_amount_cv` | v2 | Coeficiente de variação dos últimos valores (valores 'iguais demais') |
| `c_new_cp_24h` | v2 | Recebedores inéditos pagos recentemente (meia-vida 24h) |
| `c_dup_count_24h` | v2 | Transações idênticas (mesmo recebedor e valor) nas últimas 24h |
| `c_secs_since_dup` | v2 | log(1 + segundos desde a última transação idêntica) |
| `c_hour_prob` | v2 | Fração do histórico do cliente nesta hora do dia (±1h) |

## Histórico do recebedor (quem recebe)

| Feature | Versão | O que mede |
|---|:---:|---|
| `cp_tx_count` | v2 | log(1 + nº de recebimentos do recebedor) |
| `cp_age_days` | v2 | Dias desde o primeiro recebimento do recebedor |
| `cp_cnt_1h` | v2 | Recebimentos recentes (meia-vida 1h) |
| `cp_cnt_24h` | v2 | Recebimentos recentes (meia-vida 24h) |
| `cp_new_senders_24h` | v2 | Pagadores inéditos recentes (meia-vida 24h): fan-in |
| `cp_new_sender_ratio` | v2 | Fração dos recebimentos recentes vinda de pagadores inéditos |
| `cp_amt_24h` | v2 | Valor recebido recentemente (meia-vida 24h) |
| `cp_amount_ratio` | v2 | Valor / ticket médio do recebedor |

## Gerada pelo modelo

| Feature | O que mede |
|---|---|
| `anomaly_score` | Score da Isolation Forest treinada só em transações legítimas, sobre todas as features acima. Entra como coluna extra do LightGBM. |
