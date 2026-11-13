"""Mundo sintético: clientes, recebedores, tráfego legítimo e fraudes "conhecidas".

Por que um simulador próprio (ADR 003): nenhum dataset público traz identificador de
cliente, de recebedor, dispositivo e localização ao mesmo tempo, e sem isso não há como
exercitar features de histórico nem ataques comportamentais.

O tráfego legítimo tem, de propósito, comportamentos que parecem fraude (viagens, troca de
aparelho, compras grandes ocasionais, vaquinhas que recebem de muita gente). Sem esses
negativos difíceis qualquer modelo teria precision perto de 1 e a avaliação não diria nada.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

import numpy as np

from fraud.domain import Channel, TxEvent
from fraud.features.state import DAY

# 2026-01-05 00:00:00 UTC, uma segunda-feira
DEFAULT_START_TS = 1767571200.0

CITIES: tuple[tuple[str, float, float, float], ...] = (
    ("sao_paulo", -23.55, -46.63, 0.30),
    ("rio_de_janeiro", -22.91, -43.17, 0.17),
    ("belo_horizonte", -19.92, -43.94, 0.10),
    ("brasilia", -15.79, -47.88, 0.08),
    ("salvador", -12.97, -38.50, 0.08),
    ("fortaleza", -3.73, -38.52, 0.07),
    ("curitiba", -25.43, -49.27, 0.07),
    ("recife", -8.05, -34.88, 0.05),
    ("porto_alegre", -30.03, -51.23, 0.05),
    ("manaus", -3.12, -60.02, 0.03),
)

# (categoria, peso, multiplicador de valor, fração online)
MERCHANT_CATEGORIES: tuple[tuple[str, float, float, float], ...] = (
    ("grocery", 0.22, 1.0, 0.05),
    ("restaurant", 0.18, 0.6, 0.15),
    ("transport", 0.10, 0.3, 0.60),
    ("fuel", 0.08, 1.2, 0.0),
    ("pharmacy", 0.08, 0.5, 0.10),
    ("fashion", 0.08, 1.8, 0.50),
    ("electronics", 0.05, 6.0, 0.70),
    ("travel", 0.03, 8.0, 0.90),
    ("services", 0.10, 1.5, 0.50),
    ("entertainment", 0.08, 0.5, 0.80),
)
CATEGORY_MULTIPLIER = {c: m for c, _, m, _ in MERCHANT_CATEGORIES}


@dataclass(slots=True)
class Merchant:
    id: str
    category: str
    city: int
    lat: float
    lon: float
    online: bool


@dataclass(slots=True)
class Customer:
    id: str
    city: int
    home_lat: float
    home_lon: float
    mu: float  # média do log-valor
    sigma: float
    rate: float  # transações por dia
    peak_hour: float
    hour_sd: float
    devices: list[str]
    card: str
    fav_physical: list[int]
    fav_online: list[int]
    friends: list[str]
    channel_probs: tuple[float, float, float]  # pos, ecommerce, transfer

    @property
    def typical_amount(self) -> float:
        return float(np.exp(self.mu))


@dataclass(slots=True)
class LabeledTx:
    event: TxEvent
    is_fraud: bool = False
    scenario: str = "legit"
    episode: str = ""


@dataclass
class World:
    """População fixa a partir de uma semente. Gera fluxos de transações rotuladas."""

    n_customers: int = 2000
    n_merchants: int = 400
    seed: int = 7
    start_ts: float = DEFAULT_START_TS
    customers: list[Customer] = field(default_factory=list, init=False)
    merchants: list[Merchant] = field(default_factory=list, init=False)
    _seq: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        rng = np.random.default_rng(self.seed)
        city_w = np.array([c[3] for c in CITIES])
        city_w = city_w / city_w.sum()
        cat_w = np.array([c[1] for c in MERCHANT_CATEGORIES])
        cat_w = cat_w / cat_w.sum()

        for i in range(self.n_merchants):
            city = int(rng.choice(len(CITIES), p=city_w))
            cat_idx = int(rng.choice(len(MERCHANT_CATEGORIES), p=cat_w))
            cat, _, _, online_frac = MERCHANT_CATEGORIES[cat_idx]
            self.merchants.append(
                Merchant(
                    id=f"m{i:05d}",
                    category=cat,
                    city=city,
                    lat=CITIES[city][1] + float(rng.normal(0, 0.08)),
                    lon=CITIES[city][2] + float(rng.normal(0, 0.08)),
                    online=bool(rng.random() < online_frac),
                )
            )
        self._physical_by_city: dict[int, list[int]] = {}
        self._online: list[int] = []
        for idx, m in enumerate(self.merchants):
            if m.online:
                self._online.append(idx)
            else:
                self._physical_by_city.setdefault(m.city, []).append(idx)

        ids = [f"c{i:06d}" for i in range(self.n_customers)]
        for i, cid in enumerate(ids):
            city = int(rng.choice(len(CITIES), p=city_w))
            physical = self._physical_by_city.get(city) or list(range(len(self.merchants)))
            n_phys = min(len(physical), int(rng.integers(5, 13)))
            n_onl = min(len(self._online), int(rng.integers(2, 8)))
            n_friends = int(rng.integers(1, 6))
            friends = [ids[int(j)] for j in rng.integers(0, self.n_customers, n_friends)]
            p_pos = float(rng.uniform(0.3, 0.6))
            p_ecom = float(rng.uniform(0.15, 0.4))
            p_transfer = max(1.0 - p_pos - p_ecom, 0.05)
            total = p_pos + p_ecom + p_transfer
            self.customers.append(
                Customer(
                    id=cid,
                    city=city,
                    home_lat=CITIES[city][1] + float(rng.normal(0, 0.06)),
                    home_lon=CITIES[city][2] + float(rng.normal(0, 0.06)),
                    mu=float(rng.normal(4.0, 0.6)),
                    sigma=float(rng.uniform(0.45, 0.9)),
                    rate=float(np.clip(rng.gamma(2.5, 0.9), 0.3, 10.0)),
                    peak_hour=float(np.clip(rng.normal(17.0, 3.0), 9.0, 23.0)),
                    hour_sd=float(rng.uniform(2.0, 4.5)),
                    devices=[f"d-{cid}-{k}" for k in range(int(rng.integers(1, 3)))],
                    card=f"card-{cid}",
                    fav_physical=[int(x) for x in rng.choice(physical, n_phys, replace=False)],
                    fav_online=[int(x) for x in rng.choice(self._online, n_onl, replace=False)],
                    friends=[f for f in friends if f != cid] or [ids[(i + 1) % len(ids)]],
                    channel_probs=(p_pos / total, p_ecom / total, p_transfer / total),
                )
            )

    # ------------------------------------------------------------------ utilidades
    def next_id(self, prefix: str = "t") -> str:
        self._seq += 1
        return f"{prefix}{self._seq:09d}"

    def sample_hour(self, c: Customer, rng: np.random.Generator) -> float:
        """Hora do dia (UTC) seguindo o perfil do cliente, com 8% de ruído uniforme."""
        if rng.random() < 0.08:
            return float(rng.uniform(0, 24))
        return float(rng.normal(c.peak_hour, c.hour_sd) % 24)

    def sample_amount(self, c: Customer, rng: np.random.Generator, category: str = "p2p") -> float:
        mult = CATEGORY_MULTIPLIER.get(category, 1.0) ** 0.5
        amount = float(np.exp(rng.normal(c.mu, c.sigma))) * mult
        if rng.random() < 0.02:  # compra grande ocasional, legítima
            amount *= float(rng.uniform(4, 12))
        return float(round(max(amount, 1.0), 2))

    @staticmethod
    def device_location(lat: float, lon: float, rng: np.random.Generator) -> tuple[float, float]:
        """Geolocalização de um aparelho parado em (lat, lon).

        Na maioria das vezes a coordenada é idêntica (mesmo Wi-Fi/IP); às vezes muda um
        pouco (rede móvel). Vale igual para clientes e fraudadores: se só um dos dois
        tivesse coordenada repetida, "distância zero" viraria um atalho artificial para o
        modelo (foi o que a primeira rodada adversarial revelou; ver docs/ADVERSARIAL.md).
        """
        if rng.random() < 0.7:
            return lat, lon
        return lat + float(rng.normal(0, 0.02)), lon + float(rng.normal(0, 0.02))

    def home_location(self, c: Customer, rng: np.random.Generator) -> tuple[float, float]:
        return self.device_location(c.home_lat, c.home_lon, rng)

    def legit_event(
        self,
        c: Customer,
        ts: float,
        rng: np.random.Generator,
        *,
        away_city: int | None = None,
        device: str | None = None,
    ) -> TxEvent:
        """Uma transação legítima do cliente `c` no instante `ts`."""
        u = rng.random()
        if u < c.channel_probs[0]:
            channel = Channel.POS
        elif u < c.channel_probs[0] + c.channel_probs[1]:
            channel = Channel.ECOMMERCE
        else:
            channel = Channel.TRANSFER
        own_device = device or c.devices[int(rng.integers(0, len(c.devices)))]
        if rng.random() < 0.02:  # aparelho avulso (computador do trabalho, navegador novo)
            own_device = self.next_id("d-guest")
        if channel is Channel.POS:
            if away_city is not None:
                pool = self._physical_by_city.get(away_city) or c.fav_physical
                m = self.merchants[_pick(pool, rng)]
            elif rng.random() < 0.88:
                m = self.merchants[_pick(c.fav_physical, rng)]
            else:
                pool = self._physical_by_city.get(c.city) or c.fav_physical
                m = self.merchants[_pick(pool, rng)]
            return TxEvent(
                tx_id=self.next_id(),
                customer_id=c.id,
                counterparty_id=m.id,
                amount=self.sample_amount(c, rng, m.category),
                channel=channel.value,
                category=m.category,
                device_id=c.card,
                lat=m.lat + float(rng.normal(0, 0.003)),
                lon=m.lon + float(rng.normal(0, 0.003)),
                ts=ts,
            )
        if away_city is not None:
            lat = CITIES[away_city][1] + float(rng.normal(0, 0.05))
            lon = CITIES[away_city][2] + float(rng.normal(0, 0.05))
        else:
            lat, lon = self.home_location(c, rng)
        if channel is Channel.ECOMMERCE:
            if rng.random() < 0.85:
                m = self.merchants[_pick(c.fav_online, rng)]
            else:
                m = self.merchants[_pick(self._online, rng)]
            return TxEvent(
                tx_id=self.next_id(),
                customer_id=c.id,
                counterparty_id=m.id,
                amount=self.sample_amount(c, rng, m.category),
                channel=channel.value,
                category=m.category,
                device_id=own_device,
                lat=lat,
                lon=lon,
                ts=ts,
            )
        amount = self.sample_amount(c, rng, "p2p")
        if rng.random() < 0.75:
            recipient = c.friends[int(rng.integers(0, len(c.friends)))]
        else:
            if rng.random() < 0.6:
                # conta que o sistema nunca viu (outro banco, prestador de serviço). Sem
                # isso, "recebedor inédito" só existiria em fraude e viraria um atalho.
                recipient = self.next_id("ext")
                if rng.random() < 0.3:  # vira contato recorrente
                    c.friends.append(recipient)
            else:
                recipient = self.customers[int(rng.integers(0, len(self.customers)))].id
                if recipient == c.id:
                    recipient = c.friends[0]
            if rng.random() < 0.12:  # pagamento grande e raro: aluguel, carro, obra
                amount = round(amount * float(rng.uniform(4, 15)), 2)
        return TxEvent(
            tx_id=self.next_id(),
            customer_id=c.id,
            counterparty_id=recipient,
            amount=amount,
            channel=channel.value,
            category="p2p",
            device_id=own_device,
            lat=lat,
            lon=lon,
            ts=ts,
        )

    # ------------------------------------------------------------------ tráfego legítimo
    def legit_stream(
        self, days: float, rng: np.random.Generator, start_ts: float | None = None
    ) -> list[LabeledTx]:
        start = self.start_ts if start_ts is None else start_ts
        out: list[LabeledTx] = []
        for c in self.customers:
            # 12% dos clientes abrem conta durante o período: sem isso "idade do cliente"
            # seria só um relógio da simulação, e não haveria clientes sem histórico
            join_day = float(rng.uniform(0, days)) if rng.random() < 0.12 else 0.0
            n = int(rng.poisson(c.rate * days))
            if n == 0:
                continue
            day_idx = rng.integers(0, max(int(days), 1), n)
            # viagem: alguns dias em outra cidade
            trip: tuple[float, float, int] | None = None
            if rng.random() < 0.12 and days >= 4:
                t0 = float(rng.uniform(0, days - 2))
                trip = (t0, t0 + float(rng.uniform(1, 5)), int(rng.integers(0, len(CITIES))))
            # troca de aparelho legítima
            switch_day = float(rng.uniform(0, days)) if rng.random() < 0.06 else None
            new_device = f"d-{c.id}-new"
            for k in range(n):
                hour = self.sample_hour(c, rng)
                day_pos = float(day_idx[k]) + hour / 24.0
                if day_pos >= days or day_pos < join_day:
                    continue
                ts = start + day_pos * DAY
                away = trip[2] if trip and trip[0] <= day_pos <= trip[1] else None
                device = new_device if switch_day is not None and day_pos >= switch_day else None
                event = self.legit_event(c, ts, rng, away_city=away, device=device)
                out.append(LabeledTx(event))
                if event.counterparty_id.startswith("ext") and rng.random() < 0.25:
                    out.extend(self._installments(event, start + days * DAY, rng))
        out.extend(self._collectors(days, rng, start))
        return out

    def _installments(
        self, first: TxEvent, horizon: float, rng: np.random.Generator
    ) -> list[LabeledTx]:
        """Pagamento parcelado a uma conta recém-conhecida (obra, prestador, compra entre
        pessoas): novas parcelas nos dias seguintes, muitas vezes maiores que a primeira.

        É o negativo difícil para "mudança gradual" e "fracionamento": pagar de novo, e
        mais, a um recebedor que acabou de aparecer também acontece em uso legítimo.
        """
        out: list[LabeledTx] = []
        ts, amount = first.ts, first.amount
        for _ in range(int(rng.integers(1, 4))):
            ts += float(rng.uniform(0.2, 4.0)) * DAY
            if ts >= horizon:
                break
            amount = round(amount * float(rng.uniform(0.8, 1.7)), 2)
            out.append(LabeledTx(replace(first, tx_id=self.next_id(), ts=ts, amount=amount)))
        return out

    def _collectors(self, days: float, rng: np.random.Generator, start: float) -> list[LabeledTx]:
        """Vaquinhas e lojas novas: contas novas que recebem de muita gente, legitimamente.

        É o negativo difícil para a detecção de conta-laranja: mesmo padrão de fan-in.
        """
        out: list[LabeledTx] = []
        n_collectors = max(1, self.n_customers // 250)
        for _ in range(n_collectors):
            recipient = self.next_id("vaq")
            t0 = start + float(rng.uniform(0, max(days - 3, 0.5))) * DAY
            n_senders = int(rng.integers(8, 40))
            small = bool(rng.random() < 0.7)
            for idx in rng.integers(0, len(self.customers), n_senders):
                c = self.customers[int(idx)]
                ts = t0 + float(rng.exponential(1.0)) * DAY
                if ts >= start + days * DAY:
                    continue
                lat, lon = self.home_location(c, rng)
                amount = float(rng.uniform(10, 80)) if small else self.sample_amount(c, rng, "p2p")
                out.append(
                    LabeledTx(
                        TxEvent(
                            tx_id=self.next_id(),
                            customer_id=c.id,
                            counterparty_id=recipient,
                            amount=round(amount, 2),
                            channel=Channel.TRANSFER.value,
                            category="p2p",
                            device_id=c.devices[0],
                            lat=lat,
                            lon=lon,
                            ts=ts,
                        ),
                        scenario="legit_collector",
                    )
                )
        return out

    # ------------------------------------------------------------------ fraudes conhecidas
    def known_fraud_stream(
        self,
        days: float,
        rng: np.random.Generator,
        episodes_per_1k_customers_day: float = 1.8,
        start_ts: float | None = None,
    ) -> list[LabeledTx]:
        """Fraudes "de manual", as que um time já tem rotuladas: cartão roubado em
        e-commerce, tomada de conta com transferência e teste de cartão."""
        start = self.start_ts if start_ts is None else start_ts
        n_episodes = int(
            rng.poisson(episodes_per_1k_customers_day * self.n_customers / 1000 * days)
        )
        out: list[LabeledTx] = []
        for _ in range(n_episodes):
            victim = self.customers[int(rng.integers(0, len(self.customers)))]
            ts = start + float(rng.uniform(0.5, days)) * DAY
            kind = rng.choice(
                ["stolen_card", "account_takeover", "card_testing"], p=[0.5, 0.3, 0.2]
            )
            episode = self.next_id("ep")
            events = getattr(self, f"_fraud_{kind}")(victim, ts, rng)
            out.extend(
                LabeledTx(e, True, str(kind), episode) for e in events if e.ts < start + days * DAY
            )
        return out

    def _fraudster_location(
        self, victim: Customer, rng: np.random.Generator
    ) -> tuple[float, float]:
        u = rng.random()
        if u < 0.35:  # mesma cidade da vítima: geografia não ajuda
            return (
                CITIES[victim.city][1] + float(rng.normal(0, 0.08)),
                CITIES[victim.city][2] + float(rng.normal(0, 0.08)),
            )
        if u < 0.55:  # exterior
            return float(rng.uniform(-35, 55)), float(rng.uniform(-120, 140))
        city = CITIES[int(rng.integers(0, len(CITIES)))]
        return city[1] + float(rng.normal(0, 0.1)), city[2] + float(rng.normal(0, 0.1))

    def _fraud_stolen_card(
        self, victim: Customer, ts: float, rng: np.random.Generator
    ) -> list[TxEvent]:
        lat, lon = self._fraudster_location(victim, rng)
        device = self.next_id("d-x")
        events = []
        t = ts
        for _ in range(int(rng.integers(2, 7))):
            m = self.merchants[_pick(self._online, rng)]
            amount = victim.typical_amount * float(rng.uniform(0.8, 6))
            events.append(
                TxEvent(
                    tx_id=self.next_id(),
                    customer_id=victim.id,
                    counterparty_id=m.id,
                    amount=round(amount, 2),
                    channel=Channel.ECOMMERCE.value,
                    category=m.category,
                    device_id=device,
                    lat=(loc := self.device_location(lat, lon, rng))[0],
                    lon=loc[1],
                    ts=t,
                )
            )
            t += float(rng.exponential(20 * 60))
        return events

    def _fraud_account_takeover(
        self, victim: Customer, ts: float, rng: np.random.Generator
    ) -> list[TxEvent]:
        lat, lon = self._fraudster_location(victim, rng)
        # 30% das tomadas de conta usam o aparelho da vítima (acesso remoto, engenharia social)
        device = victim.devices[0] if rng.random() < 0.3 else self.next_id("d-x")
        recipient = self.next_id("mule")
        events = []
        t = ts
        for _ in range(int(rng.integers(1, 5))):
            amount = victim.typical_amount * float(rng.uniform(2, 15))
            events.append(
                TxEvent(
                    tx_id=self.next_id(),
                    customer_id=victim.id,
                    counterparty_id=recipient,
                    amount=round(amount, 2),
                    channel=Channel.TRANSFER.value,
                    category="p2p",
                    device_id=device,
                    lat=(loc := self.device_location(lat, lon, rng))[0],
                    lon=loc[1],
                    ts=t,
                )
            )
            t += float(rng.exponential(8 * 60))
        return events

    def _fraud_card_testing(
        self, victim: Customer, ts: float, rng: np.random.Generator
    ) -> list[TxEvent]:
        lat, lon = self._fraudster_location(victim, rng)
        device = self.next_id("d-x")
        m = self.merchants[_pick(self._online, rng)]
        events = []
        t = ts
        n_probes = int(rng.integers(3, 9))
        for k in range(n_probes + 1):
            last = k == n_probes
            amount = (
                victim.typical_amount * float(rng.uniform(4, 12))
                if last
                else float(rng.uniform(1, 6))
            )
            events.append(
                TxEvent(
                    tx_id=self.next_id(),
                    customer_id=victim.id,
                    counterparty_id=m.id,
                    amount=round(amount, 2),
                    channel=Channel.ECOMMERCE.value,
                    category=m.category,
                    device_id=device,
                    lat=(loc := self.device_location(lat, lon, rng))[0],
                    lon=loc[1],
                    ts=t,
                )
            )
            t += float(rng.exponential(60))
        return events

    # ------------------------------------------------------------------ fluxo completo
    def generate(
        self,
        days: float,
        seed: int,
        *,
        with_known_fraud: bool = True,
        start_ts: float | None = None,
        known_fraud_rate: float = 1.8,
    ) -> list[LabeledTx]:
        """Tráfego legítimo + fraudes conhecidas, ordenado por tempo do evento."""
        rng = np.random.default_rng(seed)
        stream = self.legit_stream(days, rng, start_ts)
        if with_known_fraud:
            stream.extend(self.known_fraud_stream(days, rng, known_fraud_rate, start_ts=start_ts))
        stream.sort(key=lambda x: x.event.ts)
        return stream


def _pick(pool: list[int], rng: np.random.Generator) -> int:
    """Sorteio uniforme; bem mais rápido que `rng.choice` para um único item."""
    return pool[int(rng.integers(0, len(pool)))]
