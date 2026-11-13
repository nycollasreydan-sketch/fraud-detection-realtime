"""Script de treino: simula dados, monta features por replay, treina a ablação completa,
avalia no bloco de teste (futuro) e serializa o modelo proposto.

    python -m fraud.ml.train --out artifacts/model --report docs/MODEL_REPORT.md
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

import numpy as np

from fraud.features.compute import (
    FEATURE_SPECS,
    LATEST_VERSION,
    categorical_features,
    feature_names,
)
from fraud.ml.dataset import Dataset, build_dataset, drop_burn_in, temporal_split
from fraud.ml.evaluate import bootstrap_pr_auc, evaluate, threshold_for_fpr, threshold_for_precision
from fraud.ml.model import (
    METADATA_FILE,
    MODEL_FILE,
    FraudModel,
    TrainParams,
    train_logreg,
    train_model,
)
from fraud.simulation.attacks import ATTACKS, attack_stream
from fraud.simulation.world import LabeledTx, World


@dataclass(frozen=True)
class TrainConfig:
    n_customers: int = 1500
    n_merchants: int = 400
    days: float = 60.0
    # dias simulados antes do período de treino: constroem histórico e são descartados
    burn_in_days: float = 21.0
    world_seed: int = 7
    stream_seed: int = 11
    feature_version: int = LATEST_VERSION
    # treino adversarial (Fase D): episódios de cada ataque injetados no treino
    adversarial_episodes: int = 20
    adversarial_kinds: tuple[str, ...] = ATTACKS
    # roda a bateria de ataques ao fim do treino e guarda o resultado no artefato
    adversarial_eval: bool = True
    adversarial_stealth: tuple[float, float] = (0.0, 0.7)
    params: TrainParams = field(default_factory=TrainParams)

    @staticmethod
    def quick() -> TrainConfig:
        """Configuração pequena para testes e CI (segundos em vez de minutos)."""
        return TrainConfig(
            n_customers=400,
            n_merchants=150,
            days=30.0,
            burn_in_days=14.0,
            adversarial_episodes=8,
            adversarial_eval=False,
        )


def build_stream(cfg: TrainConfig) -> tuple[World, list[LabeledTx]]:
    world = World(n_customers=cfg.n_customers, n_merchants=cfg.n_merchants, seed=cfg.world_seed)
    total_days = cfg.burn_in_days + cfg.days
    stream = world.generate(total_days, cfg.stream_seed)
    if cfg.adversarial_episodes > 0:
        rng = np.random.default_rng(cfg.stream_seed + 1000)
        stream.extend(
            attack_stream(
                world,
                stream,
                cfg.adversarial_kinds,
                cfg.adversarial_episodes,
                rng,
                first_day=cfg.burn_in_days,
                last_day=total_days,
                stealth_range=cfg.adversarial_stealth,
            )
        )
        stream.sort(key=lambda x: x.event.ts)
    return world, stream


def build_training_dataset(cfg: TrainConfig) -> Dataset:
    """Simula, faz o replay completo e descarta o período de aquecimento."""
    world, stream = build_stream(cfg)
    ds = build_dataset(stream)
    return drop_burn_in(ds, world.start_ts, cfg.burn_in_days)


def _transaction_only_features() -> list[str]:
    return [s.name for s in FEATURE_SPECS if s.group == "transacao"]


def latency_benchmark(model: FraudModel, ds: Dataset, n: int = 300) -> dict[str, float]:
    """Latência de `score_one` (anomalia + boosting + TreeSHAP) em uma thread."""
    rows: list[dict[str, float]] = [
        {str(k): float(v) for k, v in row.items()} for row in ds.X.head(n).to_dict(orient="records")
    ]
    model.score_one(rows[0])  # aquece
    times = []
    for row in rows:
        t0 = time.perf_counter()
        model.score_one(row)
        times.append((time.perf_counter() - t0) * 1000)
    return {
        "p50_ms": float(np.percentile(times, 50)),
        "p95_ms": float(np.percentile(times, 95)),
        "p99_ms": float(np.percentile(times, 99)),
    }


def run_ablation(
    train: Dataset, valid: Dataset, test: Dataset, cfg: TrainConfig
) -> tuple[FraudModel, list[dict[str, Any]]]:
    """Treina os quatro degraus e devolve o modelo proposto e a tabela comparativa."""
    version = cfg.feature_version
    feats = feature_names(version)
    cats = categorical_features(version)
    tx_only = _transaction_only_features()
    p = cfg.params
    rows: list[dict[str, Any]] = []

    # 1. baseline: regressão logística
    logreg = train_logreg(train, feats, cats)
    valid_scores = logreg.predict_proba(valid.X[feats])[:, 1]
    test_scores = logreg.predict_proba(test.X[feats])[:, 1]
    review = threshold_for_fpr(valid.y, valid_scores, p.review_fpr)
    block = threshold_for_precision(valid.y, valid_scores, p.block_precision, review)
    rows.append(
        {
            "model": "1. Regressão logística (baseline)",
            **evaluate(test.y, test_scores, review, block),
            "pr_auc_ci": bootstrap_pr_auc(test.y, test_scores),
        }
    )

    # 2-4. variantes do boosting
    variants: list[tuple[str, list[str], bool]] = [
        ("2. LightGBM só com colunas da transação", tx_only, False),
        ("3. LightGBM + features comportamentais", feats, False),
        ("4. Híbrido: + score de anomalia (proposto)", feats, True),
    ]
    proposed: FraudModel | None = None
    for name, columns, use_anomaly in variants:
        params = TrainParams(**{**asdict(p), "use_anomaly": use_anomaly})
        model = train_model(train, valid, columns, cats, version, params)
        scores = model.predict_proba(test.X)
        rows.append(
            {
                "model": name,
                **evaluate(test.y, scores, model.review_threshold, model.block_threshold),
                "pr_auc_ci": bootstrap_pr_auc(test.y, scores),
            }
        )
        if use_anomaly == p.use_anomaly and columns is feats:
            proposed = model
    assert proposed is not None
    return proposed, rows


def per_scenario_recall(model: FraudModel, test: Dataset) -> dict[str, dict[str, float]]:
    scores = model.predict_proba(test.X)
    out: dict[str, dict[str, float]] = {}
    for scenario in sorted(test.meta["scenario"].unique()):
        mask = (test.meta["scenario"] == scenario).to_numpy()
        flagged = float(np.mean(scores[mask] >= model.review_threshold))
        key = "recall" if test.y[mask].any() else "false_positive_rate"
        out[str(scenario)] = {"n": float(mask.sum()), key: flagged}
    return out


def adversarial_summary(model: FraudModel) -> dict[str, Any]:
    """Bateria padrão de ataques (outra população, outras sementes) contra o modelo."""
    from fraud.ml.adversarial import AttackEvalConfig, build_attack_dataset, evaluate_attacks

    return evaluate_attacks(model, build_attack_dataset(AttackEvalConfig()))


def run(cfg: TrainConfig, out_dir: str | Path, report_path: str | Path | None = None) -> FraudModel:
    t0 = time.perf_counter()
    world, stream = build_stream(cfg)
    t_sim = time.perf_counter() - t0
    t0 = time.perf_counter()
    ds = drop_burn_in(build_dataset(stream), world.start_ts, cfg.burn_in_days)
    t_feat = time.perf_counter() - t0
    train, valid, test = temporal_split(ds)

    t0 = time.perf_counter()
    model, ablation = run_ablation(train, valid, test, cfg)
    t_train = time.perf_counter() - t0

    importance = sorted(
        zip(
            model.model_features,
            (float(v) for v in model.booster.feature_importance(importance_type="gain")),
            strict=True,
        ),
        key=lambda kv: -kv[1],
    )
    total_gain = sum(v for _, v in importance) or 1.0
    model.metrics = {
        "data": {
            "source": "simulador (fraud.simulation.world)",
            "config": {k: v for k, v in asdict(cfg).items() if k != "params"},
            "n_train": len(train),
            "n_valid": len(valid),
            "n_test": len(test),
            "fraud_rate": float(ds.y.mean()),
        },
        "test": ablation[-1] if cfg.params.use_anomaly else ablation[-2],
        "ablation": ablation,
        "per_scenario": per_scenario_recall(model, test),
        "feature_importance": [
            {"feature": k, "gain_share": v / total_gain} for k, v in importance[:15]
        ],
        "latency": latency_benchmark(model, test),
        "adversarial": adversarial_summary(model) if cfg.adversarial_eval else None,
        "timings_s": {"simulate": t_sim, "features": t_feat, "train": t_train},
    }
    model.save(out_dir)
    if report_path:
        from fraud.ml.report import write_report

        write_report(model, report_path)
    return model


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="artifacts/model")
    parser.add_argument("--report", default=None, help="caminho do relatório em Markdown")
    parser.add_argument("--quick", action="store_true", help="dataset pequeno (testes/CI)")
    parser.add_argument("--if-missing", action="store_true", help="não retreina se já existir")
    parser.add_argument("--feature-version", type=int, default=LATEST_VERSION)
    parser.add_argument("--no-adversarial", action="store_true", help="sem ataques no treino")
    parser.add_argument("--no-anomaly", action="store_true", help="sem o score de anomalia")
    args = parser.parse_args()

    out = Path(args.out)
    if args.if_missing and (out / MODEL_FILE).exists() and (out / METADATA_FILE).exists():
        print(f"modelo já existe em {out}; nada a fazer")
        return
    base = TrainConfig.quick() if args.quick else TrainConfig()
    cfg = replace(base, feature_version=args.feature_version)
    if args.no_adversarial:
        cfg = replace(cfg, adversarial_episodes=0)
    if args.no_anomaly:
        cfg = replace(cfg, params=replace(cfg.params, use_anomaly=False))
    model = run(cfg, out, args.report)
    summary = {
        "version": model.version,
        "test": {k: model.metrics["test"][k] for k in ("pr_auc", "roc_auc", "review", "block")},
        "latency": model.metrics["latency"],
        "timings_s": model.metrics["timings_s"],
    }
    print(json.dumps(summary, indent=2))
    for row in model.metrics["ablation"]:
        print(
            f"{row['model']:<48} PR-AUC={row['pr_auc']:.4f} recall@review={row['review']['recall']:.3f}"
        )


if __name__ == "__main__":
    main()
