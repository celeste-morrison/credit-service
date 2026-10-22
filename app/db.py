"""MongoDB client factories and index definitions.

The API uses PyMongo's native async client; the Celery worker uses the sync client.
"""

from typing import Any

from pymongo import ASCENDING, DESCENDING, AsyncMongoClient, IndexModel, MongoClient
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.database import Database

from app.config import Settings

Doc = dict[str, Any]

USERS = "users"
CREDITS = "credits"

INDEXES: dict[str, list[IndexModel]] = {
    USERS: [IndexModel([("user_id", ASCENDING)], unique=True, name="user_id_unique")],
    CREDITS: [
        IndexModel(
            [("idempotency_key", ASCENDING)],
            unique=True,
            partialFilterExpression={"idempotency_key": {"$exists": True}},
            name="idempotency_key_unique",
        ),
        IndexModel([("user_id", ASCENDING), ("created_at", DESCENDING)], name="user_created"),
    ],
}


def _client_options(settings: Settings) -> Doc:
    return {
        "tz_aware": True,
        "serverSelectionTimeoutMS": settings.mongo_timeout_ms,
        "appname": "credit-service",
    }


def make_sync_client(settings: Settings) -> MongoClient[Doc]:
    return MongoClient(settings.mongo_url, **_client_options(settings))


def make_async_client(settings: Settings) -> AsyncMongoClient[Doc]:
    return AsyncMongoClient(settings.mongo_url, **_client_options(settings))


def ensure_indexes(db: Database[Doc]) -> None:
    for name, indexes in INDEXES.items():
        db[name].create_indexes(indexes)


async def ensure_indexes_async(db: AsyncDatabase[Doc]) -> None:
    for name, indexes in INDEXES.items():
        await db[name].create_indexes(indexes)
