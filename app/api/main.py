from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from redis.asyncio import Redis

from app.api.errors import register_error_handlers
from app.api.middleware import request_context
from app.api.routes import probes, router
from app.config import get_settings
from app.db import ensure_indexes_async, make_async_client
from app.logging import configure_logging
from app.publisher import CeleryCreditPublisher
from app.repository import AsyncCreditRepository


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    mongo = make_async_client(settings)
    redis = Redis.from_url(settings.redis_url, socket_timeout=2)
    await ensure_indexes_async(mongo[settings.mongo_db])

    app.state.repository = AsyncCreditRepository(mongo, settings.mongo_db)
    app.state.publisher = CeleryCreditPublisher()
    app.state.readiness_checks = {
        "mongo": lambda: mongo.admin.command("ping"),
        "redis": redis.ping,
    }
    try:
        yield
    finally:
        await redis.aclose()
        await mongo.close()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    app = FastAPI(title="Credit Service", version="0.1.0", lifespan=lifespan)
    app.state.api_keys = settings.api_key_set()
    if not app.state.api_keys:
        structlog.get_logger(__name__).warning("auth.no_api_keys_configured")
    app.middleware("http")(request_context)
    register_error_handlers(app)
    app.include_router(router)
    app.include_router(probes)
    return app


app = create_app()
