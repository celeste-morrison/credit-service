from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.api.main import create_app
from app.repository import AsyncCreditRepository, CreditRepository

API_KEY = "test-key"


class FakePublisher:
    def __init__(self) -> None:
        self.published: list[str] = []
        self.error: Exception | None = None

    def publish(self, credit_id: str) -> None:
        if self.error is not None:
            raise self.error
        self.published.append(credit_id)


async def _ok() -> None:
    return None


async def _down() -> None:
    raise ConnectionError("down")


@pytest.fixture
def publisher() -> FakePublisher:
    return FakePublisher()


@pytest.fixture
def app(api_repo: AsyncCreditRepository, publisher: FakePublisher) -> FastAPI:
    app = create_app()
    # ASGITransport doesn't run the lifespan, so wire state directly.
    app.state.repository = api_repo
    app.state.publisher = publisher
    app.state.readiness_checks = {"mongo": _ok, "redis": _ok}
    app.state.api_keys = frozenset({API_KEY, "rotated-key"})
    return app


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers={"X-API-Key": API_KEY}
    ) as c:
        yield c


async def post_credit(client: httpx.AsyncClient, **headers: str) -> httpx.Response:
    return await client.post(
        "/credits", json={"user_id": "u_123", "amount": "50.00"}, headers=headers
    )


async def test_post_credit_returns_202_and_publishes(
    client: httpx.AsyncClient, publisher: FakePublisher
) -> None:
    resp = await post_credit(client)

    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "pending"
    assert resp.headers["location"] == f"/credits/{body['task_id']}"
    assert publisher.published == [body["task_id"]]

    user = (await client.get("/users/u_123")).json()
    assert user == {
        "user_id": "u_123",
        "balance": "0.00",
        "status": "processing",
        "pending_credits": 1,
    }


async def test_full_flow_with_worker_apply(
    client: httpx.AsyncClient, worker_repo: CreditRepository
) -> None:
    task_id = (await post_credit(client)).json()["task_id"]
    worker_repo.apply_credit(task_id)

    user = (await client.get("/users/u_123")).json()
    assert user["balance"] == "50.00"
    assert user["status"] == "processed"
    credit = (await client.get(f"/credits/{task_id}")).json()
    assert credit["status"] == "applied"
    assert credit["amount"] == "50.00"


@pytest.mark.parametrize(
    "payload",
    [
        {"user_id": "u_123", "amount": -1},
        {"user_id": "u_123", "amount": "1.001"},
        {"user_id": "", "amount": "1"},
        {"user_id": "u_123"},
        {"user_id": "u_123", "amount": "1", "status": "processed"},
    ],
)
async def test_invalid_request_returns_422_envelope(
    client: httpx.AsyncClient, publisher: FakePublisher, payload: dict[str, Any]
) -> None:
    resp = await client.post("/credits", json=payload)

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"
    assert publisher.published == []


async def test_publish_failure_returns_503_and_releases_user(
    client: httpx.AsyncClient, publisher: FakePublisher, api_repo: AsyncCreditRepository
) -> None:
    publisher.error = ConnectionError("redis down")

    resp = await post_credit(client)

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "queue_unavailable"
    user = await api_repo.get_user("u_123")
    assert user is not None
    assert user.status == "processed"
    assert user.pending_credits == 0


async def test_idempotency_key_replay_publishes_once(
    client: httpx.AsyncClient, publisher: FakePublisher
) -> None:
    first = await post_credit(client, **{"Idempotency-Key": "abc"})
    second = await post_credit(client, **{"Idempotency-Key": "abc"})

    assert first.status_code == second.status_code == 202
    assert first.json()["task_id"] == second.json()["task_id"]
    assert len(publisher.published) == 1


async def test_idempotency_key_reuse_with_new_body_is_409(client: httpx.AsyncClient) -> None:
    await post_credit(client, **{"Idempotency-Key": "abc"})
    resp = await client.post(
        "/credits",
        json={"user_id": "u_123", "amount": "99.00"},
        headers={"Idempotency-Key": "abc"},
    )

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "idempotency_conflict"


async def test_unknown_credit_and_user_are_404(client: httpx.AsyncClient) -> None:
    assert (await client.get("/credits/nope")).json()["error"]["code"] == "credit_not_found"
    assert (await client.get("/users/nobody")).status_code == 404


async def test_health_and_readiness(client: httpx.AsyncClient, app: FastAPI) -> None:
    assert (await client.get("/healthz")).status_code == 200
    assert (await client.get("/readyz")).status_code == 200

    app.state.readiness_checks = {"mongo": _ok, "redis": _down}
    resp = await client.get("/readyz")
    assert resp.status_code == 503
    assert resp.json()["checks"] == {"mongo": "ok", "redis": "error: ConnectionError"}


@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong"}, {"X-API-Key": ""}])
async def test_credit_routes_require_api_key(
    app: FastAPI, publisher: FakePublisher, headers: dict[str, str]
) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as anon:
        post = await anon.post("/credits", json={"user_id": "u_1", "amount": "1"}, headers=headers)
        get = await anon.get("/users/u_1", headers=headers)
        health = await anon.get("/healthz")

    assert post.status_code == get.status_code == 401
    assert post.json()["error"]["code"] == "unauthorized"
    assert publisher.published == []
    assert health.status_code == 200


async def test_any_configured_key_is_accepted(client: httpx.AsyncClient) -> None:
    resp = await client.get("/users/nobody", headers={"X-API-Key": "rotated-key"})
    assert resp.status_code == 404


async def test_no_configured_keys_fails_closed(client: httpx.AsyncClient, app: FastAPI) -> None:
    app.state.api_keys = frozenset()
    assert (await client.get("/users/nobody")).status_code == 401


async def test_response_echoes_request_id(client: httpx.AsyncClient) -> None:
    resp = await client.get("/healthz", headers={"X-Request-ID": "req-42"})
    assert resp.headers["x-request-id"] == "req-42"
