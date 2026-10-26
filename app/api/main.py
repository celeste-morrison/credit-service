"""FastAPI application factory and lifespan."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from redis.asyncio import Redis

from app.api.errors import register_error_handlers
from app.api.health import Readiness
from app.api.middleware import BodySizeLimit, request_context
from app.api.ratelimit import RateLimiter
from app.api.routes import probes, router
from app.config import get_settings
from app.db import ensure_indexes_async, make_async_client
from app.logging import configure_logging
from app.publisher import CeleryCreditPublisher
from app.repository import AsyncCreditRepository

API_DESCRIPTION = """
Credits a user's balance asynchronously. `POST /credits` returns `202 Accepted`
with a `task_id`; a worker then applies the credit. Poll `GET /credits/{task_id}`
or `GET /users/{user_id}` for progress.

**Authentication:** send your key in the `X-API-Key` header (use **Authorize** above).

**Money:** send `amount` as a JSON number or a decimal string such as `"12.50"`.
Amounts are returned as strings so they are never rounded.

**Errors:** every error has the shape `{"error": {"code", "message", "details"?}}`.
"""

OPENAPI_TAGS = [
    {"name": "credits", "description": "Submit credits and check their status."},
    {"name": "users", "description": "Read a user's balance and processing status."},
]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open MongoDB and Redis clients, ensure indexes, and close them on shutdown.

    The rate limiter and the readiness probe get separate Redis pools, so a
    burst of probe traffic cannot starve rate limiting or the other way round.

    Args:
        app: The application whose state receives the shared resources.

    Yields:
        Control to the running application.
    """
    settings = get_settings()
    mongo = make_async_client(settings)
    probe_redis = Redis.from_url(settings.redis_url, socket_timeout=2, max_connections=4)
    limiter_redis = Redis.from_url(settings.redis_url, socket_timeout=1, max_connections=32)
    await ensure_indexes_async(mongo[settings.mongo_db])

    app.state.repository = AsyncCreditRepository(mongo, settings.mongo_db, settings.mongo_timeout_s)
    app.state.publisher = CeleryCreditPublisher()
    app.state.rate_limiter = RateLimiter(limiter_redis, settings.rate_limit_per_minute)
    app.state.readiness = Readiness(
        {"mongo": lambda: mongo.admin.command("ping"), "redis": probe_redis.ping}
    )
    try:
        yield
    finally:
        await limiter_redis.aclose()
        await probe_redis.aclose()
        await mongo.close()


def create_app() -> FastAPI:
    """Build the application.

    API docs and the OpenAPI schema are served only when ``docs_enabled`` is set.

    Returns:
        The configured FastAPI app. Resources are attached in ``lifespan``.
    """
    settings = get_settings()
    configure_logging(settings.log_level)
    docs = settings.docs_enabled
    app = FastAPI(
        title="Credit Service",
        version="0.1.0",
        description=API_DESCRIPTION,
        openapi_tags=OPENAPI_TAGS,
        lifespan=lifespan,
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
    )
    app.state.api_clients = settings.api_clients()
    if not app.state.api_clients:
        structlog.get_logger(__name__).warning("auth.no_api_keys_configured")
    app.middleware("http")(request_context)
    app.add_middleware(BodySizeLimit, max_bytes=settings.max_body_bytes)
    register_error_handlers(app)
    app.include_router(router)
    app.include_router(probes)
    return app


app = create_app()
