import hmac
from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import APIKeyHeader

from app.api.errors import ApiError
from app.publisher import CreditPublisher
from app.repository import AsyncCreditRepository

ReadinessChecks = dict[str, Callable[[], Awaitable[object]]]


def get_repository(request: Request) -> AsyncCreditRepository:
    repo: AsyncCreditRepository = request.app.state.repository
    return repo


def get_publisher(request: Request) -> CreditPublisher:
    publisher: CreditPublisher = request.app.state.publisher
    return publisher


def get_readiness_checks(request: Request) -> ReadinessChecks:
    checks: ReadinessChecks = request.app.state.readiness_checks
    return checks


_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_api_key(
    request: Request,
    x_api_key: Annotated[str | None, Depends(_api_key_header)],
) -> None:
    keys: frozenset[str] = request.app.state.api_keys
    if x_api_key is None or not any(
        hmac.compare_digest(x_api_key.encode(), key.encode()) for key in keys
    ):
        raise ApiError(401, "unauthorized", "Missing or invalid X-API-Key")
