"""Caminho rápido de uma linha para a Isolation Forest.

`IsolationForest.score_samples` do scikit-learn é ótimo em lote, mas para UMA linha paga
validação de entrada e orquestração de threads: ~10 ms medidos, 90% da latência de
inferência. Percorrer as árvores direto nos arrays dá o mesmo número em uma fração do tempo.
A equivalência com o scikit-learn é garantida por teste
(tests/unit/test_ml.py::test_fast_iforest_matches_sklearn).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

import numpy as np

EULER_GAMMA = 0.5772156649015329


def average_path_length(n: float) -> float:
    """c(n): comprimento médio de busca malsucedida em uma BST com n pontos."""
    if n <= 1:
        return 0.0
    if n == 2:
        return 1.0
    return 2.0 * (math.log(n - 1.0) + EULER_GAMMA) - 2.0 * (n - 1.0) / n


class FastIsolationScorer:
    """Extrai as árvores de uma IsolationForest treinada para listas Python."""

    def __init__(self, forest: Any) -> None:
        self._trees: list[tuple[list[int], list[int], list[int], list[float], list[float]]] = []
        for estimator, columns in zip(forest.estimators_, forest.estimators_features_, strict=True):
            tree = estimator.tree_
            feature = [int(columns[f]) if f >= 0 else -1 for f in tree.feature]
            leaf_adjust = [average_path_length(float(n)) for n in tree.n_node_samples]
            self._trees.append(
                (
                    tree.children_left.tolist(),
                    tree.children_right.tolist(),
                    feature,
                    tree.threshold.tolist(),
                    leaf_adjust,
                )
            )
        self._denominator = len(self._trees) * average_path_length(float(forest.max_samples_))

    def anomaly_score(self, row: Sequence[float]) -> float:
        """Equivale a `-forest.score_samples([row])[0]`: em (0, 1), maior = mais anômalo."""
        # as árvores do scikit-learn comparam em float32
        x = np.asarray(row, dtype=np.float32).tolist()
        total = 0.0
        for left, right, feature, threshold, leaf_adjust in self._trees:
            node = 0
            depth = 0
            while left[node] != -1:
                node = left[node] if x[feature[node]] <= threshold[node] else right[node]
                depth += 1
            total += depth + leaf_adjust[node]
        return float(2.0 ** (-total / self._denominator))
