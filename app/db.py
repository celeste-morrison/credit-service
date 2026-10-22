"""MongoDB client factories, collection names and index definitions.

The API uses PyMongo's native async client; the Celery worker uses the sync
client. Both share the same options and indexes.
"""

from typing import Any

from pymongo import ASCENDING, AsyncMongoClient, IndexModel, MongoClient
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.database import Database
from pymongo.errors import ConnectionFailure, PyMongoError

from app.config import Settings

Doc = dict[str, Any]

USERS = "users"
CREDITS = "credits"

INDEXES: dict[str, list[IndexModel]] = {
    USERS: [IndexModel([("user_id", ASCENDING)], unique=True, name="user_id_unique")],
    CREDITS: [
        IndexModel(
            [("client_id", ASCENDING), ("user_id", ASCENDING), ("idempotency_key", ASCENDING)],
            unique=True,
            partialFilterExpression={"idempotency_key": {"$exists": True}},
            name="client_user_idempotency_key_unique",
        ),
        IndexModel(
            [("created_at", ASCENDING)],
            partialFilterExpression={"idempotency_key": {"$exists": True}},
            name="idempotency_key_created",
        ),
        IndexModel([("status", ASCENDING), ("created_at", ASCENDING)], name="status_created"),
    ],
}
"""Indexes per collection.

Idempotency keys are unique per API client and user, so two callers that happen
to choose the same key never collide. ``idempotency_key_created`` serves the job
that expires old keys, and ``status_created`` serves the reconciliation scan for
stale pending credits.
"""


def _client_options(settings: Settings) -> Doc:
    return {
        "tz_aware": True,
        "serverSelectionTimeoutMS": settings.mongo_timeout_ms,
        "appname": "credit-service",
    }


def make_sync_client(settings: Settings) -> MongoClient[Doc]:
    """Create a synchronous client for the worker.

    Args:
        settings: Service settings.

    Returns:
        A configured ``MongoClient``.
    """
    return MongoClient(settings.mongo_url, **_client_options(settings))


def make_async_client(settings: Settings) -> AsyncMongoClient[Doc]:
    """Create an asynchronous client for the API.

    Args:
        settings: Service settings.

    Returns:
        A configured ``AsyncMongoClient``.
    """
    return AsyncMongoClient(settings.mongo_url, **_client_options(settings))


def ensure_indexes(db: Database[Doc]) -> None:
    """Create all indexes if missing (idempotent).

    Args:
        db: Target database.
    """
    for name, indexes in INDEXES.items():
        db[name].create_indexes(indexes)


async def ensure_indexes_async(db: AsyncDatabase[Doc]) -> None:
    """Create all indexes if missing (idempotent).

    Args:
        db: Target database.
    """
    for name, indexes in INDEXES.items():
        await db[name].create_indexes(indexes)


def is_transient_error(exc: BaseException) -> bool:
    """Decide whether a MongoDB error is likely to succeed on retry.

    Args:
        exc: The raised exception.

    Returns:
        True for connection failures, timeouts and transient transaction errors.
    """
    if isinstance(exc, ConnectionFailure):
        return True
    if isinstance(exc, PyMongoError):
        return exc.timeout or exc.has_error_label("TransientTransactionError")
    return False
