"""Cálculo de features: a ÚNICA implementação, usada no treino e no serviço (ADR 005).

Contrato:

1. `compute_features(tx, cliente, recebedor)` lê os estados **como estavam antes** da
   transação e devolve o vetor. A transação nunca entra no próprio histórico.
2. `apply_transaction(tx, cliente, recebedor)` atualiza os estados depois.

As duas funções são puras em relação a I/O: não sabem se o estado veio do Redis ou de um
dicionário em memória durante o replay offline.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from fraud.domain import CATEGORY_CODES, CHANNEL_CODES, OTHER_CATEGORY_CODE, TxEvent
from fraud.features.state import (
    DAY,
    H_1H,
    H_7D,
    H_24H,
    H_30D,
    H_90D,
    HOUR,
    MAX_APPLIED,
    MAX_COUNTERPARTIES,
    MAX_DEVICES,
    MAX_RECENT,
    MAX_SENDERS,
    P_AMT_24H,
    P_CNT_24H,
    P_COUNT,
    P_FIRST_AMOUNT,
    P_LAST_AMOUNT,
    P_LAST_TS,
    P_STREAK,
    CounterpartyState,
    CustomerState,
    decay,
    evict_oldest_pair,
    touch_bounded,
)

EARTH_RADIUS_KM = 6371.0
MIN_STD_LOG_AMOUNT = 0.25
MAX_SPEED_KMH = 5000.0
LN2 = math.log(2.0)
# valor de regime de um agregado com meia-vida de 24h, em "dias de atividade"
STEADY_24H_DAYS = H_24H / LN2 / DAY
NEUTRAL_HOUR_PROB = 3.0 / 24.0
STREAK_MIN_GROWTH = 1.02


@dataclass(frozen=True, slots=True)
class FeatureSpec:
    name: str
    group: str  # transacao | cliente | recebedor | modelo
    version: int  # conjunto de features em que entrou (1 = base, 2 = pós Fase D)
    description: str
    categorical: bool = False


FEATURE_SPECS: tuple[FeatureSpec, ...] = (
    # ---- transação -----------------------------------------------------------------
    FeatureSpec("amount", "transacao", 1, "Valor da transação"),
    FeatureSpec("log_amount", "transacao", 1, "log(1 + valor)"),
    FeatureSpec("hour_sin", "transacao", 1, "Hora do dia, componente seno (codificação cíclica)"),
    FeatureSpec("hour_cos", "transacao", 1, "Hora do dia, componente cosseno"),
    FeatureSpec("day_of_week", "transacao", 1, "Dia da semana (0 = segunda)"),
    FeatureSpec("channel", "transacao", 1, "Canal: pos, ecommerce ou transfer", True),
    FeatureSpec("category", "transacao", 1, "Categoria do recebedor", True),
    # ---- cliente: maturidade e recência ---------------------------------------------
    FeatureSpec("c_tx_count", "cliente", 1, "log(1 + nº de transações já vistas do cliente)"),
    FeatureSpec("c_age_days", "cliente", 1, "Dias desde a primeira transação do cliente"),
    FeatureSpec("c_secs_since_last", "cliente", 1, "log(1 + segundos desde a transação anterior)"),
    # ---- cliente: velocidade ---------------------------------------------------------
    FeatureSpec("c_cnt_1h", "cliente", 1, "Transações recentes do cliente (meia-vida 1h)"),
    FeatureSpec("c_cnt_24h", "cliente", 1, "Transações recentes do cliente (meia-vida 24h)"),
    FeatureSpec("c_cnt_7d", "cliente", 1, "Transações recentes do cliente (meia-vida 7d)"),
    FeatureSpec("c_amt_1h", "cliente", 1, "Valor movimentado recente (meia-vida 1h)"),
    FeatureSpec("c_amt_24h", "cliente", 1, "Valor movimentado recente (meia-vida 24h)"),
    # ---- cliente: desvio de valor ----------------------------------------------------
    FeatureSpec("c_mean_amt_7d", "cliente", 1, "Ticket médio recente do cliente (meia-vida 7d)"),
    FeatureSpec("c_amount_ratio", "cliente", 1, "Valor / ticket médio recente do cliente"),
    FeatureSpec("c_amount_z", "cliente", 1, "Z-score do log-valor contra a base de 30 dias"),
    # ---- cliente: novidade e geografia ----------------------------------------------
    FeatureSpec("c_new_device", "cliente", 1, "1 se o dispositivo nunca foi usado pelo cliente"),
    FeatureSpec("c_new_counterparty", "cliente", 1, "1 se o cliente nunca pagou este recebedor"),
    FeatureSpec("c_dist_last_km", "cliente", 1, "Distância até a transação anterior (km)"),
    FeatureSpec("c_speed_kmh", "cliente", 1, "Velocidade implícita desde a anterior (km/h)"),
    FeatureSpec("c_dist_home_km", "cliente", 1, "Distância até o centro habitual do cliente (km)"),
    # ==== v2: criadas depois da rodada adversarial (docs/ADVERSARIAL.md) ==============
    # ---- contra mudança gradual: linha de base lenta ---------------------------------
    FeatureSpec(
        "c_slow_amount_z", "cliente", 2, "Z-score do log-valor contra a base lenta (90 dias)"
    ),
    FeatureSpec(
        "c_fast_slow_shift",
        "cliente",
        2,
        "Base de 30d menos base de 90d (log): o 'normal' recente está subindo?",
    ),
    FeatureSpec(
        "c_cp_amount_vs_first", "cliente", 2, "Valor / primeiro valor pago a este recebedor"
    ),
    FeatureSpec(
        "c_cp_increase_streak", "cliente", 2, "Aumentos consecutivos de valor para este recebedor"
    ),
    # ---- contra fracionamento: acumulado relativo ao normal do cliente ---------------
    FeatureSpec(
        "c_spend_24h_ratio",
        "cliente",
        2,
        "Gasto recente (24h) + esta transação / gasto diário habitual",
    ),
    FeatureSpec(
        "c_cnt_24h_ratio", "cliente", 2, "Transações recentes (24h) / frequência diária habitual"
    ),
    FeatureSpec(
        "c_cp_tx_count", "cliente", 2, "log(1 + nº de pagamentos anteriores a este recebedor)"
    ),
    FeatureSpec(
        "c_cp_cnt_24h", "cliente", 2, "Pagamentos recentes a este mesmo recebedor (meia-vida 24h)"
    ),
    FeatureSpec(
        "c_cp_amt_24h_ratio",
        "cliente",
        2,
        "Valor recente a este recebedor (24h) / ticket típico do cliente",
    ),
    FeatureSpec(
        "c_recent_amount_cv",
        "cliente",
        2,
        "Coeficiente de variação dos últimos valores (valores 'iguais demais')",
    ),
    FeatureSpec(
        "c_new_cp_24h", "cliente", 2, "Recebedores inéditos pagos recentemente (meia-vida 24h)"
    ),
    # ---- contra replay: duplicatas exatas --------------------------------------------
    FeatureSpec(
        "c_dup_count_24h",
        "cliente",
        2,
        "Transações idênticas (mesmo recebedor e valor) nas últimas 24h",
    ),
    FeatureSpec(
        "c_secs_since_dup", "cliente", 2, "log(1 + segundos desde a última transação idêntica)"
    ),
    # ---- contra horário atípico: perfil do próprio cliente ---------------------------
    FeatureSpec(
        "c_hour_prob", "cliente", 2, "Fração do histórico do cliente nesta hora do dia (±1h)"
    ),
    # ---- contra conta-laranja: o lado de quem recebe ----------------------------------
    FeatureSpec("cp_tx_count", "recebedor", 2, "log(1 + nº de recebimentos do recebedor)"),
    FeatureSpec("cp_age_days", "recebedor", 2, "Dias desde o primeiro recebimento do recebedor"),
    FeatureSpec("cp_cnt_1h", "recebedor", 2, "Recebimentos recentes (meia-vida 1h)"),
    FeatureSpec("cp_cnt_24h", "recebedor", 2, "Recebimentos recentes (meia-vida 24h)"),
    FeatureSpec(
        "cp_new_senders_24h", "recebedor", 2, "Pagadores inéditos recentes (meia-vida 24h): fan-in"
    ),
    FeatureSpec(
        "cp_new_sender_ratio",
        "recebedor",
        2,
        "Fração dos recebimentos recentes vinda de pagadores inéditos",
    ),
    FeatureSpec("cp_amt_24h", "recebedor", 2, "Valor recebido recentemente (meia-vida 24h)"),
    FeatureSpec("cp_amount_ratio", "recebedor", 2, "Valor / ticket médio do recebedor"),
)


def feature_names(version: int) -> list[str]:
    """Features do conjunto `version` (cumulativo), sem as geradas pelo modelo."""
    return [s.name for s in FEATURE_SPECS if s.version <= version and s.group != "modelo"]


def categorical_features(version: int) -> list[str]:
    return [s.name for s in FEATURE_SPECS if s.version <= version and s.categorical]


ALL_FEATURES: list[str] = [s.name for s in FEATURE_SPECS if s.group != "modelo"]
LATEST_VERSION: int = max(s.version for s in FEATURE_SPECS)
FEATURE_DESCRIPTIONS: dict[str, str] = {s.name: s.description for s in FEATURE_SPECS}


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def hour_of_day(ts: float) -> float:
    return (ts % DAY) / HOUR


def day_of_week(ts: float) -> int:
    # 1970-01-01 foi uma quinta-feira (3 com segunda = 0)
    return int((ts // DAY + 3) % 7)


def compute_features(tx: TxEvent, cs: CustomerState, cps: CounterpartyState) -> dict[str, float]:
    """Vetor de features da transação, com os estados ainda sem ela."""
    f: dict[str, float] = {}
    amount = tx.amount
    log_amount = math.log1p(amount)
    hour = hour_of_day(tx.ts)
    angle = 2 * math.pi * hour / 24.0

    f["amount"] = amount
    f["log_amount"] = log_amount
    f["hour_sin"] = math.sin(angle)
    f["hour_cos"] = math.cos(angle)
    f["day_of_week"] = float(day_of_week(tx.ts))
    f["channel"] = float(CHANNEL_CODES.get(tx.channel, 0))
    f["category"] = float(CATEGORY_CODES.get(tx.category, OTHER_CATEGORY_CODE))

    has_history = cs.n > 0
    dt = tx.ts - cs.last_ts if has_history else 0.0
    dt = max(dt, 0.0)

    f["c_tx_count"] = math.log1p(cs.n)
    f["c_age_days"] = max(tx.ts - cs.first_ts, 0.0) / DAY if has_history else 0.0
    # sem histórico: usa 30 dias como "muito tempo", valor neutro para a velocidade
    f["c_secs_since_last"] = math.log1p(dt) if has_history else math.log1p(30 * DAY)

    cnt_1h = decay(cs.cnt_1h, dt, H_1H)
    cnt_24h = decay(cs.cnt_24h, dt, H_24H)
    cnt_7d = decay(cs.cnt_7d, dt, H_7D)
    amt_1h = decay(cs.amt_1h, dt, H_1H)
    amt_24h = decay(cs.amt_24h, dt, H_24H)
    amt_7d = decay(cs.amt_7d, dt, H_7D)
    f["c_cnt_1h"] = cnt_1h
    f["c_cnt_24h"] = cnt_24h
    f["c_cnt_7d"] = cnt_7d
    f["c_amt_1h"] = amt_1h
    f["c_amt_24h"] = amt_24h

    if cnt_7d > 0.05:
        mean_7d = amt_7d / cnt_7d
        f["c_mean_amt_7d"] = mean_7d
        f["c_amount_ratio"] = amount / max(mean_7d, 1.0)
    else:
        f["c_mean_amt_7d"] = amount
        f["c_amount_ratio"] = 1.0

    f["c_amount_z"] = _zscore(log_amount, cs.base_cnt, cs.base_sum, cs.base_sumsq)

    f["c_new_device"] = 1.0 if has_history and tx.device_id not in cs.devices else 0.0
    f["c_new_counterparty"] = (
        1.0 if has_history and tx.counterparty_id not in cs.counterparties else 0.0
    )

    if has_history:
        dist_last = haversine_km(cs.last_lat, cs.last_lon, tx.lat, tx.lon)
        f["c_dist_last_km"] = dist_last
        f["c_speed_kmh"] = min(dist_last / max(dt / HOUR, 1 / 60), MAX_SPEED_KMH)
        f["c_dist_home_km"] = (
            haversine_km(cs.home_lat, cs.home_lon, tx.lat, tx.lon) if cs.home_w > 0 else 0.0
        )
    else:
        f["c_dist_last_km"] = 0.0
        f["c_speed_kmh"] = 0.0
        f["c_dist_home_km"] = 0.0

    _customer_v2(f, tx, cs, dt, log_amount, amt_24h, cnt_24h)
    _counterparty_v2(f, tx, cps)
    return f


def _customer_v2(
    f: dict[str, float],
    tx: TxEvent,
    cs: CustomerState,
    dt: float,
    log_amount: float,
    amt_24h: float,
    cnt_24h: float,
) -> None:
    amount = tx.amount
    has_slow = cs.slow_cnt >= 3.0
    typical = math.expm1(cs.slow_sum / cs.slow_cnt) if has_slow else amount
    typical = max(typical, 1.0)

    # --- linha de base lenta: demora a "aprender" um novo normal imposto aos poucos
    f["c_slow_amount_z"] = _zscore(log_amount, cs.slow_cnt, cs.slow_sum, cs.slow_sumsq)
    f["c_fast_slow_shift"] = (
        cs.base_sum / cs.base_cnt - cs.slow_sum / cs.slow_cnt
        if has_slow and cs.base_cnt >= 3.0
        else 0.0
    )

    # --- acumulado contra o habitual do próprio cliente
    if has_slow:
        age = max(tx.ts - cs.first_ts, 0.0)
        # janela efetiva da média de 90d, corrigida para históricos curtos
        effective_days = max((1.0 - 0.5 ** (age / H_90D)) * H_90D / LN2 / DAY, 1.0)
        daily_spend = decay(cs.slow_amt, dt, H_90D) / effective_days
        daily_count = decay(cs.slow_cnt, dt, H_90D) / effective_days
        f["c_spend_24h_ratio"] = (amt_24h + amount) / max(daily_spend * STEADY_24H_DAYS, typical)
        f["c_cnt_24h_ratio"] = (cnt_24h + 1.0) / max(daily_count * STEADY_24H_DAYS, 1.0)
    else:
        f["c_spend_24h_ratio"] = 1.0
        f["c_cnt_24h_ratio"] = 1.0

    # --- relação com este recebedor
    pair = cs.counterparties.get(tx.counterparty_id)
    if pair is not None:
        dt_pair = max(tx.ts - pair[P_LAST_TS], 0.0)
        f["c_cp_tx_count"] = math.log1p(pair[P_COUNT])
        f["c_cp_cnt_24h"] = decay(pair[P_CNT_24H], dt_pair, H_24H)
        f["c_cp_amt_24h_ratio"] = (decay(pair[P_AMT_24H], dt_pair, H_24H) + amount) / typical
        f["c_cp_amount_vs_first"] = amount / max(pair[P_FIRST_AMOUNT], 1.0)
        f["c_cp_increase_streak"] = (
            pair[P_STREAK] + 1.0 if amount > pair[P_LAST_AMOUNT] * STREAK_MIN_GROWTH else 0.0
        )
    else:
        f["c_cp_tx_count"] = 0.0
        f["c_cp_cnt_24h"] = 0.0
        f["c_cp_amt_24h_ratio"] = amount / typical
        f["c_cp_amount_vs_first"] = 1.0
        f["c_cp_increase_streak"] = 0.0
    f["c_new_cp_24h"] = decay(cs.new_cp_24h, dt, H_24H)

    # --- últimas transações: duplicatas exatas e valores uniformes demais
    dups = 0
    since_dup = 30 * DAY
    amounts = [amount]
    for ts, past_amount, past_cp in cs.recent:
        amounts.append(past_amount)
        age_s = tx.ts - ts
        if past_cp == tx.counterparty_id and abs(past_amount - amount) < 0.005 and age_s <= DAY:
            dups += 1
            since_dup = min(since_dup, max(age_s, 0.0))
    f["c_dup_count_24h"] = float(dups)
    f["c_secs_since_dup"] = math.log1p(since_dup)
    if len(amounts) >= 4:
        mean = sum(amounts) / len(amounts)
        var = sum((a - mean) ** 2 for a in amounts) / len(amounts)
        f["c_recent_amount_cv"] = math.sqrt(var) / max(mean, 1e-9)
    else:
        f["c_recent_amount_cv"] = 1.0

    # --- perfil de horário do próprio cliente
    total = sum(cs.hour_hist)
    if total >= 5.0:
        h = int(hour_of_day(tx.ts)) % 24
        near = cs.hour_hist[h - 1] + cs.hour_hist[h] + cs.hour_hist[(h + 1) % 24]
        f["c_hour_prob"] = near / total
    else:
        f["c_hour_prob"] = NEUTRAL_HOUR_PROB


def _counterparty_v2(f: dict[str, float], tx: TxEvent, cps: CounterpartyState) -> None:
    if cps.n == 0:
        f["cp_tx_count"] = 0.0
        f["cp_age_days"] = 0.0
        f["cp_cnt_1h"] = 0.0
        f["cp_cnt_24h"] = 0.0
        f["cp_new_senders_24h"] = 0.0
        f["cp_new_sender_ratio"] = 0.0
        f["cp_amt_24h"] = 0.0
        f["cp_amount_ratio"] = 1.0
        return
    dt = max(tx.ts - cps.last_ts, 0.0)
    cnt_24h = decay(cps.cnt_24h, dt, H_24H)
    new_senders = decay(cps.new_senders_24h, dt, H_24H)
    f["cp_tx_count"] = math.log1p(cps.n)
    f["cp_age_days"] = max(tx.ts - cps.first_ts, 0.0) / DAY
    f["cp_cnt_1h"] = decay(cps.cnt_1h, dt, H_1H)
    f["cp_cnt_24h"] = cnt_24h
    f["cp_new_senders_24h"] = new_senders
    f["cp_new_sender_ratio"] = new_senders / cnt_24h if cnt_24h > 0.05 else 0.0
    f["cp_amt_24h"] = decay(cps.amt_24h, dt, H_24H)
    f["cp_amount_ratio"] = tx.amount / max(cps.amt_total / cps.n, 1.0)


def _zscore(x: float, cnt: float, total: float, total_sq: float) -> float:
    """Z-score de `x` contra uma média/variância decaídas. Neutro (0) com pouco histórico."""
    if cnt < 3.0:
        return 0.0
    mean = total / cnt
    var = max(total_sq / cnt - mean * mean, 0.0)
    std = max(math.sqrt(var), MIN_STD_LOG_AMOUNT)
    return max(min((x - mean) / std, 20.0), -20.0)


def _remember(applied: list[str], tx_id: str) -> None:
    applied.append(tx_id)
    if len(applied) > MAX_APPLIED:
        del applied[0]


def update_customer(cs: CustomerState, tx: TxEvent) -> bool:
    """Incorpora `tx` ao estado do cliente. Devolve False se já estava aplicada."""
    if tx.tx_id in cs.applied:
        return False
    amount = tx.amount
    log_amount = math.log1p(amount)
    if cs.n == 0:
        cs.first_ts = tx.ts
        dt = 0.0
    else:
        dt = max(tx.ts - cs.last_ts, 0.0)

    cs.cnt_1h = decay(cs.cnt_1h, dt, H_1H) + 1.0
    cs.cnt_24h = decay(cs.cnt_24h, dt, H_24H) + 1.0
    cs.cnt_7d = decay(cs.cnt_7d, dt, H_7D) + 1.0
    cs.amt_1h = decay(cs.amt_1h, dt, H_1H) + amount
    cs.amt_24h = decay(cs.amt_24h, dt, H_24H) + amount
    cs.amt_7d = decay(cs.amt_7d, dt, H_7D) + amount

    cs.base_cnt = decay(cs.base_cnt, dt, H_30D) + 1.0
    cs.base_sum = decay(cs.base_sum, dt, H_30D) + log_amount
    cs.base_sumsq = decay(cs.base_sumsq, dt, H_30D) + log_amount * log_amount

    cs.slow_cnt = decay(cs.slow_cnt, dt, H_90D) + 1.0
    cs.slow_sum = decay(cs.slow_sum, dt, H_90D) + log_amount
    cs.slow_sumsq = decay(cs.slow_sumsq, dt, H_90D) + log_amount * log_amount
    cs.slow_amt = decay(cs.slow_amt, dt, H_90D) + amount

    # centro habitual: média decaída das coordenadas
    w = decay(cs.home_w, dt, H_30D)
    cs.home_lat = (cs.home_lat * w + tx.lat) / (w + 1.0)
    cs.home_lon = (cs.home_lon * w + tx.lon) / (w + 1.0)
    cs.home_w = w + 1.0

    if dt > 0.0:
        factor = 0.5 ** (dt / H_90D)
        cs.hour_hist = [h * factor for h in cs.hour_hist]
    cs.hour_hist[int(hour_of_day(tx.ts)) % 24] += 1.0

    pair = cs.counterparties.get(tx.counterparty_id)
    cs.new_cp_24h = decay(cs.new_cp_24h, dt, H_24H) + (1.0 if pair is None and cs.n > 0 else 0.0)
    if pair is None:
        cs.counterparties[tx.counterparty_id] = [tx.ts, 1.0, amount, 0.0, 1.0, amount, amount]
        evict_oldest_pair(cs.counterparties, MAX_COUNTERPARTIES)
    else:
        dt_pair = max(tx.ts - pair[P_LAST_TS], 0.0)
        grew = amount > pair[P_LAST_AMOUNT] * STREAK_MIN_GROWTH
        pair[P_STREAK] = pair[P_STREAK] + 1.0 if grew else 0.0
        pair[P_CNT_24H] = decay(pair[P_CNT_24H], dt_pair, H_24H) + 1.0
        pair[P_AMT_24H] = decay(pair[P_AMT_24H], dt_pair, H_24H) + amount
        pair[P_COUNT] += 1.0
        pair[P_LAST_AMOUNT] = amount
        pair[P_LAST_TS] = max(pair[P_LAST_TS], tx.ts)

    touch_bounded(cs.devices, tx.device_id, tx.ts, MAX_DEVICES)

    cs.recent.append([tx.ts, amount, tx.counterparty_id])
    if len(cs.recent) > MAX_RECENT:
        del cs.recent[0]

    cs.last_lat = tx.lat
    cs.last_lon = tx.lon
    cs.last_ts = max(cs.last_ts, tx.ts)
    cs.n += 1
    _remember(cs.applied, tx.tx_id)
    return True


def update_counterparty(cps: CounterpartyState, tx: TxEvent) -> bool:
    """Incorpora `tx` ao estado do recebedor. Devolve False se já estava aplicada."""
    if tx.tx_id in cps.applied:
        return False
    if cps.n == 0:
        cps.first_ts = tx.ts
        dt = 0.0
    else:
        dt = max(tx.ts - cps.last_ts, 0.0)
    cps.cnt_1h = decay(cps.cnt_1h, dt, H_1H) + 1.0
    cps.cnt_24h = decay(cps.cnt_24h, dt, H_24H) + 1.0
    cps.cnt_7d = decay(cps.cnt_7d, dt, H_7D) + 1.0
    cps.amt_24h = decay(cps.amt_24h, dt, H_24H) + tx.amount
    cps.amt_total += tx.amount
    is_new_sender = tx.customer_id not in cps.senders
    cps.new_senders_24h = decay(cps.new_senders_24h, dt, H_24H) + (1.0 if is_new_sender else 0.0)
    touch_bounded(cps.senders, tx.customer_id, tx.ts, MAX_SENDERS)
    cps.last_ts = max(cps.last_ts, tx.ts)
    cps.n += 1
    _remember(cps.applied, tx.tx_id)
    return True


def apply_transaction(tx: TxEvent, cs: CustomerState, cps: CounterpartyState) -> None:
    update_customer(cs, tx)
    update_counterparty(cps, tx)
