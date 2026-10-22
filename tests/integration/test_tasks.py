"""Celery task behaviour: retries, failure policy and reconciliation.

Tasks run eagerly with ``Task.apply``, against the real repository on the
per-test database. Eager mode is fine here because what is under test is the
task's own error handling; broker delivery is covered by the e2e tests.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pymongo import MongoClient
from pymongo.errors import AutoReconnect, OperationFailure

from app.db import CREDITS, Doc
from app.domain import CreditStatus, UserStatus
from app.repository import ApplyResult, AsyncCreditRepository, CreditRepository
from app.worker import tasks
from tests.integration.helpers import credit_request, fetch_credit, fetch_user


class FlakyRepository:
    """Wraps a repository; ``apply_credit`` raises ``error`` for the first ``failures`` calls."""

    def __init__(self, inner: CreditRepository, error: Exception, failures: int) -> None:
        self.inner = inner
        self.error = error
        self.failures = failures
        self.calls = 0

    def apply_credit(self, credit_id: str) -> ApplyResult:
        """Fail, then delegate."""
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error
        return self.inner.apply_credit(credit_id)

    def mark_failed(self, credit_id: str, error: str) -> bool:
        """Delegate."""
        return self.inner.mark_failed(credit_id, error)


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tasks, "retry_countdown", lambda retries, cap_s: 0)


def use_repository(monkeypatch: pytest.MonkeyPatch, repo: object) -> None:
    """Point the tasks at ``repo`` instead of the configured database."""
    monkeypatch.setattr(tasks, "get_repository", lambda: repo)


async def pending_credit(api_repo: AsyncCreditRepository, amount: str = "10.00") -> str:
    """Create a pending credit for ``u_123`` and return its id."""
    created, _ = await api_repo.create_pending_credit(credit_request(amount=amount))
    return created.task_id


async def test_task_applies_credit(
    api_repo: AsyncCreditRepository,
    worker_repo: CreditRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_repository(monkeypatch, worker_repo)
    credit_id = await pending_credit(api_repo)

    result = tasks.apply_credit.apply(args=(credit_id,))

    assert result.get() == "applied"
    user = await fetch_user(api_repo)
    assert user.balance == Decimal("10.00")


async def test_transient_error_is_retried_then_applied(
    api_repo: AsyncCreditRepository,
    worker_repo: CreditRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flaky = FlakyRepository(worker_repo, AutoReconnect("primary stepped down"), failures=2)
    use_repository(monkeypatch, flaky)
    credit_id = await pending_credit(api_repo)

    result = tasks.apply_credit.apply(args=(credit_id,))

    assert result.get() == "applied"
    assert flaky.calls == 3


async def test_exhausted_transient_retries_leave_credit_pending(
    api_repo: AsyncCreditRepository,
    worker_repo: CreditRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flaky = FlakyRepository(worker_repo, AutoReconnect("mongo down"), failures=1000)
    use_repository(monkeypatch, flaky)
    credit_id = await pending_credit(api_repo)

    result = tasks.apply_credit.apply(args=(credit_id,))

    assert result.failed()
    credit = await fetch_credit(api_repo, credit_id)
    assert credit.status == CreditStatus.PENDING


async def test_permanent_error_marks_credit_failed_with_stable_code(
    api_repo: AsyncCreditRepository,
    worker_repo: CreditRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flaky = FlakyRepository(
        worker_repo, OperationFailure("secret internal detail", code=2), failures=1
    )
    use_repository(monkeypatch, flaky)
    credit_id = await pending_credit(api_repo)

    result = tasks.apply_credit.apply(args=(credit_id,), task_id="unrelated-task-id")

    assert result.failed()
    assert flaky.calls == 1
    credit = await fetch_credit(api_repo, credit_id)
    assert credit.status == CreditStatus.FAILED
    assert credit.error == "processing_error"
    user = await fetch_user(api_repo)
    assert user.status == UserStatus.PROCESSED


async def test_reconcile_reenqueues_only_stale_pending_credits(
    api_repo: AsyncCreditRepository,
    worker_repo: CreditRepository,
    sync_client: MongoClient[Doc],
    db_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_repository(monkeypatch, worker_repo)
    enqueued: list[str] = []
    monkeypatch.setattr(tasks, "enqueue", enqueued.append)
    stale = await pending_credit(api_repo)
    await pending_credit(api_repo)
    sync_client[db_name][CREDITS].update_one(
        {"_id": stale}, {"$set": {"created_at": datetime.now(UTC) - timedelta(hours=1)}}
    )

    assert tasks.reconcile_pending() == 1
    assert enqueued == [stale]


@pytest.mark.parametrize(
    ("error", "transient"),
    [
        (AutoReconnect("x"), True),
        (OperationFailure("x", code=2), False),
        (ValueError("x"), False),
    ],
)
def test_is_transient(error: Exception, transient: bool) -> None:
    assert tasks.is_transient(error) is transient
