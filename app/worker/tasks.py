"""Celery tasks: applying credits and reconciling stuck ones.

Failure policy:
    Transient MongoDB errors (connection loss, timeouts, transient transaction
    errors) are retried with capped exponential backoff and full jitter. If the
    retries run out, the credit is left ``pending`` rather than failed: an
    outage is not a reason to reject money the API already accepted. The
    reconciliation job re-enqueues it later.

    Any other error is a bug or bad data. The credit is marked ``failed`` with a
    stable error code, and the details go to the logs only.
"""

import random
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from billiard.exceptions import SoftTimeLimitExceeded
from celery import Task
from pymongo import MongoClient
from pymongo.errors import PyMongoError

from app.config import get_settings
from app.db import Doc, is_transient_error, make_sync_client
from app.repository import ApplyResult, CreditRepository
from app.worker.celery_app import celery

log = structlog.get_logger(__name__)

PROCESSING_ERROR = "processing_error"
"""Error code stored on credits that failed for a non-transient reason."""

RECONCILE_BATCH = 500
"""Maximum credits re-enqueued per reconciliation run."""

_client: MongoClient[Doc] | None = None

_jitter = random.SystemRandom()


def get_repository() -> CreditRepository:
    """Return a repository on this process's MongoDB client.

    The client is created lazily, so each forked worker process gets its own.

    Returns:
        A worker-side repository.
    """
    global _client
    settings = get_settings()
    if _client is None:
        _client = make_sync_client(settings)
    return CreditRepository(_client, settings.mongo_db, settings.mongo_timeout_s)


def is_transient(exc: BaseException) -> bool:
    """Decide whether a task error is worth retrying.

    Args:
        exc: The raised exception.

    Returns:
        True for transient MongoDB errors and soft time-limit expiry.
    """
    return isinstance(exc, SoftTimeLimitExceeded) or is_transient_error(exc)


def retry_countdown(retries: int, cap_s: int) -> float:
    """Exponential backoff with full jitter.

    Args:
        retries: Retries already attempted.
        cap_s: Maximum delay in seconds.

    Returns:
        Seconds to wait before the next attempt.
    """
    return _jitter.uniform(0, min(cap_s, 2**retries))


def enqueue(credit_id: str) -> None:
    """Publish a credit for the worker.

    The credit id doubles as the Celery task id: one id to poll, log and dedupe.

    Args:
        credit_id: The credit to apply.

    Raises:
        kombu.exceptions.OperationalError: The broker is unreachable.
    """
    apply_credit.apply_async(
        args=(credit_id,),
        task_id=credit_id,
        retry=True,
        retry_policy={"max_retries": 3, "interval_start": 0, "interval_step": 0.2},
    )


class CreditTask(Task):
    """Base task that settles credits whose processing failed permanently."""

    def on_failure(self, exc: Exception, task_id: str, args: Any, kwargs: Any, einfo: Any) -> None:
        """Mark the credit failed, unless the error was transient.

        Args:
            exc: The final exception.
            task_id: Celery task id.
            args: Task positional arguments; the first is the credit id.
            kwargs: Task keyword arguments.
            einfo: Exception info.
        """
        credit_id = args[0]
        if is_transient(exc):
            log.warning("credit.retries_exhausted", credit_id=credit_id, error=repr(exc))
            return
        log.error("credit.failed", credit_id=credit_id, error=repr(exc))
        try:
            get_repository().mark_failed(credit_id, PROCESSING_ERROR)
        except PyMongoError:
            log.exception("credit.mark_failed_error", credit_id=credit_id)


@celery.task(bind=True, base=CreditTask, name="credits.apply")
def apply_credit(self: CreditTask, credit_id: str) -> str:
    """Apply one credit to its user's balance.

    Args:
        self: The bound task, used for retries.
        credit_id: The credit to apply.

    Returns:
        The ``ApplyResult`` value.

    Raises:
        celery.exceptions.Retry: On a transient error, to schedule another attempt.
    """
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(credit_id=credit_id)
    settings = get_settings()
    try:
        result = get_repository().apply_credit(credit_id)
    except Exception as exc:
        if not is_transient(exc):
            raise
        raise self.retry(
            exc=exc,
            countdown=retry_countdown(self.request.retries, settings.task_retry_backoff_max_s),
            max_retries=settings.task_max_retries,
        ) from exc
    if result is ApplyResult.NOT_FOUND:
        log.warning("credit.apply", result=result, attempt=self.request.retries)
    else:
        log.info("credit.apply", result=result, attempt=self.request.retries)
    return result.value


@celery.task(name="credits.reconcile")
def reconcile_pending() -> int:
    """Re-enqueue credits that have stayed ``pending`` too long.

    This covers every way a credit can be accepted but never applied: the API
    dying between commit and publish, Redis losing un-persisted messages, or a
    worker killed past its time limit. Re-enqueueing is safe because applying
    is idempotent.

    Returns:
        Number of credits re-enqueued.
    """
    settings = get_settings()
    cutoff = datetime.now(UTC) - timedelta(seconds=settings.reconcile_after_s)
    stale = get_repository().find_stale_pending(cutoff, RECONCILE_BATCH)
    for credit_id in stale:
        enqueue(credit_id)
    if stale:
        log.warning("credit.reconciled", count=len(stale))
    return len(stale)
