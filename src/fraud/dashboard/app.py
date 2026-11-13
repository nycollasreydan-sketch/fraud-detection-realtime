"""Painel mínimo (Streamlit): alertas recentes e métricas do modelo.

Só leitura e só pela API: o painel não conhece banco nem Redis, então não cria um segundo
caminho de acesso aos dados.
"""

from __future__ import annotations

import os
from typing import Any

import altair as alt
import httpx
import pandas as pd
import streamlit as st

API_URL = os.environ.get("FRAUD_API_URL", "http://localhost:8000")
# Paleta: dois polos para polaridade (aumenta/reduz o risco), um tom único para magnitude.
RISK_UP = "#d03b3b"
RISK_DOWN = "#2a78d6"
MAGNITUDE = "#2a78d6"
ATTACK_LABELS = {
    "structuring": "Fracionamento de valores",
    "gradual_drift": "Mudança gradual de comportamento",
    "off_hours_burst": "Burst em horário atípico",
    "mule_account": "Conta-laranja",
    "replay": "Replay de transação legítima",
}

st.set_page_config(page_title="Detecção de fraude", page_icon="🛡️", layout="wide")


@st.cache_data(ttl=5)
def get(path: str, **params: Any) -> Any:
    response = httpx.get(f"{API_URL}{path}", params=params, timeout=10.0)
    response.raise_for_status()
    return response.json()


def pct(value: float | None, digits: int = 1) -> str:
    return "n/d" if value is None else f"{value:.{digits}%}"


def kpis(window: int) -> None:
    stats = get("/stats", window_minutes=window)
    tx = stats["transactions"]
    total = sum(tx.values())
    alerts = sum(stats["alerts"].values())
    p95 = stats["decision_latency_ms"]["p95"]
    cols = st.columns(6)
    cols[0].metric("Transações", f"{total:,}".replace(",", "."))
    cols[1].metric("Alertas", f"{alerts:,}".replace(",", "."))
    cols[2].metric("Taxa de alerta", pct(alerts / total if total else None, 2))
    cols[3].metric(
        "Latência p95",
        "n/d" if p95 is None else (f"{p95:.0f} ms" if p95 < 2000 else f"{p95 / 1000:.1f} s"),
        help="Da entrada na API até a decisão gravada, na janela. Inclui espera na fila.",
    )
    cols[4].metric("Pendentes", tx.get("pending", 0))
    cols[5].metric("Dead-letter", stats["queue"]["dead_letter"])


def explanation_chart(reasons: list[dict[str, Any]]) -> alt.Chart:
    df = pd.DataFrame(reasons)
    df["efeito"] = df["contribution"].map(lambda v: "Aumenta o risco" if v > 0 else "Reduz o risco")
    df["rótulo"] = df["description"] + " = " + df["value"].map(lambda v: f"{v:.2f}")
    base = alt.Chart(df).encode(
        y=alt.Y("rótulo:N", sort=alt.EncodingSortField("contribution", op="max", order="descending"),
                title=None, axis=alt.Axis(labelLimit=420)),
        x=alt.X(
            "contribution:Q",
            title="Contribuição para o score (log-odds)",
            axis=alt.Axis(tickCount=8),
        ),
        tooltip=[
            alt.Tooltip("feature:N", title="Feature"),
            alt.Tooltip("value:Q", title="Valor", format=".3f"),
            alt.Tooltip("contribution:Q", title="Contribuição", format="+.3f"),
        ],
    )  # fmt: skip
    bars = base.mark_bar(cornerRadiusEnd=4, height=18).encode(
        color=alt.Color(
            "efeito:N",
            scale=alt.Scale(
                domain=["Aumenta o risco", "Reduz o risco"], range=[RISK_UP, RISK_DOWN]
            ),
            legend=alt.Legend(title=None, orient="top"),
        )
    )
    labels = base.mark_text(align="left", dx=4, color="#52514e").encode(
        text=alt.Text("contribution:Q", format="+.2f")
    )
    return (bars + labels).properties(height=alt.Step(34))


def alerts_section() -> None:
    st.subheader("Alertas recentes")
    c1, c2, c3 = st.columns([1, 1, 2])
    severity = c1.selectbox("Severidade", ["todas", "high", "medium"])
    min_score = c2.slider("Score mínimo", 0.0, 1.0, 0.0, 0.05)
    customer = c3.text_input("Cliente (id exato)", "")
    params: dict[str, Any] = {"limit": 100, "min_score": min_score}
    if severity != "todas":
        params["severity"] = severity
    if customer.strip():
        params["customer_id"] = customer.strip()
    page = get("/alerts", **params)
    if not page["items"]:
        st.info("Nenhum alerta com esses filtros. Rode `make demo` para gerar tráfego.")
        return
    df = pd.DataFrame(page["items"])
    df["principal motivo"] = df["reasons"].map(lambda r: r[0]["description"] if r else "")
    df["amount"] = df["amount"].astype(float)
    df["created_at"] = pd.to_datetime(df["created_at"])
    table = df[
        ["created_at", "severity", "score", "amount", "customer_id", "counterparty_id", "principal motivo"]
    ].rename(
        columns={
            "created_at": "quando", "severity": "severidade", "amount": "valor (R$)",
            "customer_id": "cliente", "counterparty_id": "recebedor",
        }
    )  # fmt: skip
    st.caption(f"{page['total']} alertas no total; mostrando os {len(df)} mais recentes. "
               "Selecione uma linha para ver a explicação.")  # fmt: skip
    event = st.dataframe(
        table,
        hide_index=True,
        width="stretch",
        on_select="rerun",
        selection_mode="single-row",
        column_config={
            "score": st.column_config.ProgressColumn(
                "score", min_value=0, max_value=1, format="%.3f"
            ),
            "valor (R$)": st.column_config.NumberColumn(format="%.2f"),
            "quando": st.column_config.DatetimeColumn(format="DD/MM HH:mm:ss"),
        },
    )
    rows = event.selection.rows if event and event.selection else []
    alert = page["items"][rows[0] if rows else 0]
    st.markdown(
        f"**Por que o alerta de R$ {float(alert['amount']):.2f} do cliente "
        f"`{alert['customer_id']}` (score {alert['score']:.3f})**"
    )
    st.altair_chart(explanation_chart(alert["reasons"]), width="stretch")


def model_section() -> None:
    st.subheader("Modelo")
    try:
        info = get("/model/info")
    except httpx.HTTPStatusError:
        st.warning("Nenhum modelo treinado encontrado.")
        return
    m = info["metrics"]
    test = m["test"]
    st.caption(
        f"Versão `{info['version']}` · {len(info['features'])} features · limiar de revisão "
        f"{info['review_threshold']:.4f} · limiar de bloqueio {info['block_threshold']:.4f} · "
        f"avaliado em {test['n']:,} transações posteriores ao treino".replace(",", ".")
    )
    cols = st.columns(5)
    cols[0].metric("PR-AUC", f"{test['pr_auc']:.3f}")
    cols[1].metric("Precision (revisão)", pct(test["review"]["precision"]))
    cols[2].metric("Recall (revisão)", pct(test["review"]["recall"]))
    cols[3].metric("Falso positivo", pct(test["review"]["false_positive_rate"], 2))
    cols[4].metric("Inferência p95", f"{m['latency']['p95_ms']:.1f} ms")

    left, right = st.columns(2)
    with left:
        st.markdown("**Ablação: o que cada peça acrescenta**")
        ablation = pd.DataFrame(
            {
                "modelo": r["model"],
                "PR-AUC": r["pr_auc"],
                "precision": r["review"]["precision"],
                "recall": r["review"]["recall"],
            }
            for r in m["ablation"]
        )
        st.dataframe(
            ablation, hide_index=True, width="stretch",
            column_config={
                "PR-AUC": st.column_config.ProgressColumn(min_value=0, max_value=1, format="%.3f"),
                "precision": st.column_config.NumberColumn(format="percent"),
                "recall": st.column_config.NumberColumn(format="percent"),
            },
        )  # fmt: skip
        if adversarial := m.get("adversarial"):
            st.markdown("**Robustez: ataques simulados contra este modelo**")
            rows = pd.DataFrame(
                {
                    "ataque": ATTACK_LABELS.get(kind, kind),
                    "episódios detectados": r["episode_detection"],
                    "valor evitado": r["value_prevented"],
                }
                for kind, r in adversarial["attacks"].items()
            )
            st.dataframe(
                rows, hide_index=True, width="stretch",
                column_config={
                    "episódios detectados": st.column_config.NumberColumn(format="percent"),
                    "valor evitado": st.column_config.NumberColumn(format="percent"),
                },
            )  # fmt: skip
    with right:
        st.markdown("**Features mais importantes (participação no ganho)**")
        imp = pd.DataFrame(m["feature_importance"]).head(12)
        chart = (
            alt.Chart(imp)
            .mark_bar(cornerRadiusEnd=4, height=16, color=MAGNITUDE)
            .encode(
                y=alt.Y("feature:N", sort="-x", title=None, axis=alt.Axis(labelLimit=220)),
                x=alt.X("gain_share:Q", title="Participação no ganho", axis=alt.Axis(format="%")),
                tooltip=[
                    alt.Tooltip("feature:N", title="Feature"),
                    alt.Tooltip("gain_share:Q", title="Participação", format=".1%"),
                ],
            )
            .properties(height=alt.Step(26))
        )
        st.altair_chart(chart, width="stretch")


def main() -> None:
    st.title("Detecção de fraude em tempo real")
    top = st.columns([3, 1])
    window = top[1].selectbox(
        "Janela", [15, 60, 360, 1440], index=1,
        format_func=lambda m: f"últimos {m} min" if m < 60 else f"últimas {m // 60} h",
    )  # fmt: skip
    if top[1].button("Atualizar"):
        st.cache_data.clear()
    try:
        kpis(int(window))
        st.divider()
        alerts_section()
        st.divider()
        model_section()
    except httpx.HTTPError as exc:
        st.error(f"Não consegui falar com a API em {API_URL}: {exc}")


main()
