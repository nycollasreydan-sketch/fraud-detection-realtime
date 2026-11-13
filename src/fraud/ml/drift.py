"""Population Stability Index para monitorar drift sem rótulo (RESEARCH.md §4)."""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence

import numpy as np
import numpy.typing as npt

EPS = 1e-4


def reference_edges(scores: npt.NDArray[np.float64], bins: int = 10) -> list[float]:
    """Bordas internas dos bins (quantis) da distribuição de referência."""
    qs = np.quantile(scores, np.linspace(0, 1, bins + 1)[1:-1])
    return [float(q) for q in np.unique(qs)]


def bin_fractions(scores: Sequence[float], edges: Sequence[float]) -> npt.NDArray[np.float64]:
    idx = np.searchsorted(np.asarray(edges), np.asarray(scores), side="right")
    counts = np.bincount(idx, minlength=len(edges) + 1).astype(np.float64)
    total = counts.sum()
    return counts / total if total else counts


def psi(expected: Sequence[float], actual: Sequence[float]) -> float:
    """PSI = Σ (a - e) · ln(a / e). Regra de bolso: <0,1 estável; >0,25 mudança relevante."""
    e = np.clip(np.asarray(expected, dtype=np.float64), EPS, None)
    a = np.clip(np.asarray(actual, dtype=np.float64), EPS, None)
    return float(np.sum((a - e) * np.log(a / e)))


class ScoreDriftMonitor:
    """Janela deslizante de scores em produção comparada com a referência do treino."""

    def __init__(
        self,
        edges: Sequence[float],
        expected: Sequence[float],
        window: int = 2000,
        min_samples: int = 200,
    ) -> None:
        self.edges = list(edges)
        self.expected = list(expected)
        self.min_samples = min_samples
        self._scores: deque[float] = deque(maxlen=window)

    def observe(self, score: float) -> None:
        self._scores.append(score)

    def value(self) -> float | None:
        if len(self._scores) < self.min_samples or not self.edges:
            return None
        return psi(self.expected, bin_fractions(list(self._scores), self.edges).tolist())
