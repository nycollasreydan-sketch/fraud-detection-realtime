"""Fixtures compartilhadas. O modelo "rápido" é treinado uma vez por sessão de testes."""

from __future__ import annotations

from pathlib import Path

import pytest

from fraud.domain import TxEvent
from fraud.ml.model import FraudModel
from fraud.ml.train import TrainConfig, run

BASE_TS = 1_767_571_200.0  # 2026-01-05 00:00 UTC


@pytest.fixture(scope="session")
def model_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("model")
    run(TrainConfig.quick(), out, report_path=out / "MODEL_REPORT.md")
    return out


@pytest.fixture(scope="session")
def model(model_dir: Path) -> FraudModel:
    return FraudModel.load(model_dir)


def make_tx(
    tx_id: str = "t1",
    *,
    customer: str = "c1",
    counterparty: str = "m1",
    amount: float = 100.0,
    channel: str = "ecommerce",
    category: str = "grocery",
    device: str = "d1",
    lat: float = -23.55,
    lon: float = -46.63,
    ts: float = BASE_TS,
) -> TxEvent:
    return TxEvent(tx_id, customer, counterparty, amount, channel, category, device, lat, lon, ts)
