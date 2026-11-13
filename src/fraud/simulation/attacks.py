"""Simulador de ataque (Fase D): fraudes adversariais que tentam enganar o próprio modelo.

Modelo de ameaça: atacante de **caixa-cinza**. Ele não vê o modelo, mas conhece o perfil da
vítima (valores típicos, horários, aparelho) e sabe, em linhas gerais, o que detectores de
fraude olham. Por isso todos os ataques abaixo evitam os sinais "de manual": usam o
aparelho da vítima, a localização habitual e valores dentro da faixa normal.

Os ataques são definidos no espaço do problema (sequências de transações que um fraudador
consegue de fato emitir), não como perturbações de vetores de features (RESEARCH.md §7).

`stealth` em [0, 1] controla o quanto o atacante abre mão de velocidade e volume para ficar
discreto. O treino adversarial usa uma faixa de `stealth`; a avaliação inclui uma faixa mais
alta, nunca vista no treino, para medir generalização.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

import numpy as np

from fraud.domain import Channel, TxEvent
from fraud.features.state import DAY, HOUR
from fraud.simulation.world import Customer, LabeledTx, World

MINUTE = 60.0

ATTACK_LABELS: dict[str, str] = {
    "structuring": "Fracionamento de valores",
    "gradual_drift": "Mudança gradual de comportamento",
    "off_hours_burst": "Burst em horário atípico",
    "mule_account": "Conta-laranja",
    "replay": "Replay de transação legítima",
}
ATTACKS: tuple[str, ...] = tuple(ATTACK_LABELS)


def _at_usual_hour(world: World, c: Customer, day_start: float, rng: np.random.Generator) -> float:
    """Um instante dentro do dia `day_start`, no horário em que a vítima costuma transacionar."""
    hour = float(np.clip(rng.normal(c.peak_hour, 1.0), 0.0, 23.5))
    return day_start + hour * HOUR


def _transfer(
    world: World, c: Customer, recipient: str, amount: float, ts: float, rng: np.random.Generator
) -> TxEvent:
    lat, lon = world.home_location(c, rng)
    return TxEvent(
        tx_id=world.next_id(),
        customer_id=c.id,
        counterparty_id=recipient,
        amount=round(max(amount, 1.0), 2),
        channel=Channel.TRANSFER.value,
        category="p2p",
        device_id=c.devices[0],  # aparelho da própria vítima
        lat=lat,
        lon=lon,
        ts=ts,
    )


def structuring(
    world: World, victim: Customer, day_start: float, rng: np.random.Generator, stealth: float
) -> list[TxEvent]:
    """Fracionamento: esvazia a conta em muitas transferências de valor "normal".

    Nenhuma transação isolada é grande; o sinal só existe no acumulado.
    """
    total = victim.typical_amount * float(rng.uniform(15, 40))
    recipient = world.next_id("acct")
    gap = (3 + 40 * stealth) * MINUTE
    shrink = 1.0 - 0.3 * stealth
    ts = _at_usual_hour(world, victim, day_start, rng)
    events: list[TxEvent] = []
    moved = 0.0
    while moved < total and len(events) < 60:
        amount = victim.typical_amount * float(rng.uniform(0.6, 1.4)) * shrink
        events.append(_transfer(world, victim, recipient, amount, ts, rng))
        moved += amount
        ts += float(rng.exponential(gap))
    return events


def gradual_drift(
    world: World, victim: Customer, day_start: float, rng: np.random.Generator, stealth: float
) -> list[TxEvent]:
    """Sapo na panela: valores sobem um pouco a cada transação, ao longo de dias.

    Uma linha de base que se adapta rápido "aprende" o novo normal junto com o atacante.
    """
    recipient = world.next_id("acct")
    growth = 1.0 + (0.20 - 0.10 * stealth)
    amount = victim.typical_amount * float(rng.uniform(0.4, 0.8))
    ceiling = victim.typical_amount * float(rng.uniform(8, 15))
    events: list[TxEvent] = []
    day = day_start
    while amount < ceiling and len(events) < 60:
        for _ in range(int(rng.integers(1, 3))):
            ts = _at_usual_hour(world, victim, day, rng)
            events.append(_transfer(world, victim, recipient, amount, ts, rng))
            amount *= growth * float(rng.uniform(0.97, 1.03))
        day += DAY
    events.sort(key=lambda e: e.ts)
    return events


def off_hours_burst(
    world: World, victim: Customer, day_start: float, rng: np.random.Generator, stealth: float
) -> list[TxEvent]:
    """Rajada de compras online na madrugada da vítima, com valores comuns."""
    n = max(round(15 - 9 * stealth + rng.integers(-1, 2)), 4)
    gap = (1 + 6 * stealth) * MINUTE
    off_hour = (victim.peak_hour + 12 + float(rng.normal(0, 1.0))) % 24
    ts = day_start + off_hour * HOUR
    online = [m for m in world.merchants if m.online]
    events: list[TxEvent] = []
    for _ in range(n):
        m = online[int(rng.integers(0, len(online)))]
        lat, lon = world.home_location(victim, rng)
        events.append(
            TxEvent(
                tx_id=world.next_id(),
                customer_id=victim.id,
                counterparty_id=m.id,
                amount=round(victim.typical_amount * float(rng.uniform(0.5, 2.0)), 2),
                channel=Channel.ECOMMERCE.value,
                category=m.category,
                device_id=victim.devices[0],
                lat=lat,
                lon=lon,
                ts=ts,
            )
        )
        ts += float(rng.exponential(gap))
    return events


def mule_account(
    world: World, _victim: Customer, day_start: float, rng: np.random.Generator, stealth: float
) -> list[TxEvent]:
    """Conta-laranja: uma conta nova recebe de muitas vítimas diferentes.

    É o golpe em que a própria vítima faz a transferência (engenharia social). Do lado de
    quem paga, tudo é legítimo: aparelho, local e horário são os de sempre. O sinal está no
    recebedor.
    """
    mule = world.next_id("acct")
    n_victims = max(round(30 - 20 * stealth + rng.integers(-3, 4)), 5)
    window_days = max(round(1 + 3 * stealth), 1)
    shrink = 1.0 - 0.4 * stealth
    events: list[TxEvent] = []
    n_victims = min(n_victims, len(world.customers))
    for idx in rng.choice(len(world.customers), size=n_victims, replace=False):
        c = world.customers[int(idx)]
        # cada vítima paga no próprio horário habitual: é ela mesma quem faz a transferência
        ts = _at_usual_hour(world, c, day_start + float(rng.integers(0, window_days)) * DAY, rng)
        for _ in range(2 if rng.random() < 0.2 else 1):
            amount = c.typical_amount * float(rng.uniform(1.5, 5.0)) * shrink
            events.append(_transfer(world, c, mule, amount, ts, rng))
            ts += float(rng.exponential(10 * MINUTE))
    events.sort(key=lambda e: e.ts)
    return events


def replay(
    world: World,
    victim: Customer,
    day_start: float,
    rng: np.random.Generator,
    stealth: float,
    original: TxEvent | None = None,
) -> list[TxEvent]:
    """Replay: reenvia cópias exatas de uma transação legítima da vítima.

    Cada cópia é, campo a campo, uma transação que o modelo já viu e aprovou. (O replay da
    mesma requisição HTTP é barrado antes, pela Idempotency-Key; aqui o atacante gera
    requisições novas.)
    """
    if original is None:
        original = world.legit_event(victim, _at_usual_hour(world, victim, day_start, rng), rng)
    n = max(round(8 - 5 * stealth + rng.integers(-1, 2)), 2)
    gap = (0.5 + 60 * stealth) * MINUTE
    ts = original.ts + float(rng.uniform(1, 30)) * MINUTE
    events: list[TxEvent] = []
    for _ in range(n):
        events.append(replace(original, tx_id=world.next_id(), ts=ts))
        ts += float(rng.exponential(gap))
    return events


AttackFn = Callable[[World, Customer, float, np.random.Generator, float], list[TxEvent]]
_GENERATORS: dict[str, AttackFn] = {
    "structuring": structuring,
    "gradual_drift": gradual_drift,
    "off_hours_burst": off_hours_burst,
    "mule_account": mule_account,
}


def attack_stream(
    world: World,
    legit: list[LabeledTx],
    kinds: tuple[str, ...],
    episodes_per_kind: int,
    rng: np.random.Generator,
    *,
    first_day: float,
    last_day: float,
    stealth_range: tuple[float, float] = (0.0, 0.7),
    start_ts: float | None = None,
) -> list[LabeledTx]:
    """Episódios de ataque injetados entre `first_day` e `last_day` do fluxo legítimo.

    `first_day` deve deixar dias de aquecimento: sem histórico da vítima não há linha de
    base para o atacante imitar nem para o modelo comparar.
    """
    start = world.start_ts if start_ts is None else start_ts
    by_customer: dict[str, list[TxEvent]] = {}
    for item in legit:
        if not item.is_fraud and item.event.channel != Channel.TRANSFER.value:
            by_customer.setdefault(item.event.customer_id, []).append(item.event)
    horizon = start + last_day * DAY
    out: list[LabeledTx] = []
    for kind in kinds:
        for _ in range(episodes_per_kind):
            victim = world.customers[int(rng.integers(0, len(world.customers)))]
            stealth = float(rng.uniform(*stealth_range))
            day_start = (
                start
                + float(rng.integers(int(first_day), max(int(last_day) - 1, int(first_day) + 1)))
                * DAY
            )
            if kind == "replay":
                candidates = [
                    e for e in by_customer.get(victim.id, [])
                    if start + first_day * DAY <= e.ts < horizon - HOUR
                ]  # fmt: skip
                original = candidates[int(rng.integers(0, len(candidates)))] if candidates else None
                events = replay(world, victim, day_start, rng, stealth, original)
            else:
                events = _GENERATORS[kind](world, victim, day_start, rng, stealth)
            episode = world.next_id(f"atk-{kind}-")
            out.extend(LabeledTx(e, True, kind, episode) for e in events if e.ts < horizon)
    return out
