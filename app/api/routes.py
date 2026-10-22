"""HTTP routes.

``router`` holds the credit and user endpoints, which require an API key.
``probes`` holds the unauthenticated health endpoints.
"""

from typing import Annotated

import anyio
import structlog
from fastapi import APIRouter, Depends, Header, Response
from fastapi.responses import JSONResponse

from app.api.deps import get_publisher, get_readiness, get_repository, require_api_key
from app.api.errors import ApiError
from app.api.health import Readiness
from app.domain import CreditAccepted, CreditRequest, CreditView, UserId, UserView
from app.publisher import CreditPublisher
from app.repository import AsyncCreditRepository, IdempotencyConflictError

log = structlog.get_logger(__name__)

router = APIRouter(dependencies=[Depends(require_api_key)])
probes = APIRouter()

Repo = Annotated[AsyncCreditRepository, Depends(get_repository)]
Publisher = Annotated[CreditPublisher, Depends(get_publisher)]


@router.post("/credits", status_code=202, response_model=CreditAccepted)
async def create_credit(
    body: CreditRequest,
    response: Response,
    repo: Repo,
    publisher: Publisher,
    idempotency_key: Annotated[str | None, Header(min_length=1, max_length=255)] = None,
) -> CreditAccepted:
    """Accept a credit for asynchronous processing.

    The credit is recorded as pending and the user marked ``processing`` before
    the task is enqueued. Poll the ``Location`` header for progress.

    Replaying an ``Idempotency-Key`` for the same user and amount returns the
    original credit and its current status without enqueueing again. If the
    original could not be enqueued (503), the key is freed and the retry creates
    a new credit.

    Args:
        body: The credit to apply.
        response: Used to set the ``Location`` header.
        repo: The API-side repository.
        publisher: Hands new credits to the worker.
        idempotency_key: Optional ``Idempotency-Key`` header, unique per user.

    Returns:
        The credit's task id and current status.

    Raises:
        ApiError: 409 on an idempotency conflict, 503 if the queue is down.
    """
    try:
        credit, created = await repo.create_pending_credit(body, idempotency_key)
    except IdempotencyConflictError as exc:
        raise ApiError(
            409, "idempotency_conflict", "Idempotency-Key was already used with a different amount"
        ) from exc

    if created:
        try:
            await anyio.to_thread.run_sync(publisher.publish, credit.task_id)
        except Exception as exc:
            log.error("credit.publish_failed", credit_id=credit.task_id, error=repr(exc))
            await repo.fail_unpublished(credit.task_id)
            raise ApiError(503, "queue_unavailable", "Could not enqueue credit; retry") from exc
        log.info("credit.accepted", credit_id=credit.task_id, user_id=credit.user_id)

    response.headers["Location"] = f"/credits/{credit.task_id}"
    return CreditAccepted(task_id=credit.task_id, status=credit.status)


@router.get("/credits/{task_id}", response_model=CreditView)
async def get_credit(task_id: str, repo: Repo) -> CreditView:
    """Return a credit's status.

    Args:
        task_id: The id returned by ``POST /credits``.
        repo: The API-side repository.

    Returns:
        The credit.

    Raises:
        ApiError: 404 if the credit does not exist.
    """
    credit = await repo.get_credit(task_id)
    if credit is None:
        raise ApiError(404, "credit_not_found", "Credit not found")
    return credit


@router.get("/users/{user_id}", response_model=UserView)
async def get_user(user_id: UserId, repo: Repo) -> UserView:
    """Return a user's balance and status.

    Args:
        user_id: The user id.
        repo: The API-side repository.

    Returns:
        The user.

    Raises:
        ApiError: 404 if the user does not exist.
    """
    user = await repo.get_user(user_id)
    if user is None:
        raise ApiError(404, "user_not_found", "User not found")
    return user


@probes.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    """Liveness: the process is up. Checks no dependencies.

    Returns:
        A constant ``{"status": "ok"}``.
    """
    return {"status": "ok"}


@probes.get("/readyz", include_in_schema=False)
async def readyz(readiness: Annotated[Readiness, Depends(get_readiness)]) -> JSONResponse:
    """Readiness: MongoDB and Redis are reachable.

    Args:
        readiness: The cached dependency checks.

    Returns:
        200 with each check's result, or 503 if any check failed.
    """
    checks = await readiness.results()
    ready = all(result == "ok" for result in checks.values())
    return JSONResponse(
        status_code=200 if ready else 503,
        content={"status": "ok" if ready else "unavailable", "checks": checks},
    )
