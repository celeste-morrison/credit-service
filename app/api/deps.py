from collections.abc import Awaitable, Callable

from fastapi import Request

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
