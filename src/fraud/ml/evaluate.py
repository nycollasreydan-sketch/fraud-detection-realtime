"""Métricas de avaliação. PR-AUC é a principal (RESEARCH.md §2)."""

from __future__ import annotations

from typing import Any

import numpy as np
import numpy.typing as npt
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score

FloatArray = npt.NDArray[np.float64]
IntArray = npt.NDArray[np.int8]


def precision_recall_at(y: IntArray, scores: FloatArray, threshold: float) -> dict[str, float]:
    pred = scores >= threshold
    tp = int(np.sum(pred & (y == 1)))
    fp = int(np.sum(pred & (y == 0)))
    fn = int(np.sum(~pred & (y == 1)))
    n_neg = int(np.sum(y == 0))
    return {
        "threshold": float(threshold),
        "precision": tp / (tp + fp) if tp + fp else 0.0,
        "recall": tp / (tp + fn) if tp + fn else 0.0,
        "false_positive_rate": fp / n_neg if n_neg else 0.0,
        "alerts": float(tp + fp),
    }


def threshold_for_fpr(y: IntArray, scores: FloatArray, max_fpr: float) -> float:
    """Menor limiar cuja taxa de falso positivo nas legítimas não passa de `max_fpr`."""
    legit = scores[y == 0]
    if len(legit) == 0:
        return 0.5
    return float(np.quantile(legit, 1.0 - max_fpr, method="higher"))


def threshold_for_precision(
    y: IntArray, scores: FloatArray, min_precision: float, floor: float
) -> float:
    """Menor limiar >= `floor` em que a precision atinge `min_precision`."""
    order = np.argsort(-scores)
    s, yy = scores[order], y[order]
    tp = np.cumsum(yy == 1)
    precision = tp / np.arange(1, len(yy) + 1)
    ok = np.where((precision >= min_precision) & (s >= floor))[0]
    if len(ok) == 0:
        return float(max(floor, 0.99))
    return float(s[ok[-1]])


def bootstrap_pr_auc(
    y: IntArray, scores: FloatArray, n_boot: int = 200, seed: int = 0
) -> tuple[float, float]:
    """IC de 95% do PR-AUC por bootstrap. Com poucas fraudes no teste, a diferença entre
    dois modelos só significa algo se os intervalos não se sobrepõem."""
    rng = np.random.default_rng(seed)
    n = len(y)
    values = []
    for _ in range(n_boot):
        idx = rng.integers(0, n, n)
        if 0 < y[idx].sum() < n:
            values.append(average_precision_score(y[idx], scores[idx]))
    if not values:
        return float("nan"), float("nan")
    return float(np.percentile(values, 2.5)), float(np.percentile(values, 97.5))


def evaluate(
    y: IntArray, scores: FloatArray, review_threshold: float, block_threshold: float
) -> dict[str, Any]:
    has_both = 0 < int(y.sum()) < len(y)
    return {
        "n": len(y),
        "n_fraud": int(y.sum()),
        "fraud_rate": float(y.mean()) if len(y) else 0.0,
        "pr_auc": float(average_precision_score(y, scores)) if has_both else float("nan"),
        "roc_auc": float(roc_auc_score(y, scores)) if has_both else float("nan"),
        "brier": float(brier_score_loss(y, np.clip(scores, 0, 1))) if len(y) else float("nan"),
        "review": precision_recall_at(y, scores, review_threshold),
        "block": precision_recall_at(y, scores, block_threshold),
    }
