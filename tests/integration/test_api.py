from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from redis.asyncio import Redis

from fraud.api.app import create_app
from fraud.config import Settings
from fraud.db.models import Alert, Transaction
from fraud.db.session import SessionFactory
from tests.integration.conftest import payload


def assert_error(response: httpx.Response, status: int, code: str) -> dict:  # type: ignore[type-arg]
    assert response.status_code == status
    error = response.json()["error"]
    assert error["code"] == code
    assert error["message"]
    assert error["request_id"]
    return error  # type: ignore[no-any-return]


# ---------------------------------------------------------------- POST /transactions
async def test_create_transaction_persists_and_enqueues(
    client: httpx.AsyncClient, redis: Redis, sessions: SessionFactory, settings: Settings
) -> None:
    response = await client.post("/transactions", json=payload())
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "pending"
    assert body["score"] is None
    assert response.headers["x-request-id"]

    async with sessions() as session:
        row = await session.get(Transaction, uuid.UUID(body["id"]))
        assert row is not None
        assert row.status == "pending"
        assert float(row.amount) == 100.0

    messages = await redis.xrange(settings.stream_name)
    assert len(messages) == 1
    fields = messages[0][1]
    assert fields["transaction_id"] == body["id"]
    assert "traceparent" in fields  # contexto de trace viaja na mensagem


async def test_validation_error_uses_standard_envelope(client: httpx.AsyncClient) -> None:
    response = await client.post("/transactions", json=payload(amount="-1", channel="boleto"))
    error = assert_error(response, 422, "validation_error")
    fields = {d["field"] for d in error["details"]}
    assert fields == {"body.amount", "body.channel"}


async def test_malformed_json_uses_standard_envelope(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/transactions", content=b"{nope", headers={"content-type": "application/json"}
    )
    assert_error(response, 422, "validation_error")


async def test_future_event_time_is_rejected(client: httpx.AsyncClient) -> None:
    future = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    response = await client.post("/transactions", json=payload(occurred_at=future))
    error = assert_error(response, 422, "validation_error")
    assert error["details"][0]["field"] == "body.occurred_at"


async def test_idempotency_key_returns_same_transaction(
    client: httpx.AsyncClient, redis: Redis, settings: Settings
) -> None:
    headers = {"Idempotency-Key": "abc-123"}
    first = await client.post("/transactions", json=payload(), headers=headers)
    second = await client.post("/transactions", json=payload(), headers=headers)
    assert first.status_code == 202
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert await redis.xlen(settings.stream_name) == 1  # replay da requisição não reenfileira


async def test_idempotency_key_with_different_payload_conflicts(client: httpx.AsyncClient) -> None:
    headers = {"Idempotency-Key": "abc-456"}
    await client.post("/transactions", json=payload(), headers=headers)
    response = await client.post("/transactions", json=payload(amount="999.00"), headers=headers)
    assert_error(response, 409, "conflict")


@pytest.mark.parametrize(
    "changed",
    [{"currency": "USD"}, {"channel": "pos"}, {"category": "travel"}, {"latitude": 10.0}],
    ids=lambda c: next(iter(c)),
)
async def test_idempotency_conflict_covers_every_payload_field(
    client: httpx.AsyncClient, changed: dict[str, object]
) -> None:
    headers = {"Idempotency-Key": "abc-789"}
    await client.post("/transactions", json=payload(), headers=headers)
    response = await client.post("/transactions", json=payload(**changed), headers=headers)
    assert_error(response, 409, "conflict")


async def test_idempotent_retry_with_explicit_event_time(client: httpx.AsyncClient) -> None:
    headers = {"Idempotency-Key": "abc-790"}
    body = payload(occurred_at="2026-01-05T12:00:00.123456-03:00")
    first = await client.post("/transactions", json=body, headers=headers)
    again = await client.post("/transactions", json=body, headers=headers)
    assert (first.status_code, again.status_code) == (202, 200)
    other = payload(occurred_at="2026-01-05T12:00:01-03:00")
    assert_error(await client.post("/transactions", json=other, headers=headers), 409, "conflict")


async def test_queue_down_returns_503_but_keeps_transaction(
    settings: Settings, sessions: SessionFactory
) -> None:
    broken = settings.model_copy(update={"redis_url": "redis://localhost:1/0"})
    app = create_app(broken)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            response = await http.post("/transactions", json=payload())
            error = assert_error(response, 503, "service_unavailable")
            health = await http.get("/health")
    assert health.status_code == 503
    assert health.json()["checks"]["redis"].startswith("error")
    assert health.json()["checks"]["database"] == "ok"
    # a transação ficou registrada como pendente: o sweeper do worker a republica
    async with sessions() as session:
        row = await session.get(Transaction, uuid.UUID(error["details"]["transaction_id"]))
        assert row is not None
        assert row.status == "pending"


# ---------------------------------------------------------------- GET /transactions/{id}
async def test_get_transaction(client: httpx.AsyncClient) -> None:
    created = (await client.post("/transactions", json=payload())).json()
    response = await client.get(f"/transactions/{created['id']}")
    assert response.status_code == 200
    assert response.json()["id"] == created["id"]


async def test_get_unknown_transaction_is_404(client: httpx.AsyncClient) -> None:
    assert_error(await client.get(f"/transactions/{uuid.uuid4()}"), 404, "not_found")


async def test_get_invalid_id_is_422(client: httpx.AsyncClient) -> None:
    assert_error(await client.get("/transactions/not-a-uuid"), 422, "validation_error")


async def test_unknown_route_uses_standard_envelope(client: httpx.AsyncClient) -> None:
    assert_error(await client.get("/nope"), 404, "not_found")
    assert_error(await client.delete("/alerts"), 405, "method_not_allowed")


# ---------------------------------------------------------------- GET /alerts
async def seed_alerts(sessions: SessionFactory, n: int = 7) -> None:
    now = datetime.now(UTC)
    async with sessions() as session:
        for i in range(n):
            tx = Transaction(
                id=uuid.uuid4(), customer_id=f"c{i % 2}", counterparty_id="m1", amount=10 + i,
                currency="BRL", channel="pos", category="other", device_id="d", latitude=0,
                longitude=0, occurred_at=now, status="scored",
            )  # fmt: skip
            session.add(tx)
            await session.flush()  # sem relationship no ORM: garante a ordem do FK
            session.add(
                Alert(
                    transaction_id=tx.id, customer_id=tx.customer_id, counterparty_id="m1",
                    amount=tx.amount, score=0.1 * (i + 1), model_version="test",
                    severity="high" if i >= 4 else "medium",
                    status="dismissed" if i == 0 else "open",
                    reasons=[{"feature": "amount", "value": 1.0, "contribution": 0.5, "description": "x"}],
                    created_at=now - timedelta(minutes=n - i),
                )
            )  # fmt: skip
        await session.commit()


async def test_alerts_pagination(client: httpx.AsyncClient, sessions: SessionFactory) -> None:
    await seed_alerts(sessions)
    page1 = (await client.get("/alerts", params={"limit": 3})).json()
    page2 = (await client.get("/alerts", params={"limit": 3, "offset": 3})).json()
    page3 = (await client.get("/alerts", params={"limit": 3, "offset": 6})).json()
    assert page1["total"] == 7
    assert page1["limit"] == 3
    assert page1["offset"] == 0
    assert [len(p["items"]) for p in (page1, page2, page3)] == [3, 3, 1]
    scores = [a["score"] for p in (page1, page2, page3) for a in p["items"]]
    assert scores == sorted(scores, reverse=True)  # mais recente primeiro
    assert len({a["id"] for p in (page1, page2, page3) for a in p["items"]}) == 7


async def test_alerts_filters(client: httpx.AsyncClient, sessions: SessionFactory) -> None:
    await seed_alerts(sessions)

    async def total(**params: object) -> int:
        response = await client.get("/alerts", params=params)  # type: ignore[arg-type]
        assert response.status_code == 200
        return int(response.json()["total"])

    assert await total(severity="high") == 3
    assert await total(severity="medium", status="open") == 3
    assert await total(status="dismissed") == 1
    assert await total(customer_id="c0") == 4
    assert await total(min_score=0.55) == 2
    assert await total(customer_id="c1", severity="high") == 1
    since = (datetime.now(UTC) - timedelta(minutes=3, seconds=30)).isoformat()
    assert await total(since=since) == 3
    assert await total(until=since) == 4


async def test_alerts_rejects_bad_parameters(client: httpx.AsyncClient) -> None:
    for params in (
        {"limit": 0},
        {"limit": 1000},
        {"offset": -1},
        {"severity": "x"},
        {"min_score": 2},
    ):
        assert_error(await client.get("/alerts", params=params), 422, "validation_error")


# ---------------------------------------------------------------- demais endpoints
async def test_health_ok(client: httpx.AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "checks": {"database": "ok", "redis": "ok", "model": "ok"},
    }


async def test_model_info(client: httpx.AsyncClient, settings: Settings, tmp_path: Path) -> None:
    info = (await client.get("/model/info")).json()
    assert info["version"]
    assert info["metrics"]["test"]["pr_auc"] > 0
    app = create_app(settings.model_copy(update={"model_dir": str(tmp_path)}))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            assert_error(await http.get("/model/info"), 404, "not_found")
            assert (await http.get("/health")).json()["checks"]["model"] == "missing"


async def test_stats_and_metrics(client: httpx.AsyncClient, sessions: SessionFactory) -> None:
    await seed_alerts(sessions, n=5)
    await client.post("/transactions", json=payload())
    stats = (await client.get("/stats")).json()
    assert stats["transactions"] == {"scored": 5, "pending": 1}
    assert stats["alerts"] == {"medium": 4, "high": 1}
    assert stats["queue"] == {"length": 1, "lag": 1, "pending": 0, "dead_letter": 0}

    metrics = (await client.get("/metrics")).text
    assert 'fraud_http_requests_total{method="POST",route="/transactions",status="202"}' in metrics
    assert "fraud_transactions_ingested_total" in metrics
    assert "/transactions/{transaction_id}" not in metrics or "not-a-uuid" not in metrics


async def test_openapi_documents_error_envelope(client: httpx.AsyncClient) -> None:
    spec = (await client.get("/openapi.json")).json()
    responses = spec["paths"]["/transactions"]["post"]["responses"]
    assert {"202", "200", "409", "422", "503"} <= set(responses)
    assert "ErrorResponse" in spec["components"]["schemas"]
