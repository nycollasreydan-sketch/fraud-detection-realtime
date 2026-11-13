"""Teste de fumaça do painel: renderiza a página inteira com a API simulada."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from streamlit.testing.v1 import AppTest

from fraud.ml.model import FraudModel

APP = str(Path(__file__).resolve().parents[2] / "src/fraud/dashboard/app.py")

ALERT = {
    "id": "a1",
    "transaction_id": "t1",
    "customer_id": "c1",
    "counterparty_id": "acct9",
    "amount": "950.00",
    "score": 0.93,
    "severity": "high",
    "status": "open",
    "model_version": "v",
    "created_at": "2026-01-05T12:00:00Z",
    "reasons": [
        {
            "feature": "cp_new_senders_24h",
            "value": 12.0,
            "contribution": 2.1,
            "description": "Fan-in",
        },
        {"feature": "c_amount_z", "value": 3.2, "contribution": 0.8, "description": "Z-score"},
    ],
}
STATS = {
    "window_minutes": 60,
    "transactions": {"scored": 1200, "pending": 3},
    "decisions": {"approve": 1180, "block": 20},
    "alerts": {"high": 20},
    "decision_latency_ms": {"p50": 12.0, "p95": 40.0, "p99": 90.0},
    "queue": {"length": 3, "dead_letter": 1},
}


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    import streamlit as st

    st.cache_data.clear()  # o painel guarda respostas da API por 5 s


def fake_api(model: FraudModel, alerts: list[dict[str, Any]]) -> Any:
    def get(
        url: str, params: dict[str, Any] | None = None, timeout: float = 10.0
    ) -> httpx.Response:
        path = url.split("//", 1)[1].split("/", 1)[1]
        body: Any
        if path == "stats":
            body = STATS
        elif path == "alerts":
            body = {"items": alerts, "total": len(alerts), "limit": 100, "offset": 0}
        elif path == "model/info":
            body = model.metadata()
        else:
            return httpx.Response(404, request=httpx.Request("GET", url))
        return httpx.Response(200, json=body, request=httpx.Request("GET", url))

    return get


@pytest.mark.parametrize("alerts", [[ALERT], []], ids=["com-alertas", "sem-alertas"])
def test_dashboard_renders(
    model: FraudModel, monkeypatch: pytest.MonkeyPatch, alerts: list[dict[str, Any]]
) -> None:
    monkeypatch.setattr(httpx, "get", fake_api(model, alerts))
    app = AppTest.from_file(APP, default_timeout=30).run()
    assert not app.exception
    assert app.title[0].value == "Detecção de fraude em tempo real"
    metrics = {m.label: m.value for m in app.metric}
    assert metrics["Transações"] == "1.203"
    assert metrics["Dead-letter"] == "1"
    assert "PR-AUC" in metrics
    assert bool(app.info) is (not alerts)


def test_dashboard_reports_api_down(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_: Any, **__: Any) -> httpx.Response:
        raise httpx.ConnectError("sem API")

    monkeypatch.setattr(httpx, "get", boom)
    app = AppTest.from_file(APP, default_timeout=30).run()
    assert not app.exception
    assert "Não consegui falar com a API" in app.error[0].value
