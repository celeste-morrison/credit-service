"""HTTP behaviour, with a real repository and a fake publisher."""

from collections.abc import AsyncIterator, Iterator
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pymongo import MongoClient
from redis.asyncio import Redis

from app.api.health import Readiness
from app.api.main import create_app
from app.api.ratelimit import RateLimiter
from app.config import Settings, get_settings
from app.db import CREDITS, Doc
from app.repository import AsyncCreditRepository, CreditRepository
from tests.integration.helpers import fetch_user

API_KEY = "test-key"
OTHER_KEY = "other-client-key"


class AllowAll:
    """Rate limiter stand-in that never limits."""

    async def retry_after(self, client_id: str) -> int | None:
        """Allow every request."""
        return None


class FakePublisher:
    """Records published credit ids, or raises ``error`` if set."""

    def __init__(self) -> None:
        self.published: list[str] = []
        self.error: Exception | None = None

    def publish(self, credit_id: str) -> None:
        """Record or fail the publish."""
        if self.error is not None:
            raise self.error
        self.published.append(credit_id)


async def _ok() -> None:
    return None


async def _down() -> None:
    raise ConnectionError("down")


@pytest.fixture
def publisher() -> FakePublisher:
    """A fresh fake publisher."""
    return FakePublisher()


@pytest.fixture
def app(api_repo: AsyncCreditRepository, publisher: FakePublisher) -> FastAPI:
    """The app with state wired directly, since ASGITransport skips the lifespan."""
    app = create_app()
    app.state.repository = api_repo
    app.state.publisher = publisher
    app.state.readiness = Readiness({"mongo": _ok, "redis": _ok}, ttl_s=0)
    app.state.api_clients = {API_KEY: "billing", "rotated-key": "billing", OTHER_KEY: "payouts"}
    app.state.rate_limiter = AllowAll()
    return app


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """An authenticated client for ``app``."""
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers={"X-API-Key": API_KEY}
    ) as c:
        yield c


@pytest.fixture
async def anon(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """A client for ``app`` that sends no API key."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def post_credit(
    client: httpx.AsyncClient, amount: str = "50.00", **headers: str
) -> httpx.Response:
    """POST a credit for ``u_123``."""
    return await client.post(
        "/credits", json={"user_id": "u_123", "amount": amount}, headers=headers
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
        {"user_id": "u_123", "amount": "1_000"},
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


async def test_publish_failure_returns_503_and_fails_credit(
    client: httpx.AsyncClient,
    publisher: FakePublisher,
    api_repo: AsyncCreditRepository,
    sync_client: MongoClient[Doc],
    db_name: str,
) -> None:
    publisher.error = ConnectionError("redis down")

    resp = await post_credit(client)

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "queue_unavailable"
    user = await fetch_user(api_repo)
    assert user.status == "processed"
    assert user.pending_credits == 0
    credit = sync_client[db_name][CREDITS].find_one()
    assert credit is not None
    assert credit["status"] == "failed"


async def test_retry_with_same_key_after_503_creates_and_publishes_credit(
    client: httpx.AsyncClient, publisher: FakePublisher
) -> None:
    publisher.error = ConnectionError("redis down")
    assert (await post_credit(client, **{"Idempotency-Key": "abc"})).status_code == 503

    publisher.error = None
    retry = await post_credit(client, **{"Idempotency-Key": "abc"})

    assert retry.status_code == 202
    assert retry.json()["status"] == "pending"
    assert publisher.published == [retry.json()["task_id"]]


async def test_idempotency_key_replay_publishes_once(
    client: httpx.AsyncClient, publisher: FakePublisher
) -> None:
    first = await post_credit(client, **{"Idempotency-Key": "abc"})
    second = await post_credit(client, **{"Idempotency-Key": "abc"})

    assert first.status_code == second.status_code == 202
    assert first.json()["task_id"] == second.json()["task_id"]
    assert len(publisher.published) == 1


async def test_idempotency_key_reuse_with_new_amount_is_409(client: httpx.AsyncClient) -> None:
    await post_credit(client, **{"Idempotency-Key": "abc"})
    resp = await post_credit(client, amount="99.00", **{"Idempotency-Key": "abc"})

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "idempotency_conflict"


async def test_unknown_credit_and_user_are_404(client: httpx.AsyncClient) -> None:
    assert (await client.get("/credits/nope")).json()["error"]["code"] == "credit_not_found"
    assert (await client.get("/users/nobody")).status_code == 404


async def test_health_and_readiness(client: httpx.AsyncClient, app: FastAPI) -> None:
    assert (await client.get("/healthz")).status_code == 200
    assert (await client.get("/readyz")).status_code == 200

    app.state.readiness = Readiness({"mongo": _ok, "redis": _down}, ttl_s=0)
    resp = await client.get("/readyz")
    assert resp.status_code == 503
    assert resp.json()["checks"] == {"mongo": "ok", "redis": "error: ConnectionError"}


async def test_readiness_result_is_cached() -> None:
    calls = 0

    async def counting() -> None:
        nonlocal calls
        calls += 1

    readiness = Readiness({"mongo": counting}, ttl_s=60)
    for _ in range(5):
        await readiness.results()

    assert calls == 1


@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong"}, {"X-API-Key": ""}])
async def test_credit_routes_require_api_key(
    anon: httpx.AsyncClient, publisher: FakePublisher, headers: dict[str, str]
) -> None:
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
    app.state.api_clients = {}
    assert (await client.get("/users/nobody")).status_code == 401


async def test_oversized_body_is_rejected_before_auth(anon: httpx.AsyncClient) -> None:
    resp = await anon.post("/credits", content=b"x" * (get_settings().max_body_bytes + 1))

    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "payload_too_large"


async def test_chunked_body_without_length_is_rejected(client: httpx.AsyncClient) -> None:
    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(64):
            yield b"x" * 1024

    resp = await client.post("/credits", content=chunks())

    assert resp.status_code == 411
    assert resp.json()["error"]["code"] == "length_required"


@pytest.mark.parametrize(
    ("supplied", "echoed"), [("req-42", True), ("x" * 65, False), ("bad id\n", False)]
)
async def test_request_id_is_echoed_only_when_valid(
    client: httpx.AsyncClient, supplied: str, echoed: bool
) -> None:
    resp = await client.get("/healthz", headers={"X-Request-ID": supplied})

    assert (resp.headers["x-request-id"] == supplied) is echoed
    assert len(resp.headers["x-request-id"]) <= 64


async def test_unhandled_error_uses_error_envelope(
    client: httpx.AsyncClient, api_repo: AsyncCreditRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(_: str) -> None:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(api_repo, "get_user", boom)

    resp = await client.get("/users/u_123")

    assert resp.status_code == 500
    assert resp.json() == {"error": {"code": "internal_error", "message": "Internal server error"}}


@pytest.fixture
def lifespan_client(db_name: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A client that runs the real lifespan against the per-test database."""
    monkeypatch.setenv("CREDITS_MONGO_DB", db_name)
    monkeypatch.setenv("CREDITS_API_KEYS", API_KEY)
    get_settings.cache_clear()
    try:
        with TestClient(create_app()) as client:
            yield client
    finally:
        get_settings.cache_clear()


def test_lifespan_creates_indexes_and_reports_ready(
    lifespan_client: TestClient, sync_client: MongoClient[Doc], db_name: str
) -> None:
    resp = lifespan_client.get("/readyz")

    assert resp.status_code == 200, resp.json()
    assert resp.json()["checks"] == {"mongo": "ok", "redis": "ok"}
    indexes = sync_client[db_name][CREDITS].index_information()
    assert "client_user_idempotency_key_unique" in indexes


async def test_credit_records_the_issuing_client(
    client: httpx.AsyncClient, sync_client: MongoClient[Doc], db_name: str
) -> None:
    task_id = (await post_credit(client)).json()["task_id"]

    stored = sync_client[db_name][CREDITS].find_one({"_id": task_id})
    assert stored is not None
    assert stored["client_id"] == "billing"


async def test_idempotency_keys_are_scoped_per_client(
    client: httpx.AsyncClient, publisher: FakePublisher
) -> None:
    first = await post_credit(client, **{"Idempotency-Key": "abc"})
    other = await post_credit(client, **{"Idempotency-Key": "abc", "X-API-Key": OTHER_KEY})

    assert first.json()["task_id"] != other.json()["task_id"]
    assert len(publisher.published) == 2


async def test_rate_limit_returns_429_with_retry_after(
    client: httpx.AsyncClient, app: FastAPI, settings: Settings
) -> None:
    redis = Redis.from_url(settings.redis_url)
    app.state.api_clients = {API_KEY: f"limited-{uuid4().hex}"}
    app.state.rate_limiter = RateLimiter(redis, limit=2)
    try:
        statuses = [(await client.get("/users/nobody")).status_code for _ in range(3)]
        limited = await client.get("/users/nobody")
    finally:
        await redis.aclose()

    assert statuses == [404, 404, 429]
    assert limited.json()["error"]["code"] == "rate_limited"
    assert 1 <= int(limited.headers["retry-after"]) <= 60


async def test_rate_limiter_allows_requests_when_redis_is_down() -> None:
    redis = Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.2)
    try:
        assert await RateLimiter(redis, limit=0).retry_after("anyone") is None
    finally:
        await redis.aclose()


async def test_api_docs_are_disabled_by_default(anon: httpx.AsyncClient) -> None:
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert (await anon.get(path)).status_code == 404


def test_api_docs_can_be_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CREDITS_DOCS_ENABLED", "true")
    get_settings.cache_clear()
    try:
        app = create_app()
    finally:
        get_settings.cache_clear()

    assert app.openapi_url == "/openapi.json"
