"""Avaliação adversarial: roda os ataques contra um modelo e mede o que passou.

Métricas por tipo de ataque:

* **detecção por episódio**: fração dos episódios com pelo menos um alerta;
* **valor evitado**: fração do valor do episódio a partir do primeiro alerta (supõe que o
  alerta congela a conta, então tudo o que vem depois é evitado);
* **recall por transação**: fração das transações do ataque sinalizadas. É reportado, mas
  não é alvo: a primeira transação de um ataque bem-feito é indistinguível por construção.

O limiar é o do modelo (definido na validação do treino); nada é reajustado aqui.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from fraud.ml.dataset import Dataset, build_dataset, drop_burn_in
from fraud.ml.model import FraudModel
from fraud.simulation.attacks import ATTACKS, attack_stream
from fraud.simulation.world import World

# Alvo da Fase D (justificativa em docs/ADVERSARIAL.md)
TARGET_EPISODE_DETECTION = 0.90
TARGET_VALUE_PREVENTED = 0.70
MAX_LEGIT_FPR = 0.005


@dataclass(frozen=True)
class AttackEvalConfig:
    """População e sementes diferentes das do treino: mede generalização, não memória."""

    n_customers: int = 800
    n_merchants: int = 250
    days: float = 35.0
    warmup_days: float = 21.0  # histórico construído antes; não entra na avaliação
    episodes_per_attack: int = 60
    world_seed: int = 101
    stream_seed: int = 202
    stealth_range: tuple[float, float] = (0.0, 0.7)

    @staticmethod
    def quick() -> AttackEvalConfig:
        return AttackEvalConfig(
            n_customers=300, n_merchants=120, days=21.0, warmup_days=14.0, episodes_per_attack=25
        )

    def stealthier(self) -> AttackEvalConfig:
        """Ataques mais discretos do que qualquer coisa vista no treino adversarial."""
        return AttackEvalConfig(
            **{**self.__dict__, "stealth_range": (0.7, 1.0), "stream_seed": self.stream_seed + 1}
        )


def build_attack_dataset(cfg: AttackEvalConfig, kinds: tuple[str, ...] = ATTACKS) -> Dataset:
    world = World(n_customers=cfg.n_customers, n_merchants=cfg.n_merchants, seed=cfg.world_seed)
    rng = np.random.default_rng(cfg.stream_seed)
    total_days = cfg.warmup_days + cfg.days
    stream = world.legit_stream(total_days, rng)
    stream.extend(
        attack_stream(
            world, stream, kinds, cfg.episodes_per_attack, rng,
            first_day=cfg.warmup_days, last_day=total_days, stealth_range=cfg.stealth_range,
        )
    )  # fmt: skip
    stream.sort(key=lambda x: x.event.ts)
    return drop_burn_in(build_dataset(stream), world.start_ts, cfg.warmup_days)


def evaluate_attacks(model: FraudModel, ds: Dataset) -> dict[str, Any]:
    scores = model.predict_proba(ds.X)
    flagged = scores >= model.review_threshold
    meta = ds.meta.assign(flagged=flagged, score=scores)
    legit = meta[ds.y == 0]
    result: dict[str, Any] = {
        "model_version": model.version,
        "review_threshold": model.review_threshold,
        "legit_false_positive_rate": float(legit["flagged"].mean()),
        "n_legit": len(legit),
        "attacks": {},
    }
    for kind in sorted(set(meta["scenario"]) - {"legit", "legit_collector"}):
        rows = meta[meta["scenario"] == kind]
        detected: list[bool] = []
        prevented: list[float] = []
        to_detect: list[int] = []
        for _, ep in rows.groupby("episode", sort=False):
            ep = ep.sort_values("ts")
            hits = np.flatnonzero(ep["flagged"].to_numpy())
            amounts = ep["amount"].to_numpy()
            detected.append(len(hits) > 0)
            if len(hits):
                prevented.append(float(amounts[hits[0] :].sum() / amounts.sum()))
                to_detect.append(int(hits[0]) + 1)
            else:
                prevented.append(0.0)
        result["attacks"][kind] = {
            "episodes": len(detected),
            "transactions": len(rows),
            "episode_detection": float(np.mean(detected)),
            "value_prevented": float(np.mean(prevented)),
            "tx_recall": float(rows["flagged"].mean()),
            "median_tx_to_detect": float(np.median(to_detect)) if to_detect else None,
        }
    return result


def meets_target(result: dict[str, Any]) -> dict[str, bool]:
    """Para cada ataque, se o alvo foi atingido; `_fpr` é a guarda de falso positivo."""
    out = {
        kind: r["episode_detection"] >= TARGET_EPISODE_DETECTION
        and r["value_prevented"] >= TARGET_VALUE_PREVENTED
        for kind, r in result["attacks"].items()
    }
    out["_fpr"] = result["legit_false_positive_rate"] <= MAX_LEGIT_FPR
    return out


def format_table(result: dict[str, Any]) -> str:
    lines = [
        f"modelo {result['model_version']}  |  FPR em legítimas: "
        f"{result['legit_false_positive_rate']:.3%} (n={result['n_legit']})",
        f"{'ataque':<18}{'episódios':>10}{'detecção':>10}{'valor evitado':>15}{'recall tx':>11}",
    ]
    for kind, r in result["attacks"].items():
        lines.append(
            f"{kind:<18}{r['episodes']:>10}{r['episode_detection']:>10.1%}"
            f"{r['value_prevented']:>15.1%}{r['tx_recall']:>11.1%}"
        )
    return "\n".join(lines)
