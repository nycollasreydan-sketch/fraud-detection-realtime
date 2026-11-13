"""Demonstração de ponta a ponta (`make demo`).

Em ordem: confere a saúde do sistema, mostra o contrato da API (erro padronizado e
idempotência), envia alguns dias de tráfego sintético com fraudes e ataques, espera o
worker decidir tudo, compara as decisões com o gabarito do simulador, mostra alertas com
explicação e exercita a dead-letter queue.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import defaultdict
from datetime import datetime
from typing import Any

import httpx
from redis.asyncio import Redis

from fraud.config import get_settings
from fraud.simulation.attacks import ATTACK_LABELS
from fraud.simulation.generator import Sent, build_stream, send_stream, to_payload

SCENARIO_LABELS = {
    "stolen_card": "Cartão roubado (e-commerce)",
    "account_takeover": "Tomada de conta",
    "card_testing": "Teste de cartão",
    **ATTACK_LABELS,
}


def title(text: str) -> None:
    print(f"\n\033[1m== {text} ==\033[0m")


async def wait_healthy(client: httpx.AsyncClient, timeout_s: float = 120.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            response = await client.get("/health")
            if response.status_code == 200:
                return dict(response.json())
        except httpx.HTTPError:
            pass
        if time.monotonic() > deadline:
            raise SystemExit("API não ficou saudável a tempo; rode `make up` e veja `make logs`")
        await asyncio.sleep(1.0)


async def fetch_all(client: httpx.AsyncClient, ids: list[str]) -> dict[str, dict[str, Any]]:
    semaphore = asyncio.Semaphore(32)

    async def one(tx_id: str) -> tuple[str, dict[str, Any]]:
        async with semaphore:
            return tx_id, (await client.get(f"/transactions/{tx_id}")).json()

    return dict(await asyncio.gather(*(one(i) for i in ids)))


async def wait_scored(
    client: httpx.AsyncClient, ids: list[str], timeout_s: float = 180.0
) -> dict[str, dict[str, Any]]:
    deadline = time.monotonic() + timeout_s
    results: dict[str, dict[str, Any]] = {}
    pending = list(ids)
    while pending:
        results.update(await fetch_all(client, pending))
        pending = [i for i in pending if results[i].get("status") == "pending"]
        print(f"\r  decididas {len(ids) - len(pending)}/{len(ids)}", end="")
        sys.stdout.flush()
        if pending:
            if time.monotonic() > deadline:
                print()
                raise SystemExit(f"{len(pending)} transações ainda pendentes; veja `make logs`")
            await asyncio.sleep(1.0)
    print()
    return results


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(int(q * len(ordered)), len(ordered) - 1)] if ordered else float("nan")


def summarize(sent: list[Sent], results: dict[str, dict[str, Any]], cutoff: float) -> None:
    """Compara decisões e gabarito, só para transações posteriores ao aquecimento."""
    tp = fp = fn = tn = 0
    by_scenario: dict[str, list[bool]] = defaultdict(list)
    episodes: dict[str, dict[str, list[bool]]] = defaultdict(lambda: defaultdict(list))
    latencies: list[float] = []
    for s in sent:
        if s.server_id is None:
            continue
        r = results[s.server_id]
        if r.get("processed_at") and r.get("created_at"):
            delta = datetime.fromisoformat(r["processed_at"]) - datetime.fromisoformat(
                r["created_at"]
            )
            latencies.append(delta.total_seconds() * 1000)
        if s.item.event.ts < cutoff:
            continue
        flagged = r.get("decision") in ("review", "block")
        if s.item.is_fraud:
            tp += flagged
            fn += not flagged
            by_scenario[s.item.scenario].append(flagged)
            episodes[s.item.scenario][s.item.episode].append(flagged)
        else:
            fp += flagged
            tn += not flagged

    title("Decisões do modelo contra o gabarito do simulador (após o aquecimento)")
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    print(f"  transações: {tp + fp + fn + tn}   fraudes: {tp + fn}   alertas: {tp + fp}")
    print(f"  precision: {precision:.1%}   recall: {recall:.1%}   "
          f"falso positivo: {fp / max(fp + tn, 1):.2%}")  # fmt: skip
    print(f"\n  {'cenário':<36}{'transações':>11}{'recall tx':>11}{'episódios detectados':>23}")
    for scenario, flags in sorted(by_scenario.items()):
        eps = episodes[scenario]
        detected = sum(any(v) for v in eps.values())
        print(
            f"  {SCENARIO_LABELS.get(scenario, scenario):<36}{len(flags):>11}"
            f"{sum(flags) / len(flags):>11.0%}{f'{detected}/{len(eps)}':>23}"
        )
    title("Latência sob rajada (carga em lote: inclui o tempo de espera na fila)")
    print(
        f"  p50 {percentile(latencies, 0.5) / 1000:.1f} s   p95 "
        f"{percentile(latencies, 0.95) / 1000:.1f} s   ({len(latencies)} transações enviadas "
        "de uma vez)"
    )


async def latency_probe(client: httpx.AsyncClient, samples: list[dict[str, object]]) -> None:
    """Latência em regime: uma transação por vez, com a fila vazia.

    Cada amostra é uma compra de rotina de um cliente diferente, reenviada com a hora atual.
    """
    title("Latência em regime (fila vazia, uma transação por vez)")
    for _ in range(120):  # espera a fila drenar de verdade
        queue = (await client.get("/stats")).json()["queue"]
        if queue["lag"] == 0 and queue["pending"] == 0:
            break
        await asyncio.sleep(0.5)
    ids = []
    for sample in samples:
        body = {k: v for k, v in sample.items() if k != "occurred_at"}
        ids.append((await client.post("/transactions", json=body)).json()["id"])
        await asyncio.sleep(0.1)
    await asyncio.sleep(1.0)
    results = await fetch_all(client, ids)
    values = [
        (
            datetime.fromisoformat(r["processed_at"]) - datetime.fromisoformat(r["created_at"])
        ).total_seconds()
        * 1000
        for r in results.values()
        if r.get("processed_at")
    ]
    print(
        f"  entrada na API -> decisão gravada: p50 {percentile(values, 0.5):.0f} ms   "
        f"p95 {percentile(values, 0.95):.0f} ms   p99 {percentile(values, 0.99):.0f} ms   "
        f"(n={len(values)})"
    )


async def show_api_contract(client: httpx.AsyncClient, sample: dict[str, object]) -> None:
    title("Contrato da API")
    bad = await client.post("/transactions", json={**sample, "amount": "-5"})
    print(
        f"  valor negativo -> HTTP {bad.status_code}: {json.dumps(bad.json(), ensure_ascii=False)[:230]}"
    )
    headers = {"Idempotency-Key": f"demo-{time.time_ns()}"}
    first = await client.post("/transactions", json=sample, headers=headers)
    again = await client.post("/transactions", json=sample, headers=headers)
    same = first.json()["id"] == again.json()["id"]
    print(f"  mesma Idempotency-Key duas vezes -> HTTP {first.status_code}, depois HTTP "
          f"{again.status_code}; mesmo id: {same}")  # fmt: skip


async def show_alerts(client: httpx.AsyncClient, run_id: str, sent: list[Sent]) -> None:
    title("Alertas com explicação (GET /alerts)")
    truth = {s.server_id: s.item for s in sent if s.server_id}
    page = (await client.get("/alerts", params={"severity": "high", "limit": 200})).json()
    mine = [a for a in page["items"] if a["transaction_id"] in truth][:3]
    print(f"  {page['total']} alertas de severidade alta no total; três desta execução:")
    for alert in mine:
        item = truth.get(alert["transaction_id"])
        label = SCENARIO_LABELS.get(item.scenario, "legítima") if item else "?"
        print(f"\n  cliente {alert['customer_id']}  R$ {alert['amount']}  score {alert['score']:.3f}"
              f"  gabarito: {label}")  # fmt: skip
        for reason in alert["reasons"][:3]:
            print(
                f"    +{reason['contribution']:.2f}  {reason['description']} = {reason['value']:.2f}"
            )


async def show_dead_letter(client: httpx.AsyncClient) -> None:
    title("Tratamento de falhas: dead-letter queue")
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        before = (await client.get("/stats")).json()["queue"]["dead_letter"]
        await redis.xadd(settings.stream_name, {"transaction_id": "isto-nao-e-um-uuid"})
        for _ in range(20):
            await asyncio.sleep(0.5)
            after = (await client.get("/stats")).json()["queue"]["dead_letter"]
            if after > before:
                break
        print(f"  mensagem envenenada publicada na fila -> DLQ foi de {before} para {after};"
              " o worker seguiu processando")  # fmt: skip
    finally:
        await redis.aclose()


async def run(args: argparse.Namespace) -> None:
    started = time.perf_counter()
    async with httpx.AsyncClient(base_url=args.api_url, timeout=30.0) as client:
        title("Saúde do sistema")
        print(f"  {await wait_healthy(client)}")
        info = (await client.get("/model/info")).json()
        test = info["metrics"]["test"]
        print(f"  modelo {info['version']}: PR-AUC {test['pr_auc']:.3f} no teste offline, "
              f"{len(info['features'])} features")  # fmt: skip

        run_id = f"demo{int(time.time()) % 100000}"
        stream = build_stream(
            args.customers,
            args.days,
            args.seed,
            args.attacks,
            run_id=run_id,
            warmup_days=args.warmup,
        )
        cutoff = min(x.event.ts for x in stream) + args.warmup * 86400
        await show_api_contract(client, to_payload(stream[0]))

        title(f"Enviando {len(stream)} transações ({args.days:.0f} dias simulados, sendo "
              f"{args.warmup:.0f} de aquecimento; {sum(x.is_fraud for x in stream)} fraudulentas)")  # fmt: skip
        sent = await send_stream(args.api_url, stream, args.concurrency)
        ids = [s.server_id for s in sent if s.server_id]
        print(f"  aceitas: {len(ids)}/{len(sent)}")

        title("Aguardando o worker decidir")
        results = await wait_scored(client, ids)
        summarize(sent, results, cutoff)
        await show_alerts(client, run_id, sent)
        routine: dict[str, dict[str, object]] = {}
        for x in stream:  # uma compra antiga (fora da janela de duplicatas) por cliente
            if not x.is_fraud and x.event.channel == "pos" and x.event.ts < cutoff:
                routine.setdefault(x.event.customer_id, to_payload(x))
        await latency_probe(client, list(routine.values()))
        await show_dead_letter(client)

    title(f"Pronto em {time.perf_counter() - started:.0f} s")
    print("  Painel      http://localhost:8501")
    print("  API (docs)  http://localhost:8000/docs")
    print("  Prometheus  http://localhost:9090")
    print(
        "  Jaeger      http://localhost:16686  (serviço fraud-api: um trace cobre API -> fila -> worker)"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default=get_settings().api_url)
    parser.add_argument("--customers", type=int, default=40)
    parser.add_argument("--days", type=float, default=31.0)
    parser.add_argument("--warmup", type=float, default=21.0, help="dias de aquecimento")
    parser.add_argument("--attacks", type=int, default=2)
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--concurrency", type=int, default=16)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
