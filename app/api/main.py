"""FastAPI application factory and lifespan."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from redis.asyncio import Redis

from app.api.errors import register_error_handlers
from app.api.health import Readiness
from app.api.middleware import BodySizeLimit, request_context
from app.api.routes import probes, router
from app.config import get_settings
from app.db import ensure_indexes_async, make_async_client
from app.logging import configure_logging
from app.publisher import CeleryCreditPublisher
from app.repository import AsyncCreditRepository


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open MongoDB and Redis clients, ensure indexes, and close them on shutdown.

    Args:
        app: The application whose state receives the shared resources.

    Yields:
        Control to the running application.
    """
    settings = get_settings()
    mongo = make_async_client(settings)
    redis = Redis.from_url(settings.redis_url, socket_timeout=2, max_connections=4)
    await ensure_indexes_async(mongo[settings.mongo_db])

    app.state.repository = AsyncCreditRepository(mongo, settings.mongo_db, settings.mongo_timeout_s)
    app.state.publisher = CeleryCreditPublisher()
    app.state.readiness = Readiness(
        {"mongo": lambda: mongo.admin.command("ping"), "redis": redis.ping}
    )
    try:
        yield
    finally:
        await redis.aclose()
        await mongo.close()


def create_app() -> FastAPI:
    """Build the application.

    Returns:
        The configured FastAPI app. Resources are attached in ``lifespan``.
    """
    settings = get_settings()
    configure_logging(settings.log_level)
    app = FastAPI(title="Credit Service", version="0.1.0", lifespan=lifespan)
    app.state.api_keys = settings.api_key_set()
    if not app.state.api_keys:
        structlog.get_logger(__name__).warning("auth.no_api_keys_configured")
    app.middleware("http")(request_context)
    app.add_middleware(BodySizeLimit, max_bytes=settings.max_body_bytes)
    register_error_handlers(app)
    app.include_router(router)
    app.include_router(probes)
    return app


app = create_app()
