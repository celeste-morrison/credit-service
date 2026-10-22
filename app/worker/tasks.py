from typing import Any

import structlog
from celery import Task
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure

from app.config import get_settings
from app.db import Doc, make_sync_client
from app.repository import CreditRepository
from app.worker.celery_app import celery

log = structlog.get_logger(__name__)

_client: MongoClient[Doc] | None = None


def get_repository() -> CreditRepository:
    """Per-process repository, created lazily so each forked worker gets its own client."""
    global _client
    settings = get_settings()
    if _client is None:
        _client = make_sync_client(settings)
    return CreditRepository(_client, settings.mongo_db)


class CreditTask(Task):  # type: ignore[type-arg]
    def on_failure(self, exc: Exception, task_id: str, args: Any, kwargs: Any, einfo: Any) -> None:
        """Retries exhausted or non-retryable error: settle the credit as failed."""
        log.error("credit.failed", credit_id=task_id, error=repr(exc))
        try:
            get_repository().mark_failed(task_id, f"{type(exc).__name__}: {exc}")
        except Exception:
            # Credit stays pending; a reconciliation sweep is the backstop.
            log.exception("credit.mark_failed_error", credit_id=task_id)


@celery.task(
    bind=True,
    base=CreditTask,
    name="credits.apply",
    autoretry_for=(ConnectionFailure,),
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    max_retries=get_settings().task_max_retries,
)
def apply_credit(self: CreditTask, credit_id: str) -> str:
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(credit_id=credit_id)
    result = get_repository().apply_credit(credit_id)
    log.info("credit.apply", result=result, attempt=self.request.retries)
    return result.value
