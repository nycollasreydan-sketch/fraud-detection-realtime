from __future__ import annotations

import numpy as np
import pytest

from fraud.simulation.attacks import ATTACKS, attack_stream, replay, structuring
from fraud.simulation.generator import batches, build_stream, to_payload
from fraud.simulation.world import World


@pytest.fixture(scope="module")
def world() -> World:
    return World(n_customers=60, n_merchants=40, seed=9)


def test_world_is_deterministic() -> None:
    a = World(n_customers=20, n_merchants=20, seed=1).generate(5, seed=2)
    b = World(n_customers=20, n_merchants=20, seed=1).generate(5, seed=2)
    assert [x.event for x in a] == [x.event for x in b]
    c = World(n_customers=20, n_merchants=20, seed=1).generate(5, seed=3)
    assert [x.event for x in a] != [x.event for x in c]


def test_stream_is_sorted_labelled_and_mostly_legit(world: World) -> None:
    stream = world.generate(20, seed=1)
    ts = [x.event.ts for x in stream]
    assert ts == sorted(ts)
    frauds = [x for x in stream if x.is_fraud]
    assert 0 < len(frauds) / len(stream) < 0.05
    assert all(x.episode and x.scenario != "legit" for x in frauds)
    assert all(x.event.amount >= 1.0 for x in stream)
    assert len({x.event.tx_id for x in stream}) == len(stream)


def test_legit_traffic_contains_hard_negatives(world: World) -> None:
    """Sem negativos difíceis a avaliação seria trivial (ver docstring de world.py)."""
    stream = world.legit_stream(30, np.random.default_rng(4))
    new_accounts = {
        x.event.counterparty_id for x in stream if x.event.counterparty_id.startswith("ext")
    }
    guest_devices = [x for x in stream if x.event.device_id.startswith("d-guest")]
    collectors = [x for x in stream if x.scenario == "legit_collector"]
    assert len(new_accounts) > 20
    assert guest_devices
    assert collectors
    assert not any(x.is_fraud for x in stream)


def test_attacks_use_the_victims_own_device_and_normal_amounts(world: World) -> None:
    rng = np.random.default_rng(5)
    victim = world.customers[3]
    events = structuring(world, victim, world.start_ts + 86400 * 5, rng, stealth=0.3)
    assert len(events) > 8
    assert {e.device_id for e in events} == {victim.devices[0]}
    assert len({e.counterparty_id for e in events}) == 1
    assert max(e.amount for e in events) < victim.typical_amount * 1.5
    assert sum(e.amount for e in events) > victim.typical_amount * 10


def test_replay_copies_are_exact(world: World) -> None:
    rng = np.random.default_rng(6)
    victim = world.customers[0]
    original = world.legit_event(victim, world.start_ts + 1000, rng)
    copies = replay(world, victim, world.start_ts, rng, 0.0, original)
    assert len(copies) >= 2
    for c in copies:
        assert (c.amount, c.counterparty_id, c.device_id, c.lat, c.lon) == (
            original.amount, original.counterparty_id, original.device_id, original.lat, original.lon,
        )  # fmt: skip
        assert c.ts > original.ts
        assert c.tx_id != original.tx_id


def test_attack_stream_covers_every_kind(world: World) -> None:
    rng = np.random.default_rng(7)
    legit = world.legit_stream(20, rng)
    attacks = attack_stream(world, legit, ATTACKS, 3, rng, first_day=10, last_day=20)
    assert {x.scenario for x in attacks} == set(ATTACKS)
    assert all(x.is_fraud for x in attacks)
    start = world.start_ts + 10 * 86400
    assert all(x.event.ts >= start for x in attacks if x.scenario != "replay")
    assert len({x.episode for x in attacks}) == 3 * len(ATTACKS)


def test_stealth_makes_attacks_slower(world: World) -> None:
    victim = world.customers[5]

    def mean_gap(stealth: float) -> float:
        events = structuring(world, victim, world.start_ts, np.random.default_rng(8), stealth)
        return float(np.mean(np.diff([e.ts for e in events])))

    assert mean_gap(1.0) > 3 * mean_gap(0.0)


def test_generator_stream_and_payload() -> None:
    stream = build_stream(
        n_customers=15, days=4, seed=1, attacks_per_kind=1, end_ts=2e9, run_id="r1"
    )
    assert stream
    assert all(x.event.customer_id.startswith("r1-") for x in stream)
    assert max(x.event.ts for x in stream) < 2e9
    payload = to_payload(stream[0])
    assert payload["occurred_at"].endswith("+00:00")  # type: ignore[union-attr]
    assert set(payload) >= {"customer_id", "counterparty_id", "amount", "channel", "device_id"}


def test_batches_never_repeat_an_entity() -> None:
    stream = build_stream(
        n_customers=10, days=3, seed=2, attacks_per_kind=1, end_ts=2e9, run_id="r"
    )
    groups = batches(stream, 8)
    assert [x for g in groups for x in g] == stream  # ordem preservada
    for group in groups:
        ids = [x.event.customer_id for x in group] + [x.event.counterparty_id for x in group]
        assert len(ids) == len(set(ids))
        assert len(group) <= 8
