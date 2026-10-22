"""Fixtures for end-to-end tests against the running docker compose stack."""

import os
from collections.abc import Iterator

import httpx
import pytest

from app.config import get_settings


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark every test in this directory as ``e2e``."""
    for item in items:
        if "/e2e/" in str(item.path):
            item.add_marker(pytest.mark.e2e)


@pytest.fixture(scope="session")
def api() -> Iterator[httpx.Client]:
    """Client for the API running in docker compose (`make up`)."""
    base_url = os.environ.get("API_URL", f"http://localhost:{os.environ.get('API_PORT', '8000')}")
    api_key = min(get_settings().api_key_set() or {"dev-local-key"})
    with httpx.Client(base_url=base_url, headers={"X-API-Key": api_key}, timeout=5) as client:
        client.get("/readyz").raise_for_status()
        yield client
