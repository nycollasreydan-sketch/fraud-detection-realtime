"""Constrói a matriz de treino por replay: passa o fluxo de transações, em ordem de tempo,
pela mesma função de features usada no worker (ADR 005)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt
import pandas as pd

from fraud.features.compute import ALL_FEATURES, apply_transaction, compute_features
from fraud.features.state import DAY, CounterpartyState, CustomerState
from fraud.simulation.world import LabeledTx

FloatArray = npt.NDArray[np.float64]


@dataclass
class Dataset:
    X: pd.DataFrame  # todas as features, uma coluna por nome
    y: npt.NDArray[np.int8]
    meta: pd.DataFrame  # tx_id, ts, customer_id, counterparty_id, amount, scenario, episode

    def __len__(self) -> int:
        return len(self.y)

    def subset(self, mask: npt.NDArray[np.bool_]) -> Dataset:
        return Dataset(
            self.X.loc[mask].reset_index(drop=True),
            self.y[mask],
            self.meta.loc[mask].reset_index(drop=True),
        )


class ReplayEngine:
    """Mantém os estados em memória e devolve features transação a transação."""

    def __init__(self) -> None:
        self.customers: dict[str, CustomerState] = {}
        self.counterparties: dict[str, CounterpartyState] = {}

    def step(self, item: LabeledTx) -> dict[str, float]:
        tx = item.event
        cs = self.customers.get(tx.customer_id)
        if cs is None:
            cs = self.customers[tx.customer_id] = CustomerState()
        cps = self.counterparties.get(tx.counterparty_id)
        if cps is None:
            cps = self.counterparties[tx.counterparty_id] = CounterpartyState()
        feats = compute_features(tx, cs, cps)
        apply_transaction(tx, cs, cps)
        return feats


def build_dataset(stream: Iterable[LabeledTx], engine: ReplayEngine | None = None) -> Dataset:
    """`stream` precisa estar ordenado por tempo do evento."""
    engine = engine or ReplayEngine()
    rows: list[list[float]] = []
    labels: list[int] = []
    meta: list[tuple[str, float, str, str, float, str, str]] = []
    for item in stream:
        feats = engine.step(item)
        rows.append([feats[name] for name in ALL_FEATURES])
        labels.append(1 if item.is_fraud else 0)
        e = item.event
        meta.append(
            (e.tx_id, e.ts, e.customer_id, e.counterparty_id, e.amount, item.scenario, item.episode)
        )
    X = pd.DataFrame(np.asarray(rows, dtype=np.float64), columns=ALL_FEATURES)
    meta_df = pd.DataFrame(
        meta,
        columns=["tx_id", "ts", "customer_id", "counterparty_id", "amount", "scenario", "episode"],
    )
    return Dataset(X, np.asarray(labels, dtype=np.int8), meta_df)


def temporal_split(
    ds: Dataset, train_frac: float = 0.6, valid_frac: float = 0.2
) -> tuple[Dataset, Dataset, Dataset]:
    """Treino no passado, validação no meio, teste no futuro. Nunca aleatório."""
    ts = ds.meta["ts"].to_numpy()
    t_train = float(np.quantile(ts, train_frac))
    t_valid = float(np.quantile(ts, train_frac + valid_frac))
    train = ds.subset(ts < t_train)
    valid = ds.subset((ts >= t_train) & (ts < t_valid))
    test = ds.subset(ts >= t_valid)
    return train, valid, test


def drop_burn_in(ds: Dataset, start_ts: float, burn_in_days: float) -> Dataset:
    """Remove as linhas do período de aquecimento.

    O aquecimento passa pelo replay (constrói o histórico de clientes e recebedores), mas
    não entra em treino nem em avaliação: nele todo mundo é "cliente novo".
    """
    if burn_in_days <= 0:
        return ds
    return ds.subset((ds.meta["ts"] >= start_ts + burn_in_days * DAY).to_numpy())
