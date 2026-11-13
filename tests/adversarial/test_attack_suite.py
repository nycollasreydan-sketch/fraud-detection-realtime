"""Suíte adversarial (Fase D): os cinco ataques rodam contra o modelo como teste automático.

O modelo é treinado aqui com a configuração padrão (a mesma do `make up`), e a bateria usa
outra população e outras sementes. Se uma mudança em features, simulador ou treino derrubar
a detecção de algum ataque abaixo do alvo, o teste daquele ataque falha.

Alvo e justificativa: docs/ADVERSARIAL.md.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from fraud.ml.adversarial import (
    MAX_LEGIT_FPR,
    TARGET_EPISODE_DETECTION,
    TARGET_VALUE_PREVENTED,
    AttackEvalConfig,
    build_attack_dataset,
    evaluate_attacks,
    format_table,
)
from fraud.ml.adversarial_report import train_stage
from fraud.ml.model import FraudModel
from fraud.ml.train import TrainConfig
from fraud.simulation.attacks import ATTACKS

pytestmark = pytest.mark.adversarial

# Piso para a bateria furtiva (ataques mais discretos do que os vistos no treino): não é o
# alvo, é um alarme de regressão.
STEALTH_DETECTION_FLOOR = 0.85
STEALTH_VALUE_FLOOR = 0.60


@pytest.fixture(scope="module")
def final_model() -> FraudModel:
    return train_stage(TrainConfig(), {}, use_anomaly=TrainConfig().params.use_anomaly)


@pytest.fixture(scope="module")
def standard(final_model: FraudModel) -> dict[str, Any]:
    result = evaluate_attacks(final_model, build_attack_dataset(AttackEvalConfig()))
    print("\n\nBateria padrão\n" + format_table(result))
    out = Path("artifacts")
    out.mkdir(exist_ok=True)
    (out / "adversarial_suite.json").write_text(json.dumps(result, indent=2))
    return result


@pytest.fixture(scope="module")
def stealthy(final_model: FraudModel) -> dict[str, Any]:
    result = evaluate_attacks(final_model, build_attack_dataset(AttackEvalConfig().stealthier()))
    print("\n\nBateria furtiva\n" + format_table(result))
    return result


@pytest.mark.parametrize("attack", ATTACKS)
def test_attack_is_detected(standard: dict[str, Any], attack: str) -> None:
    r = standard["attacks"][attack]
    assert r["episodes"] >= 50
    assert r["episode_detection"] >= TARGET_EPISODE_DETECTION, (
        f"{attack}: só {r['episode_detection']:.1%} dos episódios detectados"
    )
    assert r["value_prevented"] >= TARGET_VALUE_PREVENTED, (
        f"{attack}: só {r['value_prevented']:.1%} do valor evitado"
    )


def test_detection_does_not_come_from_flagging_everything(standard: dict[str, Any]) -> None:
    """A guarda que dá sentido aos outros testes: detectar tudo alertando tudo não vale."""
    assert standard["n_legit"] > 20_000
    assert standard["legit_false_positive_rate"] <= MAX_LEGIT_FPR


@pytest.mark.parametrize("attack", ATTACKS)
def test_stealthier_attack_does_not_collapse(stealthy: dict[str, Any], attack: str) -> None:
    r = stealthy["attacks"][attack]
    assert r["episode_detection"] >= STEALTH_DETECTION_FLOOR
    assert r["value_prevented"] >= STEALTH_VALUE_FLOOR


def test_stealth_suite_respects_false_positive_budget(stealthy: dict[str, Any]) -> None:
    assert stealthy["legit_false_positive_rate"] <= MAX_LEGIT_FPR
