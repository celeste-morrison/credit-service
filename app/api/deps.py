"""FastAPI dependencies: shared resources from app state, and API-key auth."""

import hmac
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import APIKeyHeader

from app.api.errors import ApiError
from app.api.health import Readiness
from app.publisher import CreditPublisher
from app.repository import AsyncCreditRepository

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def get_repository(request: Request) -> AsyncCreditRepository:
    """Return the repository created at startup.

    Args:
        request: The incoming request.

    Returns:
        The shared API-side repository.
    """
    repo: AsyncCreditRepository = request.app.state.repository
    return repo


def get_publisher(request: Request) -> CreditPublisher:
    """Return the publisher created at startup.

    Args:
        request: The incoming request.

    Returns:
        The shared credit publisher.
    """
    publisher: CreditPublisher = request.app.state.publisher
    return publisher


def get_readiness(request: Request) -> Readiness:
    """Return the readiness probe created at startup.

    Args:
        request: The incoming request.

    Returns:
        The shared readiness probe.
    """
    readiness: Readiness = request.app.state.readiness
    return readiness


def require_api_key(
    request: Request,
    x_api_key: Annotated[str | None, Depends(_api_key_header)],
) -> None:
    """Reject the request unless it carries a configured API key.

    Keys are compared in constant time. With no keys configured every request
    is rejected.

    Args:
        request: The incoming request.
        x_api_key: Value of the ``X-API-Key`` header.

    Raises:
        ApiError: 401 if the key is missing or unknown.
    """
    keys: frozenset[str] = request.app.state.api_keys
    supplied = (x_api_key or "").encode()
    if not supplied or not any(hmac.compare_digest(supplied, key.encode()) for key in keys):
        raise ApiError(401, "unauthorized", "Missing or invalid X-API-Key")
