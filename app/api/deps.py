"""FastAPI dependencies: shared resources from app state, and API-key auth."""

import hmac
from typing import Annotated

import structlog
from fastapi import Depends, Request
from fastapi.security import APIKeyHeader

from app.api.errors import ApiError
from app.api.health import Readiness
from app.api.ratelimit import RateLimiter
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
) -> str:
    """Authenticate the caller by API key.

    Keys are compared in constant time, and every configured key is compared
    so the time taken does not reveal which one matched. With no keys
    configured every request is rejected.

    Args:
        request: The incoming request.
        x_api_key: Value of the ``X-API-Key`` header.

    Returns:
        The name of the client the key belongs to.

    Raises:
        ApiError: 401 if the key is missing or unknown.
    """
    clients: dict[str, str] = request.app.state.api_clients
    supplied = (x_api_key or "").encode()
    client_id = None
    for key, client in clients.items():
        if hmac.compare_digest(supplied, key.encode()):
            client_id = client
    if not supplied or client_id is None:
        raise ApiError(401, "unauthorized", "Missing or invalid X-API-Key")
    structlog.contextvars.bind_contextvars(client_id=client_id)
    return client_id


ClientId = Annotated[str, Depends(require_api_key)]


async def enforce_rate_limit(request: Request, client_id: ClientId) -> None:
    """Reject the request if its client is over its per-minute limit.

    Args:
        request: The incoming request.
        client_id: The authenticated client.

    Raises:
        ApiError: 429 with ``Retry-After`` when the limit is exceeded.
    """
    limiter: RateLimiter = request.app.state.rate_limiter
    retry_after = await limiter.retry_after(client_id)
    if retry_after is not None:
        raise ApiError(
            429,
            "rate_limited",
            "Too many requests",
            headers={"Retry-After": str(retry_after)},
        )
