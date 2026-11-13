"""Benchmark em dados reais: dataset de cartões da ULB (Kaggle `mlg-ulb/creditcardfraud`).

    python -m fraud.ml.benchmark_kaggle --data data/creditcard.csv --report docs/BENCHMARK_KAGGLE.md

O dataset não tem cliente nem recebedor (as colunas V1..V28 são componentes PCA anônimas),
então não serve para as features comportamentais. Serve para uma pergunta específica: a
parte do modelo que independe do simulador (boosting calibrado, com e sem o score de
anomalia empilhado) se sustenta em dados reais? (ADR 003)
"""

from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from fraud.ml.dataset import Dataset, temporal_split
from fraud.ml.evaluate import bootstrap_pr_auc, evaluate, threshold_for_fpr, threshold_for_precision
from fraud.ml.model import TrainParams, train_logreg, train_model
from fraud.ml.report import _n

SEEDS = (13, 14, 15)


def load(path: str | Path) -> Dataset:
    df = pd.read_csv(path)
    X = df.drop(columns=["Class", "Time"]).astype(np.float64)
    X["hour"] = (df["Time"] % 86400) / 3600.0
    meta = pd.DataFrame({"ts": df["Time"].astype(np.float64)})
    return Dataset(X, df["Class"].to_numpy(dtype=np.int8), meta)


def run(path: str | Path) -> dict[str, Any]:
    ds = load(path)
    train, valid, test = temporal_split(ds)
    features = list(ds.X.columns)
    base = TrainParams()
    rows: list[dict[str, Any]] = []

    logreg = train_logreg(train, features, [])
    valid_scores = logreg.predict_proba(valid.X)[:, 1]
    scores = logreg.predict_proba(test.X)[:, 1]
    review = threshold_for_fpr(valid.y, valid_scores, base.review_fpr)
    block = threshold_for_precision(valid.y, valid_scores, base.block_precision, review)
    rows.append(
        {
            "model": "Regressão logística (baseline)",
            **evaluate(test.y, scores, review, block),
            "pr_auc_ci": bootstrap_pr_auc(test.y, scores),
            "pr_auc_seeds": [],
        }
    )
    for name, use_anomaly in (
        ("LightGBM calibrado", False),
        ("Híbrido: + score de anomalia", True),
    ):
        per_seed = []
        first: dict[str, Any] | None = None
        for seed in SEEDS:
            params = replace(base, use_anomaly=use_anomaly, seed=seed)
            model = train_model(train, valid, features, [], 0, params)
            scores = model.predict_proba(test.X)
            result = evaluate(test.y, scores, model.review_threshold, model.block_threshold)
            per_seed.append(result["pr_auc"])
            if first is None:
                first = {**result, "pr_auc_ci": bootstrap_pr_auc(test.y, scores)}
        assert first is not None
        rows.append({"model": name, **first, "pr_auc_seeds": per_seed})
    return {
        "n_train": len(train),
        "n_valid": len(valid),
        "n_test": len(test),
        "n_fraud_test": int(test.y.sum()),
        "fraud_rate": float(ds.y.mean()),
        "rows": rows,
    }


def markdown(result: dict[str, Any]) -> str:
    lines = [
        "# Benchmark em dados reais (ULB / Kaggle)",
        "",
        "> Gerado por `make benchmark`. Não edite à mão.",
        "",
        f"- {_n(result['n_train'])} transações de treino, {_n(result['n_valid'])} de validação e "
        f"{_n(result['n_test'])} de teste, em blocos temporais consecutivos (nunca split aleatório)",
        f"- Taxa de fraude: {result['fraud_rate']:.3%}; apenas {result['n_fraud_test']} fraudes "
        "no bloco de teste, por isso os intervalos são largos",
        "- Mesmo código de treino do projeto (`train_model`): peso de classe moderado, "
        "calibração de Platt e limiar pelo orçamento de 0,3% de falso positivo",
        "",
        "| Modelo | PR-AUC | IC 95% (bootstrap) | PR-AUC por semente | ROC-AUC | Precision | Recall |",
        "|---|---:|---:|---|---:|---:|---:|",
    ]
    for r in result["rows"]:
        seeds = ", ".join(f"{v:.3f}" for v in r["pr_auc_seeds"]) or "n/d"
        lines.append(
            f"| {r['model']} | {r['pr_auc']:.4f} | {r['pr_auc_ci'][0]:.3f} a {r['pr_auc_ci'][1]:.3f} "
            f"| {seeds} | {r['roc_auc']:.4f} | {r['review']['precision']:.1%} "
            f"| {r['review']['recall']:.1%} |"
        )
    lines += ["", "Precision e recall no limiar de revisão. Leitura dos números em "
              "[ADR 006](decisions/006-modelo-hibrido.md).", ""]  # fmt: skip
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", default="data/creditcard.csv")
    parser.add_argument("--report", default="docs/BENCHMARK_KAGGLE.md")
    args = parser.parse_args()
    if not Path(args.data).exists():
        raise SystemExit(
            f"{args.data} não encontrado. Baixe de "
            "https://www.kaggle.com/datasets/mlg-ulb/creditcardfraud e coloque em data/."
        )
    result = run(args.data)
    text = markdown(result)
    Path(args.report).write_text(text)
    print(text)


if __name__ == "__main__":
    main()
