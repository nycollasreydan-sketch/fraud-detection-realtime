from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from sklearn.ensemble import IsolationForest

from fraud.domain import Decision
from fraud.features.compute import ALL_FEATURES, compute_features
from fraud.features.state import CounterpartyState, CustomerState
from fraud.ml import drift
from fraud.ml.dataset import ReplayEngine, build_dataset, temporal_split
from fraud.ml.evaluate import (
    bootstrap_pr_auc,
    evaluate,
    precision_recall_at,
    threshold_for_fpr,
    threshold_for_precision,
)
from fraud.ml.fast_iforest import FastIsolationScorer, average_path_length
from fraud.ml.model import ANOMALY_FEATURE, METADATA_FILE, FraudModel
from fraud.ml.report import feature_docs
from fraud.simulation.world import World
from tests.conftest import make_tx


# ---------------------------------------------------------------- isolation forest rápida
def test_fast_iforest_matches_sklearn() -> None:
    rng = np.random.default_rng(0)
    X = rng.normal(size=(3000, 9)) * rng.uniform(1, 500, 9)
    forest = IsolationForest(n_estimators=60, max_samples=256, random_state=1).fit(X)
    fast = FastIsolationScorer(forest)
    probe = rng.normal(size=(100, 9)) * rng.uniform(1, 1500, 9)
    ours = np.array([fast.anomaly_score(row) for row in probe])
    np.testing.assert_allclose(ours, -forest.score_samples(probe), rtol=0, atol=1e-12)


def test_average_path_length_edge_cases() -> None:
    assert average_path_length(1) == 0.0
    assert average_path_length(2) == 1.0
    assert average_path_length(256) == pytest.approx(10.2448, abs=1e-3)


# ---------------------------------------------------------------- métricas
def test_precision_recall_at_threshold() -> None:
    y = np.array([0, 0, 0, 1, 1], dtype=np.int8)
    scores = np.array([0.1, 0.2, 0.8, 0.7, 0.9])
    r = precision_recall_at(y, scores, 0.75)
    assert r["precision"] == 0.5
    assert r["recall"] == 0.5
    assert r["false_positive_rate"] == pytest.approx(1 / 3)


def test_threshold_for_fpr_respects_budget() -> None:
    rng = np.random.default_rng(1)
    y = np.zeros(10_000, dtype=np.int8)
    scores = rng.random(10_000)
    threshold = threshold_for_fpr(y, scores, 0.01)
    assert np.mean(scores >= threshold) <= 0.0101


def test_threshold_for_precision() -> None:
    y = np.array([1, 1, 1, 0, 1, 0, 0], dtype=np.int8)
    scores = np.array([0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3])
    assert threshold_for_precision(y, scores, 1.0, floor=0.0) == 0.7
    assert threshold_for_precision(y, scores, 0.8, floor=0.0) == 0.5
    assert threshold_for_precision(y, scores, 0.8, floor=0.65) == 0.7  # nunca abaixo do piso


def test_evaluate_and_bootstrap() -> None:
    rng = np.random.default_rng(2)
    y = (rng.random(4000) < 0.05).astype(np.int8)
    scores = np.clip(y * 0.6 + rng.random(4000) * 0.5, 0, 1)
    result = evaluate(y, scores, 0.5, 0.9)
    assert 0.5 < result["pr_auc"] <= 1.0
    low, high = bootstrap_pr_auc(y, scores, n_boot=50)
    assert low <= result["pr_auc"] <= high


# ---------------------------------------------------------------- drift
def test_psi_is_zero_for_same_distribution_and_grows_with_shift() -> None:
    rng = np.random.default_rng(3)
    reference = rng.beta(1, 20, 20_000)
    edges = drift.reference_edges(reference)
    expected = drift.bin_fractions(list(reference), edges)
    same = drift.bin_fractions(list(rng.beta(1, 20, 20_000)), edges)
    shifted = drift.bin_fractions(list(rng.beta(3, 10, 20_000)), edges)
    assert drift.psi(expected, same) < 0.01
    assert drift.psi(expected, shifted) > 0.25


def test_drift_monitor_waits_for_enough_samples() -> None:
    monitor = drift.ScoreDriftMonitor([0.5], [0.5, 0.5], window=100, min_samples=10)
    for _ in range(9):
        monitor.observe(0.9)
    assert monitor.value() is None
    monitor.observe(0.9)
    value = monitor.value()
    assert value is not None
    assert value > 0.25


# ---------------------------------------------------------------- dataset
def test_temporal_split_never_mixes_past_and_future() -> None:
    world = World(n_customers=40, n_merchants=30, seed=1)
    ds = build_dataset(world.generate(10, seed=2))
    train, valid, test = temporal_split(ds)
    assert len(train) + len(valid) + len(test) == len(ds)
    assert train.meta["ts"].max() <= valid.meta["ts"].min()
    assert valid.meta["ts"].max() <= test.meta["ts"].min()
    assert list(ds.X.columns) == ALL_FEATURES


def test_replay_engine_matches_manual_state_handling() -> None:
    """Paridade treino/serviço: o replay offline e o caminho "ler estado, calcular,
    gravar estado" dão exatamente as mesmas features, mesmo com o estado passando por JSON
    a cada passo, como acontece com o Redis."""
    world = World(n_customers=15, n_merchants=20, seed=3)
    stream = world.generate(6, seed=4)[:400]
    engine = ReplayEngine()
    customers: dict[str, str] = {}
    counterparties: dict[str, str] = {}
    from fraud.features.compute import apply_transaction

    for item in stream:
        tx = item.event
        offline = engine.step(item)
        cs = CustomerState.from_dict(json.loads(customers.get(tx.customer_id, "{}")))
        cps = CounterpartyState.from_dict(json.loads(counterparties.get(tx.counterparty_id, "{}")))
        online = compute_features(tx, cs, cps)
        apply_transaction(tx, cs, cps)
        customers[tx.customer_id] = json.dumps(cs.to_dict())
        counterparties[tx.counterparty_id] = json.dumps(cps.to_dict())
        assert online == offline


# ---------------------------------------------------------------- modelo
def test_model_artifact_metadata(model: FraudModel, model_dir: Path) -> None:
    meta = json.loads((model_dir / METADATA_FILE).read_text())
    assert meta["version"] == model.version
    assert meta["features"][-1] == ANOMALY_FEATURE
    assert 0 < meta["review_threshold"] <= meta["block_threshold"] <= 1
    assert len(meta["metrics"]["ablation"]) == 4
    assert (model_dir / "MODEL_REPORT.md").read_text().startswith("# Relatório do modelo")


def test_model_beats_baseline_on_held_out_future(model: FraudModel) -> None:
    ablation = {row["model"][:1]: row["pr_auc"] for row in model.metrics["ablation"]}
    assert ablation["4"] > ablation["1"]  # proposto > regressão logística
    assert ablation["3"] > ablation["2"] + 0.2  # features comportamentais são o grande salto
    assert model.metrics["test"]["pr_auc"] > 0.6


def test_online_and_batch_scores_agree(model: FraudModel) -> None:
    """`score_one` (caminho do worker, com a Isolation Forest rápida) tem de dar o mesmo
    número que `predict_proba` (caminho do treino e da avaliação)."""
    world = World(n_customers=30, n_merchants=30, seed=5)
    ds = build_dataset(world.generate(8, seed=6))
    batch = model.predict_proba(ds.X.head(200))
    for i in range(0, 200, 7):
        scored = model.score_one(ds.X.iloc[i].to_dict())
        assert scored.score == pytest.approx(batch[i], abs=1e-9)
        assert 0.0 <= scored.score <= 1.0


def test_explanation_is_ranked_and_consistent(model: FraudModel) -> None:
    feats = compute_features(make_tx(amount=5000.0), CustomerState(), CounterpartyState())
    scored = model.score_one(feats, top_k=5)
    magnitudes = [abs(c.contribution) for c in scored.contributions]
    assert len(scored.contributions) == 5
    assert magnitudes == sorted(magnitudes, reverse=True)
    assert all(c.feature in model.model_features and c.description for c in scored.contributions)
    assert scored.anomaly_score is not None
    assert 0 < scored.anomaly_score < 1


def test_decision_thresholds(model: FraudModel) -> None:
    assert model.decide(0.0) is Decision.APPROVE
    assert model.decide(model.review_threshold) is Decision.REVIEW
    assert model.decide(1.0) is Decision.BLOCK


def test_save_and_load_roundtrip(model: FraudModel, tmp_path: Path) -> None:
    feats = compute_features(make_tx(), CustomerState(), CounterpartyState())
    before = model.score_one(feats).score
    model.save(tmp_path)
    loaded = FraudModel.load(tmp_path)
    assert loaded.version == model.version
    assert loaded.score_one(feats).score == pytest.approx(before, abs=1e-12)


def test_load_rejects_unexpected_artifact(tmp_path: Path) -> None:
    import joblib

    joblib.dump({"not": "a model"}, tmp_path / "model.joblib")
    with pytest.raises(TypeError):
        FraudModel.load(tmp_path)


def test_feature_docs_list_every_feature() -> None:
    docs = feature_docs()
    assert all(f"`{name}`" in docs for name in ALL_FEATURES)
