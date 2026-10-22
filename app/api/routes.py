import asyncio
from typing import Annotated, Any

import anyio
import structlog
from fastapi import APIRouter, Depends, Header, Response
from fastapi.responses import JSONResponse

from app.api.deps import ReadinessChecks, get_publisher, get_readiness_checks, get_repository
from app.api.errors import ApiError
from app.domain import CreditAccepted, CreditRequest, CreditView, UserId, UserView
from app.publisher import CreditPublisher
from app.repository import AsyncCreditRepository, IdempotencyConflictError

log = structlog.get_logger(__name__)
router = APIRouter()

Repo = Annotated[AsyncCreditRepository, Depends(get_repository)]
Publisher = Annotated[CreditPublisher, Depends(get_publisher)]

READINESS_TIMEOUT_S = 2.0


@router.post("/credits", status_code=202, response_model=CreditAccepted)
async def create_credit(
    body: CreditRequest,
    response: Response,
    repo: Repo,
    publisher: Publisher,
    idempotency_key: Annotated[str | None, Header(min_length=1, max_length=255)] = None,
) -> CreditAccepted:
    """Accept a credit for asynchronous processing; poll `Location` for its status."""
    try:
        credit, created = await repo.create_pending_credit(body, idempotency_key)
    except IdempotencyConflictError as exc:
        raise ApiError(
            409, "idempotency_conflict", "Idempotency-Key was already used with a different body"
        ) from exc

    if created:
        try:
            # Kombu publishing is blocking; keep it off the event loop.
            await anyio.to_thread.run_sync(publisher.publish, credit.task_id)
        except Exception as exc:
            log.error("credit.publish_failed", credit_id=credit.task_id, error=repr(exc))
            await repo.mark_failed(credit.task_id, f"publish failed: {type(exc).__name__}")
            raise ApiError(503, "queue_unavailable", "Could not enqueue credit; retry") from exc
        log.info("credit.accepted", credit_id=credit.task_id, user_id=credit.user_id)

    response.headers["Location"] = f"/credits/{credit.task_id}"
    return CreditAccepted(task_id=credit.task_id, status=credit.status)


@router.get("/credits/{task_id}", response_model=CreditView)
async def get_credit(task_id: str, repo: Repo) -> CreditView:
    credit = await repo.get_credit(task_id)
    if credit is None:
        raise ApiError(404, "credit_not_found", "Credit not found")
    return credit


@router.get("/users/{user_id}", response_model=UserView)
async def get_user(user_id: UserId, repo: Repo) -> UserView:
    user = await repo.get_user(user_id)
    if user is None:
        raise ApiError(404, "user_not_found", "User not found")
    return user


@router.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz", include_in_schema=False)
async def readyz(
    checks: Annotated[ReadinessChecks, Depends(get_readiness_checks)],
) -> JSONResponse:
    async def run(check: Any) -> str:
        try:
            await asyncio.wait_for(check(), READINESS_TIMEOUT_S)
            return "ok"
        except Exception as exc:
            return f"error: {type(exc).__name__}"

    names = list(checks)
    results = dict(zip(names, await asyncio.gather(*(run(checks[n]) for n in names)), strict=True))
    ready = all(r == "ok" for r in results.values())
    return JSONResponse(
        status_code=200 if ready else 503,
        content={"status": "ok" if ready else "unavailable", "checks": results},
    )
