"""Modelo de fraude: definição do artefato, treino e inferência.

Modelo proposto (RESEARCH.md §6, ADR 006):

    features de estado ──┬──────────────────────────► LightGBM ─► margem ─► Platt ─► p(fraude)
                         └─► Isolation Forest ─► anomaly_score ─┘            │
                             (treinada só em legítimas)                      └─► TreeSHAP

O mesmo objeto `FraudModel` cobre as variantes da ablação: com ou sem o score de anomalia e
com qualquer subconjunto de features.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import joblib
import lightgbm as lgb
import numpy as np
import numpy.typing as npt
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import IsolationForest
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from fraud.domain import Decision
from fraud.features.compute import ALL_FEATURES, FEATURE_DESCRIPTIONS
from fraud.ml import drift
from fraud.ml.dataset import Dataset
from fraud.ml.evaluate import threshold_for_fpr, threshold_for_precision
from fraud.ml.fast_iforest import FastIsolationScorer

FloatArray = npt.NDArray[np.float64]

ANOMALY_FEATURE = "anomaly_score"
ANOMALY_DESCRIPTION = "Score de anomalia da Isolation Forest (maior = mais atípica)"
MODEL_FILE = "model.joblib"
METADATA_FILE = "metadata.json"


@dataclass(frozen=True)
class TrainParams:
    use_anomaly: bool = True
    review_fpr: float = 0.003  # orçamento: no máx. 0,3% das legítimas viram alerta
    block_precision: float = 0.95  # bloqueio automático só com precision >= 95% na validação
    learning_rate: float = 0.05
    num_leaves: int = 31
    min_child_samples: int = 40
    max_rounds: int = 600
    early_stopping_rounds: int = 50
    iforest_trees: int = 100
    seed: int = 13


@dataclass
class Contribution:
    feature: str
    value: float
    contribution: float  # em log-odds; positivo empurra para fraude
    description: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Scored:
    score: float
    decision: Decision
    anomaly_score: float | None
    contributions: list[Contribution]


@dataclass
class FraudModel:
    version: str
    feature_version: int
    features: list[str]  # features de entrada, na ordem da matriz
    categorical: list[str]
    booster: lgb.Booster
    iforest: IsolationForest | None
    platt_a: float
    platt_b: float
    review_threshold: float
    block_threshold: float
    score_edges: list[float] = field(default_factory=list)
    score_expected: list[float] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)
    _fast_iforest: FastIsolationScorer | None = field(default=None, repr=False, compare=False)

    # ------------------------------------------------------------------ inferência
    @property
    def model_features(self) -> list[str]:
        return [*self.features, ANOMALY_FEATURE] if self.iforest is not None else self.features

    def _matrix(self, X: FloatArray) -> FloatArray:
        if self.iforest is None:
            return X
        anomaly = -self.iforest.score_samples(X)
        return np.column_stack([X, anomaly])

    def _calibrate(self, margin: FloatArray) -> FloatArray:
        z = np.clip(self.platt_a * margin + self.platt_b, -40, 40)
        return np.asarray(1.0 / (1.0 + np.exp(-z)), dtype=np.float64)

    def predict_proba(self, X: pd.DataFrame) -> FloatArray:
        """Probabilidade calibrada de fraude para um lote."""
        matrix = self._matrix(X[self.features].to_numpy(dtype=np.float64))
        margin = np.asarray(self.booster.predict(matrix, raw_score=True), dtype=np.float64)
        return self._calibrate(margin)

    def decide(self, score: float) -> Decision:
        if score >= self.block_threshold:
            return Decision.BLOCK
        if score >= self.review_threshold:
            return Decision.REVIEW
        return Decision.APPROVE

    def score_one(self, feats: dict[str, float], top_k: int = 5) -> Scored:
        """Score, decisão e explicação de uma transação (caminho online).

        Uma única chamada com `pred_contrib=True` devolve as contribuições TreeSHAP; a soma
        delas é a margem do modelo, então não há uma segunda predição.
        """
        values = [feats[name] for name in self.features]
        if self.iforest is not None:
            if self._fast_iforest is None:
                self._fast_iforest = FastIsolationScorer(self.iforest)
            values.append(self._fast_iforest.anomaly_score(values))
        matrix = np.array([values], dtype=np.float64)
        contrib = np.asarray(self.booster.predict(matrix, pred_contrib=True), dtype=np.float64)[0]
        margin = float(contrib.sum())
        score = float(self._calibrate(np.array([margin]))[0])
        names = self.model_features
        order = np.argsort(-np.abs(contrib[:-1]))[:top_k]
        contributions = [
            Contribution(
                feature=names[i],
                value=float(matrix[0, i]),
                contribution=float(contrib[i]),
                description=FEATURE_DESCRIPTIONS.get(names[i], ANOMALY_DESCRIPTION),
            )
            for i in order
        ]
        anomaly = float(matrix[0, -1]) if self.iforest is not None else None
        return Scored(score, self.decide(score), anomaly, contributions)

    # ------------------------------------------------------------------ persistência
    def metadata(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "feature_version": self.feature_version,
            "features": self.model_features,
            "uses_anomaly_score": self.iforest is not None,
            "review_threshold": self.review_threshold,
            "block_threshold": self.block_threshold,
            "calibration": {"method": "platt", "a": self.platt_a, "b": self.platt_b},
            "score_reference": {"edges": self.score_edges, "expected": self.score_expected},
            "params": self.params,
            "metrics": self.metrics,
        }

    def save(self, directory: str | Path) -> Path:
        path = Path(directory)
        path.mkdir(parents=True, exist_ok=True)
        self._fast_iforest = None  # derivado da floresta; reconstruído sob demanda
        joblib.dump(self, path / MODEL_FILE)
        (path / METADATA_FILE).write_text(json.dumps(self.metadata(), indent=2, default=_json))
        return path

    @staticmethod
    def load(directory: str | Path) -> FraudModel:
        model = joblib.load(Path(directory) / MODEL_FILE)
        if not isinstance(model, FraudModel):
            raise TypeError(f"artefato inesperado em {directory}: {type(model)!r}")
        # Artefato treinado com outra versão do código de features: sem esta checagem o
        # worker subiria "saudável" e mandaria toda transação para a DLQ com KeyError.
        unknown = sorted(set(model.features) - set(ALL_FEATURES))
        if unknown:
            raise ValueError(
                f"modelo {model.version} pede features que este código não calcula: {unknown}; "
                "retreine (`make train`) ou volte o código para a versão do artefato"
            )
        return model


def _json(value: Any) -> Any:
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, float) and math.isnan(value):
        return None
    raise TypeError(type(value))


def _version(feature_version: int, features: list[str], params: TrainParams, n_train: int) -> str:
    payload = json.dumps([features, asdict(params), n_train], sort_keys=True).encode()
    kind = "hybrid" if params.use_anomaly else "gbm"
    return f"{kind}-f{feature_version}-{hashlib.sha256(payload).hexdigest()[:8]}"


def train_model(
    train: Dataset,
    valid: Dataset,
    features: list[str],
    categorical: list[str],
    feature_version: int,
    params: TrainParams | None = None,
) -> FraudModel:
    """Treina o LightGBM (com ou sem score de anomalia), calibra e define limiares.

    `train` ajusta as árvores; `valid` (posterior no tempo) faz early stopping, calibração
    e escolha de limiares. O conjunto de teste nunca passa por aqui.
    """
    p = params or TrainParams()
    x_train = train.X[features].to_numpy(dtype=np.float64)
    x_valid = valid.X[features].to_numpy(dtype=np.float64)
    names = list(features)

    iforest: IsolationForest | None = None
    if p.use_anomaly:
        iforest = IsolationForest(
            n_estimators=p.iforest_trees, max_samples=256, random_state=p.seed, n_jobs=1
        )
        iforest.fit(x_train[train.y == 0])
        x_train = np.column_stack([x_train, -iforest.score_samples(x_train)])
        x_valid = np.column_stack([x_valid, -iforest.score_samples(x_valid)])
        names = [*names, ANOMALY_FEATURE]

    n_pos = max(int(train.y.sum()), 1)
    n_neg = max(len(train.y) - n_pos, 1)
    lgb_params: dict[str, Any] = {
        "objective": "binary",
        "metric": "average_precision",
        "learning_rate": p.learning_rate,
        "num_leaves": p.num_leaves,
        "min_child_samples": p.min_child_samples,
        "feature_fraction": 0.8,
        "bagging_fraction": 0.8,
        "bagging_freq": 1,
        "lambda_l2": 1.0,
        # peso moderado: raiz da razão de desbalanceamento, não a razão inteira
        "scale_pos_weight": math.sqrt(n_neg / n_pos),
        "seed": p.seed,
        "deterministic": True,
        "force_row_wise": True,
        "num_threads": 2,
        "verbose": -1,
    }
    cats = [c for c in categorical if c in names]
    dtrain = lgb.Dataset(x_train, label=train.y, feature_name=names, categorical_feature=cats)
    dvalid = lgb.Dataset(x_valid, label=valid.y, reference=dtrain)
    booster = lgb.train(
        lgb_params,
        dtrain,
        num_boost_round=p.max_rounds,
        valid_sets=[dvalid],
        callbacks=[lgb.early_stopping(p.early_stopping_rounds, verbose=False)],
    )

    margin = np.asarray(booster.predict(x_valid, raw_score=True), dtype=np.float64)
    platt = LogisticRegression(C=1e6, solver="lbfgs", max_iter=1000)
    platt.fit(margin.reshape(-1, 1), valid.y)
    a, b = float(platt.coef_[0, 0]), float(platt.intercept_[0])
    if a <= 0:  # calibração degenerada (validação sem sinal): mantém a ordenação
        a, b = 1.0, 0.0

    model = FraudModel(
        version=_version(feature_version, names, p, len(train)),
        feature_version=feature_version,
        features=list(features),
        categorical=cats,
        booster=booster,
        iforest=iforest,
        platt_a=a,
        platt_b=b,
        review_threshold=0.5,
        block_threshold=0.9,
        params={**asdict(p), "best_iteration": booster.best_iteration},
    )
    scores = model._calibrate(margin)
    model.review_threshold = threshold_for_fpr(valid.y, scores, p.review_fpr)
    model.block_threshold = threshold_for_precision(
        valid.y, scores, p.block_precision, floor=model.review_threshold
    )
    model.score_edges = drift.reference_edges(scores)
    model.score_expected = [float(v) for v in drift.bin_fractions(list(scores), model.score_edges)]
    return model


def train_logreg(train: Dataset, features: list[str], categorical: list[str]) -> Pipeline:
    """Baseline: regressão logística com one-hot e padronização, pesos balanceados."""
    numeric = [f for f in features if f not in categorical]
    cats = [f for f in features if f in categorical]
    pipe = Pipeline(
        [
            (
                "prep",
                ColumnTransformer(
                    [
                        ("num", StandardScaler(), numeric),
                        ("cat", OneHotEncoder(handle_unknown="ignore"), cats),
                    ]
                ),
            ),
            ("clf", LogisticRegression(class_weight="balanced", max_iter=2000, C=1.0)),
        ]
    )
    pipe.fit(train.X[features], train.y)
    return pipe
