"""Fixtures for tests that run against the real MongoDB from docker compose."""

from collections.abc import AsyncIterator, Iterator
from uuid import uuid4

import pytest
from pymongo import AsyncMongoClient, MongoClient

from app.config import Settings, get_settings
from app.db import Doc, ensure_indexes, make_async_client, make_sync_client
from app.repository import AsyncCreditRepository, CreditRepository

TIMEOUT_S = 5.0


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark every test in this directory as ``integration``."""
    for item in items:
        if "/integration/" in str(item.path):
            item.add_marker(pytest.mark.integration)


@pytest.fixture(scope="session")
def settings() -> Settings:
    """Settings from the environment and ``.env``."""
    return get_settings()


@pytest.fixture(scope="session")
def sync_client(settings: Settings) -> Iterator[MongoClient[Doc]]:
    """A shared sync client; fails fast if MongoDB is not running."""
    client = make_sync_client(settings)
    client.admin.command("ping")
    yield client
    client.close()


@pytest.fixture
def db_name(sync_client: MongoClient[Doc]) -> Iterator[str]:
    """Create a fresh, indexed database per test and drop it afterwards."""
    name = f"credits_test_{uuid4().hex[:12]}"
    ensure_indexes(sync_client[name])
    yield name
    sync_client.drop_database(name)


@pytest.fixture
async def async_client(settings: Settings) -> AsyncIterator[AsyncMongoClient[Doc]]:
    """An async client bound to the test's event loop."""
    client = make_async_client(settings)
    yield client
    await client.close()


@pytest.fixture
def api_repo(async_client: AsyncMongoClient[Doc], db_name: str) -> AsyncCreditRepository:
    """The API-side repository on the per-test database."""
    return AsyncCreditRepository(async_client, db_name, TIMEOUT_S)


@pytest.fixture
def worker_repo(sync_client: MongoClient[Doc], db_name: str) -> CreditRepository:
    """The worker-side repository on the per-test database."""
    return CreditRepository(sync_client, db_name, TIMEOUT_S)
