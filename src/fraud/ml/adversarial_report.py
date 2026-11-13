"""Reproduz o ciclo adversarial inteiro (Fase D) e grava os resultados.

    python -m fraud.ml.adversarial_report --out docs/adversarial_runs

Treina cada estágio do ciclo e roda a mesma bateria de ataques contra todos:

    v1                    features base, sem ataques no treino      (ponto de partida)
    v2_features           + features v2, sem ataques no treino      (o que as features dão sozinhas)
    v2_adversarial        + ataques no treino                       (modelo final)
    v2_sem_anomalia       modelo final sem o score de anomalia      (ablação da peça híbrida)
    v2_ataque_inedito     cada ataque avaliado por um modelo treinado SEM ele (leave-one-out)
    v2_ataque_inedito_sem_anomalia   o mesmo, sem o score de anomalia

O último estágio é o controle mais importante: treinar e testar com o mesmo gerador de
ataques mede em parte a capacidade de reconhecer o gerador. Deixar um ataque inteiro fora do
treino mede o que interessa: o que acontece quando aparece um golpe que ninguém rotulou.

A bateria usa outra população e outras sementes, em dois níveis de discrição: `padrao`
(mesma faixa de `stealth` do treino adversarial) e `furtivo` (faixa nunca vista no treino).
"""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from fraud.features.compute import categorical_features, feature_names
from fraud.ml.adversarial import (
    AttackEvalConfig,
    build_attack_dataset,
    evaluate_attacks,
    format_table,
    meets_target,
)
from fraud.ml.dataset import temporal_split
from fraud.ml.model import FraudModel, train_model
from fraud.ml.train import TrainConfig, build_training_dataset
from fraud.simulation.attacks import ATTACK_LABELS, ATTACKS

STAGES: dict[str, tuple[str, dict[str, Any], bool]] = {
    # nome: (descrição, overrides do TrainConfig, usa score de anomalia)
    "v1": (
        "Features base, sem ataques no treino",
        {"feature_version": 1, "adversarial_episodes": 0},
        True,
    ),
    "v2_features": ("+ features v2", {"feature_version": 2, "adversarial_episodes": 0}, True),
    "v2_adversarial": ("+ treino adversarial (final)", {"feature_version": 2}, True),
    "v2_sem_anomalia": ("Final sem score de anomalia", {"feature_version": 2}, False),
}


def train_stage(base: TrainConfig, overrides: dict[str, Any], use_anomaly: bool) -> FraudModel:
    cfg = replace(base, **overrides)
    train, valid, _ = temporal_split(build_training_dataset(cfg))
    params = replace(cfg.params, use_anomaly=use_anomaly)
    return train_model(
        train,
        valid,
        feature_names(cfg.feature_version),
        categorical_features(cfg.feature_version),
        cfg.feature_version,
        params,
    )


def run(quick: bool = False) -> dict[str, Any]:
    base = TrainConfig.quick() if quick else TrainConfig()
    eval_cfg = AttackEvalConfig.quick() if quick else AttackEvalConfig()
    suites = {
        "padrao": build_attack_dataset(eval_cfg),
        "furtivo": build_attack_dataset(eval_cfg.stealthier()),
    }
    results: dict[str, Any] = {"stages": {}, "quick": quick}
    for name, (description, overrides, use_anomaly) in STAGES.items():
        model = train_stage(base, overrides, use_anomaly)
        stage: dict[str, Any] = {"description": description, "model_version": model.version}
        for suite_name, ds in suites.items():
            result = evaluate_attacks(model, ds)
            result["meets_target"] = meets_target(result)
            stage[suite_name] = result
            print(f"\n== {name} / {suite_name} ==\n{format_table(result)}")
        results["stages"][name] = stage
    results["stages"][LEAVE_ONE_OUT] = leave_one_out(base, suites, use_anomaly=True)
    results["stages"][LEAVE_ONE_OUT_PLAIN] = leave_one_out(base, suites, use_anomaly=False)
    return results


LEAVE_ONE_OUT = "v2_ataque_inedito"
LEAVE_ONE_OUT_PLAIN = "v2_ataque_inedito_sem_anomalia"


def leave_one_out(
    base: TrainConfig, suites: dict[str, Any], *, use_anomaly: bool
) -> dict[str, Any]:
    """Para cada ataque, treina com os outros quatro e mede só o que ficou de fora.

    Rodar com e sem o score de anomalia é o teste direto da hipótese do modelo híbrido: se
    a Isolation Forest ajuda em algum lugar, é contra o ataque que ninguém rotulou.
    """
    stage: dict[str, Any] = {
        "description": "Cada ataque avaliado por um modelo que nunca o viu"
        + ("" if use_anomaly else " (sem score de anomalia)"),
        "model_version": "um modelo por ataque",
    }
    for suite_name in suites:
        stage[suite_name] = {"attacks": {}, "legit_false_positive_rate": 0.0, "n_legit": 0}
    for held_out in ATTACKS:
        kinds = tuple(k for k in ATTACKS if k != held_out)
        model = train_stage(base, {"feature_version": 2, "adversarial_kinds": kinds}, use_anomaly)
        for suite_name, ds in suites.items():
            result = evaluate_attacks(model, ds)
            target = stage[suite_name]
            target["attacks"][held_out] = result["attacks"][held_out]
            target["n_legit"] = result["n_legit"]
            # média dos cinco modelos
            target["legit_false_positive_rate"] += result["legit_false_positive_rate"] / len(
                ATTACKS
            )
            r = result["attacks"][held_out]
            print(
                f"leave-one-out {held_out:<16} {suite_name:<8} detecção={r['episode_detection']:.1%} "
                f"valor evitado={r['value_prevented']:.1%} recall tx={r['tx_recall']:.1%}"
            )
    for suite_name in suites:
        stage[suite_name]["meets_target"] = meets_target(stage[suite_name])
    return stage


def markdown(results: dict[str, Any]) -> str:
    lines: list[str] = []
    for suite, title in (
        ("padrao", "Bateria padrão (discrição 0,0 a 0,7)"),
        ("furtivo", "Bateria furtiva (discrição 0,7 a 1,0, nunca vista no treino)"),
    ):
        stages = results["stages"]
        lines += [f"### {title}", ""]
        for metric, label in (
            ("episode_detection", "Detecção por episódio"),
            ("value_prevented", "Valor evitado"),
            ("tx_recall", "Recall por transação"),
        ):
            lines += [f"**{label}**", "", "| Ataque | " + " | ".join(stages) + " |"]
            lines.append("|---|" + "---:|" * len(stages))
            for kind, kind_label in ATTACK_LABELS.items():
                cells = [f"{s[suite]['attacks'][kind][metric]:.1%}" for s in stages.values()]
                lines.append(f"| {kind_label} | " + " | ".join(cells) + " |")
            if metric == "episode_detection":
                fpr = [f"{s[suite]['legit_false_positive_rate']:.2%}" for s in stages.values()]
                lines.append("| *Falso positivo em legítimas* | " + " | ".join(fpr) + " |")
            lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="docs/adversarial_runs")
    parser.add_argument("--quick", action="store_true")
    args = parser.parse_args()
    results = run(args.quick)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "latest.json").write_text(json.dumps(results, indent=2))
    (out / "latest.md").write_text(markdown(results))
    print(f"\nresultados em {out}/latest.json e {out}/latest.md")


if __name__ == "__main__":
    main()
