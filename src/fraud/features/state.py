"""Estado incremental por entidade (cliente e recebedor).

Cada estado é pequeno, tem tamanho limitado e é atualizado em O(1) por transação. Os
agregados usam decaimento exponencial: um contador com meia-vida `h` vale

    c(t) = c(t0) * 2^(-(t - t0) / h) + 1

Isso aproxima uma janela deslizante sem guardar a lista de eventos (ADR 004).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

HOUR = 3600.0
DAY = 86400.0

H_1H = HOUR
H_24H = DAY
H_7D = 7 * DAY
H_30D = 30 * DAY
H_90D = 90 * DAY

MAX_DEVICES = 8
MAX_COUNTERPARTIES = 48
MAX_SENDERS = 64
MAX_RECENT = 8
MAX_APPLIED = 6


def decay(value: float, dt: float, half_life: float) -> float:
    """Decai `value` por `dt` segundos. `dt` negativo (evento fora de ordem) não decai."""
    if dt <= 0.0 or value == 0.0:
        return value
    return float(value * 0.5 ** (dt / half_life))


@dataclass(slots=True)
class CustomerState:
    n: int = 0
    first_ts: float = 0.0
    last_ts: float = 0.0
    # velocidade: contagem e valor decaídos
    cnt_1h: float = 0.0
    cnt_24h: float = 0.0
    cnt_7d: float = 0.0
    amt_1h: float = 0.0
    amt_24h: float = 0.0
    amt_7d: float = 0.0
    # linha de base de valor (log1p do valor), meia-vida de 30 dias
    base_cnt: float = 0.0
    base_sum: float = 0.0
    base_sumsq: float = 0.0
    # linha de base lenta (90 dias) em log e em valor absoluto  [v2]
    slow_cnt: float = 0.0
    slow_sum: float = 0.0
    slow_sumsq: float = 0.0
    slow_amt: float = 0.0
    # geografia
    last_lat: float = 0.0
    last_lon: float = 0.0
    home_w: float = 0.0
    home_lat: float = 0.0
    home_lon: float = 0.0
    # aparelhos já usados: id -> último ts visto (limitado)
    devices: dict[str, float] = field(default_factory=dict)
    # relação com cada recebedor: id -> PairStats como lista (limitado)  [v2]
    counterparties: dict[str, list[float]] = field(default_factory=dict)
    # perfil de horário: contagem decaída (90 dias) por hora do dia  [v2]
    hour_hist: list[float] = field(default_factory=lambda: [0.0] * 24)
    # novos recebedores nas últimas 24h (decaído)  [v2]
    new_cp_24h: float = 0.0
    # últimas transações: [ts, valor, recebedor]  [v2]
    recent: list[list[Any]] = field(default_factory=list)
    # ids já aplicados, para idempotência na reentrega
    applied: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CustomerState:
        known = {k: v for k, v in data.items() if k in cls.__slots__}
        return cls(**known)


@dataclass(slots=True)
class CounterpartyState:
    """Estado do lado recebedor: é onde a conta-laranja aparece.  [v2]"""

    n: int = 0
    first_ts: float = 0.0
    last_ts: float = 0.0
    cnt_1h: float = 0.0
    cnt_24h: float = 0.0
    cnt_7d: float = 0.0
    amt_24h: float = 0.0
    amt_total: float = 0.0
    new_senders_24h: float = 0.0
    senders: dict[str, float] = field(default_factory=dict)
    applied: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CounterpartyState:
        known = {k: v for k, v in data.items() if k in cls.__slots__}
        return cls(**known)


# Índices de PairStats, o resumo da relação cliente → recebedor guardado como lista
# (lista e não dataclass para serializar em JSON sem custo).
P_LAST_TS = 0  # último pagamento
P_COUNT = 1  # nº de pagamentos
P_LAST_AMOUNT = 2
P_STREAK = 3  # aumentos consecutivos de valor
P_CNT_24H = 4  # pagamentos recentes (meia-vida 24h)
P_AMT_24H = 5  # valor recente (meia-vida 24h)
P_FIRST_AMOUNT = 6


def touch_bounded(mapping: dict[str, float], key: str, ts: float, max_size: int) -> None:
    """Registra `key` e descarta o item visto há mais tempo se passar do limite."""
    mapping[key] = ts
    if len(mapping) > max_size:
        oldest = min(mapping, key=mapping.__getitem__)
        del mapping[oldest]


def evict_oldest_pair(mapping: dict[str, list[float]], max_size: int) -> None:
    if len(mapping) > max_size:
        oldest = min(mapping, key=lambda k: mapping[k][P_LAST_TS])
        del mapping[oldest]
