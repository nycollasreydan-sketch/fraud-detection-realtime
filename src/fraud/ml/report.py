"""Gera a documentação derivada do código: relatório do modelo e tabela de features.

    python -m fraud.ml.report --features docs/FEATURES.md

Os dois arquivos são gerados para nunca divergirem do que o código faz de verdade.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from fraud.features.compute import FEATURE_SPECS
from fraud.ml.model import FraudModel

GROUP_TITLES = {
    "transacao": "Da própria transação",
    "cliente": "Histórico do cliente (quem paga)",
    "recebedor": "Histórico do recebedor (quem recebe)",
}


def _n(value: int) -> str:
    """Inteiro com separador de milhar brasileiro."""
    return f"{value:,}".replace(",", ".")


def _pct(value: float) -> str:
    return f"{value:.1%}"


def _f(value: float | None, digits: int = 4) -> str:
    return "n/d" if value is None or value != value else f"{value:.{digits}f}"


def model_report(model: FraudModel) -> str:
    m: dict[str, Any] = model.metrics
    data, test, lat = m["data"], m["test"], m["latency"]
    lines = [
        "# Relatório do modelo",
        "",
        "> Gerado por `python -m fraud.ml.train --report docs/MODEL_REPORT.md`. Não edite à mão.",
        "",
        f"- **Versão:** `{model.version}`",
        f"- **Conjunto de features:** v{model.feature_version} "
        f"({len(model.model_features)} colunas, incluindo o score de anomalia)"
        if model.iforest is not None
        else f"- **Conjunto de features:** v{model.feature_version} ({len(model.features)} colunas)",
        f"- **Dados:** {data['source']}; {_n(data['n_train'])} transações de treino, "
        f"{_n(data['n_valid'])} de validação e {_n(data['n_test'])} de teste, em blocos "
        f"temporais consecutivos; taxa de fraude {data['fraud_rate']:.2%}",
        f"- **Limiares** (definidos na validação): revisão ≥ `{model.review_threshold:.4f}` "
        f"(orçamento de {model.params['review_fpr']:.1%} de falso positivo); bloqueio ≥ "
        f"`{model.block_threshold:.4f}` (precision ≥ {model.params['block_precision']:.0%})",
        "",
        "## Resultado no bloco de teste (o futuro)",
        "",
        "| Métrica | Valor |",
        "|---|---:|",
        f"| PR-AUC (average precision) | {_f(test['pr_auc'])} |",
        f"| ROC-AUC | {_f(test['roc_auc'])} |",
        f"| Brier score | {_f(test['brier'], 5)} |",
        f"| Precision no limiar de revisão | {_pct(test['review']['precision'])} |",
        f"| Recall no limiar de revisão | {_pct(test['review']['recall'])} |",
        f"| Falso positivo no limiar de revisão | {test['review']['false_positive_rate']:.2%} |",
        f"| Precision no limiar de bloqueio | {_pct(test['block']['precision'])} |",
        f"| Recall no limiar de bloqueio | {_pct(test['block']['recall'])} |",
        "",
        "## Ablação: o que cada peça acrescenta",
        "",
        "Mesmos dados, mesmos blocos temporais. O intervalo é um bootstrap de 95% do PR-AUC.",
        "",
        "| Modelo | PR-AUC | IC 95% | ROC-AUC | Precision (revisão) | Recall (revisão) |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for row in m["ablation"]:
        ci = row.get("pr_auc_ci")
        ci_text = f"{ci[0]:.3f} a {ci[1]:.3f}" if ci else "n/d"
        lines.append(
            f"| {row['model']} | {_f(row['pr_auc'])} | {ci_text} | {_f(row['roc_auc'])} | "
            f"{_pct(row['review']['precision'])} | {_pct(row['review']['recall'])} |"
        )
    lines += [
        "",
        "## Por cenário (limiar de revisão, bloco de teste)",
        "",
        "| Cenário | Transações | Sinalizadas |",
        "|---|---:|---:|",
    ]
    for scenario, r in m["per_scenario"].items():
        value = r.get("recall", r.get("false_positive_rate", 0.0))
        kind = "recall" if "recall" in r else "falso positivo"
        lines.append(f"| `{scenario}` | {int(r['n'])} | {value:.2%} ({kind}) |")
    lines += [
        "",
        "## Features mais importantes (ganho)",
        "",
        "| Feature | Participação no ganho |",
        "|---|---:|",
    ]
    lines += [f"| `{f['feature']}` | {_pct(f['gain_share'])} |" for f in m["feature_importance"]]
    if adv := m.get("adversarial"):
        lines += [
            "",
            "## Robustez adversarial (bateria padrão)",
            "",
            "Outra população e outras sementes; análise completa em [ADVERSARIAL.md](ADVERSARIAL.md).",
            f"Falso positivo em legítimas nesta população: {adv['legit_false_positive_rate']:.2%}.",
            "",
            "| Ataque | Episódios | Detecção por episódio | Valor evitado | Recall por transação |",
            "|---|---:|---:|---:|---:|",
        ]
        lines += [
            f"| `{kind}` | {r['episodes']} | {_pct(r['episode_detection'])} | "
            f"{_pct(r['value_prevented'])} | {_pct(r['tx_recall'])} |"
            for kind, r in adv["attacks"].items()
        ]
    lines += [
        "",
        "## Latência de inferência",
        "",
        "Uma transação, uma thread, incluindo score de anomalia, boosting e explicação TreeSHAP.",
        "",
        f"- p50: {lat['p50_ms']:.2f} ms",
        f"- p95: {lat['p95_ms']:.2f} ms",
        f"- p99: {lat['p99_ms']:.2f} ms",
        "",
    ]
    return "\n".join(lines)


def write_report(model: FraudModel, path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(model_report(model))


def feature_docs() -> str:
    lines = [
        "# Feature engineering",
        "",
        "> Tabelas geradas a partir de `FEATURE_SPECS` em `src/fraud/features/compute.py` por",
        "> `python -m fraud.ml.report --features docs/FEATURES.md`. Não edite à mão.",
        "",
        "## Princípios",
        "",
        "1. **Sem vazamento do presente.** As features de uma transação são calculadas com o",
        "   estado *anterior* a ela; só depois o estado é atualizado.",
        "2. **Uma implementação.** `compute_features` roda igual no replay de treino e no",
        "   worker ([ADR 005](decisions/005-paridade-treino-servico.md)).",
        "3. **Custo constante.** Cada estado é atualizado em O(1) com agregados de decaimento",
        "   exponencial ([ADR 004](decisions/004-agregados-com-decaimento-exponencial.md)).",
        '   "Meia-vida de 24h" significa que um evento perde metade do peso a cada 24 horas.',
        "4. **Relativo ao próprio cliente.** R$ 2.000 é rotina para um cliente e anomalia para",
        "   outro; quase toda feature de valor é uma razão ou z-score contra a linha de base",
        "   de quem paga.",
        "5. **Valores neutros sem histórico.** Cliente novo recebe razão 1 e z-score 0, e",
        "   `c_tx_count` diz ao modelo quanta evidência existe por trás dos números.",
        "",
        "A versão indica quando a feature entrou: **v1** é o conjunto base da literatura (RFM,",
        "novidade, geografia); **v2** foi criado depois de os ataques da Fase D passarem pelo",
        "v1 ([ADVERSARIAL.md](ADVERSARIAL.md)).",
        "",
    ]
    for group, title in GROUP_TITLES.items():
        lines += [f"## {title}", "", "| Feature | Versão | O que mede |", "|---|:---:|---|"]
        lines += [
            f"| `{s.name}` | v{s.version} | {s.description} |"
            for s in FEATURE_SPECS
            if s.group == group
        ]
        lines.append("")
    lines += [
        "## Gerada pelo modelo",
        "",
        "| Feature | O que mede |",
        "|---|---|",
        "| `anomaly_score` | Score da Isolation Forest treinada só em transações legítimas, "
        "sobre todas as features acima. Entra como coluna extra do LightGBM. |",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--features", default="docs/FEATURES.md")
    args = parser.parse_args()
    Path(args.features).write_text(feature_docs())
    print(f"escrito {args.features}")


if __name__ == "__main__":
    main()
