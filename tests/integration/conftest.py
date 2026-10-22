from collections.abc import AsyncIterator, Iterator
from uuid import uuid4

import pytest
from pymongo import AsyncMongoClient, MongoClient

from app.config import Settings, get_settings
from app.db import Doc, ensure_indexes, make_async_client, make_sync_client
from app.repository import AsyncCreditRepository, CreditRepository


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "/integration/" in str(item.path):
            item.add_marker(pytest.mark.integration)


@pytest.fixture(scope="session")
def settings() -> Settings:
    return get_settings()


@pytest.fixture(scope="session")
def sync_client(settings: Settings) -> Iterator[MongoClient[Doc]]:
    client = make_sync_client(settings)
    client.admin.command("ping")
    yield client
    client.close()


@pytest.fixture
def db_name(sync_client: MongoClient[Doc]) -> Iterator[str]:
    """A fresh, indexed database per test."""
    name = f"credits_test_{uuid4().hex[:12]}"
    ensure_indexes(sync_client[name])
    yield name
    sync_client.drop_database(name)


@pytest.fixture
async def async_client(settings: Settings) -> AsyncIterator[AsyncMongoClient[Doc]]:
    client = make_async_client(settings)
    yield client
    await client.close()


@pytest.fixture
def api_repo(async_client: AsyncMongoClient[Doc], db_name: str) -> AsyncCreditRepository:
    return AsyncCreditRepository(async_client, db_name)


@pytest.fixture
def worker_repo(sync_client: MongoClient[Doc], db_name: str) -> CreditRepository:
    return CreditRepository(sync_client, db_name)
