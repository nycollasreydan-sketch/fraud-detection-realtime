from __future__ import annotations

import json
import math

import pytest

from fraud.features.compute import (
    ALL_FEATURES,
    FEATURE_SPECS,
    apply_transaction,
    categorical_features,
    compute_features,
    day_of_week,
    feature_names,
    haversine_km,
    hour_of_day,
    update_counterparty,
    update_customer,
)
from fraud.features.state import (
    DAY,
    HOUR,
    MAX_COUNTERPARTIES,
    MAX_DEVICES,
    MAX_RECENT,
    CounterpartyState,
    CustomerState,
    decay,
)
from tests.conftest import BASE_TS, make_tx


def step(tx, cs, cps):  # type: ignore[no-untyped-def]
    feats = compute_features(tx, cs, cps)
    apply_transaction(tx, cs, cps)
    return feats


# ---------------------------------------------------------------- decaimento
def test_decay_halves_after_one_half_life() -> None:
    assert decay(8.0, HOUR, HOUR) == pytest.approx(4.0)
    assert decay(8.0, 3 * HOUR, HOUR) == pytest.approx(1.0)


def test_decay_ignores_out_of_order_events() -> None:
    assert decay(8.0, -100.0, HOUR) == 8.0
    assert decay(8.0, 0.0, HOUR) == 8.0


# ---------------------------------------------------------------- utilidades
def test_haversine_known_distance() -> None:
    # São Paulo -> Rio de Janeiro: cerca de 360 km
    assert haversine_km(-23.55, -46.63, -22.91, -43.17) == pytest.approx(360, abs=10)
    assert haversine_km(1.0, 2.0, 1.0, 2.0) == 0.0


def test_time_helpers() -> None:
    assert hour_of_day(BASE_TS + 13.5 * HOUR) == pytest.approx(13.5)
    assert day_of_week(BASE_TS) == 0  # 2026-01-05 é segunda-feira
    assert day_of_week(BASE_TS + 6 * DAY) == 6


def test_feature_registry_is_consistent() -> None:
    names = [s.name for s in FEATURE_SPECS]
    assert len(names) == len(set(names))
    assert set(feature_names(1)) < set(feature_names(2)) == set(ALL_FEATURES)
    assert set(categorical_features(2)) == {"channel", "category"}
    feats = compute_features(make_tx(), CustomerState(), CounterpartyState())
    assert set(feats) == set(ALL_FEATURES)
    assert all(math.isfinite(v) for v in feats.values())


# ---------------------------------------------------------------- sem vazamento
def test_transaction_never_sees_itself() -> None:
    cs, cps = CustomerState(), CounterpartyState()
    feats = step(make_tx("t1"), cs, cps)
    assert feats["c_tx_count"] == 0.0
    assert feats["c_cnt_1h"] == 0.0
    assert feats["c_new_device"] == 0.0  # sem histórico não há "novo"
    assert feats["cp_tx_count"] == 0.0
    assert cs.n == 1
    assert cps.n == 1


def test_velocity_counts_previous_transactions() -> None:
    cs, cps = CustomerState(), CounterpartyState()
    for i in range(3):
        step(make_tx(f"t{i}", ts=BASE_TS + i * 60), cs, cps)
    feats = compute_features(make_tx("t3", ts=BASE_TS + 180), cs, cps)
    assert feats["c_tx_count"] == pytest.approx(math.log1p(3))
    assert 2.8 < feats["c_cnt_1h"] < 3.0  # três transações há poucos minutos
    assert feats["c_amt_1h"] == pytest.approx(feats["c_cnt_1h"] * 100, rel=1e-6)
    # um dia depois a janela de 1h esvaziou e a de 7d quase não mudou
    later = compute_features(make_tx("t4", ts=BASE_TS + DAY), cs, cps)
    assert later["c_cnt_1h"] < 1e-6
    assert later["c_cnt_7d"] > 2.7


def test_novelty_flags() -> None:
    cs, cps = CustomerState(), CounterpartyState()
    step(make_tx("t1"), cs, cps)
    same = compute_features(make_tx("t2", ts=BASE_TS + 60), cs, cps)
    assert same["c_new_device"] == 0.0
    assert same["c_new_counterparty"] == 0.0
    other = compute_features(
        make_tx("t3", device="d-x", counterparty="m9", ts=BASE_TS + 60), cs, CounterpartyState()
    )
    assert other["c_new_device"] == 1.0
    assert other["c_new_counterparty"] == 1.0


def test_amount_deviation_is_relative_to_the_customer() -> None:
    cs, cps = CustomerState(), CounterpartyState()
    for i in range(10):
        step(make_tx(f"t{i}", amount=100.0 + i, ts=BASE_TS + i * HOUR), cs, cps)
    normal = compute_features(make_tx("n", amount=105.0, ts=BASE_TS + 11 * HOUR), cs, cps)
    big = compute_features(make_tx("b", amount=2000.0, ts=BASE_TS + 11 * HOUR), cs, cps)
    assert abs(normal["c_amount_z"]) < 1
    assert big["c_amount_z"] > 5
    assert big["c_amount_ratio"] == pytest.approx(2000 / normal["c_mean_amt_7d"])


def test_impossible_travel() -> None:
    cs, cps = CustomerState(), CounterpartyState()
    step(make_tx("t1"), cs, cps)
    feats = compute_features(
        make_tx("t2", lat=48.85, lon=2.35, ts=BASE_TS + 600), cs, cps
    )  # Paris, 10 min depois
    assert feats["c_dist_last_km"] > 9000
    assert feats["c_speed_kmh"] > 3000
    assert feats["c_dist_home_km"] > 9000


# ---------------------------------------------------------------- estado
def test_update_is_idempotent_per_transaction() -> None:
    cs, cps = CustomerState(), CounterpartyState()
    tx = make_tx("t1")
    assert update_customer(cs, tx) is True
    assert update_customer(cs, tx) is False
    assert update_counterparty(cps, tx) is True
    assert update_counterparty(cps, tx) is False
    assert cs.n == 1
    assert cps.n == 1
    assert cs.cnt_24h == 1.0


def test_state_survives_json_roundtrip() -> None:
    cs, cps = CustomerState(), CounterpartyState()
    for i in range(12):
        step(make_tx(f"t{i}", counterparty=f"m{i % 3}", ts=BASE_TS + i * 500), cs, cps)
    cs2 = CustomerState.from_dict(json.loads(json.dumps(cs.to_dict())))
    cps2 = CounterpartyState.from_dict(json.loads(json.dumps(cps.to_dict())))
    tx = make_tx("next", ts=BASE_TS + 9000)
    assert compute_features(tx, cs2, cps2) == compute_features(tx, cs, cps)


def test_from_dict_ignores_unknown_fields() -> None:
    # estado gravado por uma versão mais nova do código não pode derrubar o worker
    assert CustomerState.from_dict({"n": 3, "campo_do_futuro": 1}).n == 3


def test_state_size_is_bounded() -> None:
    cs, cps = CustomerState(), CounterpartyState()
    for i in range(200):
        tx = make_tx(f"t{i}", counterparty=f"m{i}", device=f"d{i}", ts=BASE_TS + i * 60)
        step(tx, cs, CounterpartyState())
    assert len(cs.devices) == MAX_DEVICES
    assert len(cs.counterparties) == MAX_COUNTERPARTIES
    assert len(cs.recent) == MAX_RECENT
    assert "m199" in cs.counterparties
    assert "m0" not in cs.counterparties
    assert len(json.dumps(cs.to_dict())) < 8000
    assert cps.n == 0


def test_out_of_order_event_does_not_rewind_the_clock() -> None:
    cs, cps = CustomerState(), CounterpartyState()
    step(make_tx("t1", ts=BASE_TS + HOUR), cs, cps)
    step(make_tx("t0", ts=BASE_TS), cs, cps)  # chegou atrasada
    assert cs.last_ts == BASE_TS + HOUR
    assert cs.cnt_1h == pytest.approx(2.0)


# ---------------------------------------------------------------- features v2 (Fase D)
def test_replay_shows_up_as_exact_duplicate() -> None:
    cs, cps = CustomerState(), CounterpartyState()
    step(make_tx("t1", amount=59.9), cs, cps)
    step(make_tx("t2", amount=12.0, counterparty="m2", ts=BASE_TS + 30), cs, CounterpartyState())
    feats = compute_features(make_tx("t3", amount=59.9, ts=BASE_TS + 120), cs, cps)
    assert feats["c_dup_count_24h"] == 1.0
    assert feats["c_secs_since_dup"] == pytest.approx(math.log1p(120))
    different = compute_features(make_tx("t4", amount=60.9, ts=BASE_TS + 120), cs, cps)
    assert different["c_dup_count_24h"] == 0.0


def test_structuring_shows_up_as_cumulative_spend() -> None:
    cs, cps = CustomerState(), CounterpartyState()
    for i in range(30):  # um mês de rotina: uma compra de ~100 por dia
        step(make_tx(f"h{i}", ts=BASE_TS + i * DAY), cs, cps)
    t0 = BASE_TS + 30 * DAY
    mule = CounterpartyState()
    ratios = []
    for i in range(12):  # 12 transferências "normais" de 100 em uma hora
        tx = make_tx(f"s{i}", counterparty="acct", channel="transfer", ts=t0 + i * 300)
        ratios.append(step(tx, cs, mule)["c_spend_24h_ratio"])
    assert ratios[0] < 2.5
    assert ratios[-1] > 5
    assert ratios[-1] > 4 * ratios[0]
    last = compute_features(make_tx("s", counterparty="acct", ts=t0 + 3700), cs, mule)
    assert last["c_cp_cnt_24h"] > 10
    assert last["c_recent_amount_cv"] < 0.05  # valores iguais demais


def test_gradual_drift_shows_up_against_slow_baseline() -> None:
    cs, cps = CustomerState(), CounterpartyState()
    for i in range(30):
        step(make_tx(f"h{i}", ts=BASE_TS + i * DAY), cs, cps)
    acct = CounterpartyState()
    amount, feats = 60.0, {}
    for i in range(15):
        tx = make_tx(f"g{i}", counterparty="acct", amount=amount, ts=BASE_TS + (30 + i) * DAY)
        feats = step(tx, cs, acct)
        amount *= 1.2
    assert feats["c_cp_increase_streak"] == 14.0
    assert feats["c_cp_amount_vs_first"] > 10
    assert feats["c_slow_amount_z"] > 2
    assert feats["c_fast_slow_shift"] > 0  # o "normal" recente está subindo


def test_mule_account_shows_up_on_the_receiving_side() -> None:
    mule = CounterpartyState()
    feats = {}
    for i in range(15):
        tx = make_tx(f"v{i}", customer=f"c{i}", counterparty="mule", ts=BASE_TS + i * 600)
        feats = step(tx, CustomerState(), mule)
    assert feats["cp_new_senders_24h"] > 12
    assert feats["cp_new_sender_ratio"] == pytest.approx(1.0)
    assert feats["cp_age_days"] < 0.2
    # loja antiga com fregueses recorrentes: mesmo volume, nenhum pagador inédito
    shop = CounterpartyState()
    for i in range(60):
        tx = make_tx(f"s{i}", customer=f"c{i % 5}", counterparty="shop", ts=BASE_TS + i * DAY / 2)
        feats = step(tx, CustomerState(), shop)
    assert feats["cp_new_sender_ratio"] < 0.05


def test_off_hours_shows_up_against_own_profile() -> None:
    cs, cps = CustomerState(), CounterpartyState()
    for i in range(30):
        step(make_tx(f"h{i}", ts=BASE_TS + i * DAY + 18 * HOUR), cs, cps)
    usual = compute_features(make_tx("u", ts=BASE_TS + 31 * DAY + 18.5 * HOUR), cs, cps)
    night = compute_features(make_tx("n", ts=BASE_TS + 31 * DAY + 4 * HOUR), cs, cps)
    assert usual["c_hour_prob"] > 0.9
    assert night["c_hour_prob"] == 0.0
