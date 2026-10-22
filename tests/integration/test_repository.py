import asyncio
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from pymongo import MongoClient

from app.db import USERS, Doc
from app.domain import CreditRequest, CreditStatus, UserStatus
from app.repository import (
    ApplyResult,
    AsyncCreditRepository,
    CreditRepository,
    IdempotencyConflictError,
)


def credit(user_id: str = "u_123", amount: str = "50.00") -> CreditRequest:
    return CreditRequest(user_id=user_id, amount=Decimal(amount))


async def test_create_pending_credit_marks_user_processing(
    api_repo: AsyncCreditRepository,
) -> None:
    created, is_new = await api_repo.create_pending_credit(credit())

    assert is_new
    assert created.status == CreditStatus.PENDING
    user = await api_repo.get_user("u_123")
    assert user is not None
    assert user.status == UserStatus.PROCESSING
    assert user.pending_credits == 1


async def test_balance_field_is_created_by_first_apply(
    api_repo: AsyncCreditRepository,
    worker_repo: CreditRepository,
    sync_client: MongoClient[Doc],
    db_name: str,
) -> None:
    created, _ = await api_repo.create_pending_credit(credit(amount="150.00"))
    raw = sync_client[db_name][USERS].find_one({"user_id": "u_123"})
    assert raw is not None and "balance" not in raw

    assert worker_repo.apply_credit(created.task_id) == ApplyResult.APPLIED

    user = await api_repo.get_user("u_123")
    assert user is not None
    assert user.balance == Decimal("150.00")
    assert user.status == UserStatus.PROCESSED
    assert user.pending_credits == 0
    applied = await api_repo.get_credit(created.task_id)
    assert applied is not None
    assert applied.status == CreditStatus.APPLIED
    assert applied.applied_at is not None


async def test_apply_adds_to_existing_balance(
    api_repo: AsyncCreditRepository, worker_repo: CreditRepository
) -> None:
    for amount in ("100.00", "50.25"):
        created, _ = await api_repo.create_pending_credit(credit(amount=amount))
        worker_repo.apply_credit(created.task_id)

    user = await api_repo.get_user("u_123")
    assert user is not None
    assert user.balance == Decimal("150.25")


async def test_apply_twice_credits_once(
    api_repo: AsyncCreditRepository, worker_repo: CreditRepository
) -> None:
    """A redelivered task (at-least-once broker) must not double-credit."""
    created, _ = await api_repo.create_pending_credit(credit(amount="50.00"))

    assert worker_repo.apply_credit(created.task_id) == ApplyResult.APPLIED
    assert worker_repo.apply_credit(created.task_id) == ApplyResult.ALREADY_SETTLED

    user = await api_repo.get_user("u_123")
    assert user is not None
    assert user.balance == Decimal("50.00")
    assert user.pending_credits == 0


async def test_concurrent_duplicate_deliveries_credit_once(
    api_repo: AsyncCreditRepository, worker_repo: CreditRepository
) -> None:
    created, _ = await api_repo.create_pending_credit(credit(amount="10.00"))

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(worker_repo.apply_credit, [created.task_id] * 8))

    assert results.count(ApplyResult.APPLIED) == 1
    user = await api_repo.get_user("u_123")
    assert user is not None
    assert user.balance == Decimal("10.00")


async def test_concurrent_credits_for_one_user_sum_exactly(
    api_repo: AsyncCreditRepository, worker_repo: CreditRepository
) -> None:
    amounts = [Decimal("0.10") + Decimal(i) / 100 for i in range(50)]
    created = await asyncio.gather(
        *(api_repo.create_pending_credit(credit(amount=str(a))) for a in amounts)
    )

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(worker_repo.apply_credit, [c.task_id for c, _ in created]))

    assert results == [ApplyResult.APPLIED] * 50
    user = await api_repo.get_user("u_123")
    assert user is not None
    assert user.balance == sum(amounts)
    assert user.pending_credits == 0
    assert user.status == UserStatus.PROCESSED


async def test_status_stays_processing_until_last_credit_settles(
    api_repo: AsyncCreditRepository, worker_repo: CreditRepository
) -> None:
    first, _ = await api_repo.create_pending_credit(credit(amount="1.00"))
    second, _ = await api_repo.create_pending_credit(credit(amount="2.00"))

    worker_repo.apply_credit(first.task_id)
    user = await api_repo.get_user("u_123")
    assert user is not None
    assert user.status == UserStatus.PROCESSING
    assert user.pending_credits == 1

    worker_repo.apply_credit(second.task_id)
    user = await api_repo.get_user("u_123")
    assert user is not None
    assert user.status == UserStatus.PROCESSED


async def test_mark_failed_releases_user_without_changing_balance(
    api_repo: AsyncCreditRepository, worker_repo: CreditRepository
) -> None:
    ok, _ = await api_repo.create_pending_credit(credit(amount="5.00"))
    worker_repo.apply_credit(ok.task_id)
    bad, _ = await api_repo.create_pending_credit(credit(amount="99.00"))

    assert worker_repo.mark_failed(bad.task_id, "boom")

    user = await api_repo.get_user("u_123")
    assert user is not None
    assert user.balance == Decimal("5.00")
    assert user.status == UserStatus.PROCESSED
    assert user.pending_credits == 0
    failed = await api_repo.get_credit(bad.task_id)
    assert failed is not None
    assert failed.status == CreditStatus.FAILED
    assert failed.error == "boom"


async def test_async_mark_failed_matches_worker_behaviour(
    api_repo: AsyncCreditRepository,
) -> None:
    created, _ = await api_repo.create_pending_credit(credit())

    assert await api_repo.mark_failed(created.task_id, "broker unavailable")
    assert not await api_repo.mark_failed(created.task_id, "again")

    user = await api_repo.get_user("u_123")
    assert user is not None
    assert user.status == UserStatus.PROCESSED
    assert user.pending_credits == 0


async def test_failed_credit_is_never_applied(
    api_repo: AsyncCreditRepository, worker_repo: CreditRepository
) -> None:
    created, _ = await api_repo.create_pending_credit(credit())
    worker_repo.mark_failed(created.task_id, "boom")

    assert worker_repo.apply_credit(created.task_id) == ApplyResult.ALREADY_SETTLED
    user = await api_repo.get_user("u_123")
    assert user is not None
    assert user.balance == Decimal("0")


def test_apply_unknown_credit(worker_repo: CreditRepository) -> None:
    assert worker_repo.apply_credit("does-not-exist") == ApplyResult.NOT_FOUND


async def test_idempotency_key_replay_returns_original(
    api_repo: AsyncCreditRepository,
) -> None:
    first, first_new = await api_repo.create_pending_credit(credit(), idempotency_key="k1")
    again, again_new = await api_repo.create_pending_credit(credit(), idempotency_key="k1")

    assert first_new and not again_new
    assert again.task_id == first.task_id
    user = await api_repo.get_user("u_123")
    assert user is not None
    assert user.pending_credits == 1


async def test_idempotency_key_with_different_body_conflicts(
    api_repo: AsyncCreditRepository,
) -> None:
    await api_repo.create_pending_credit(credit(amount="10.00"), idempotency_key="k1")

    with pytest.raises(IdempotencyConflictError):
        await api_repo.create_pending_credit(credit(amount="20.00"), idempotency_key="k1")


async def test_concurrent_first_credits_for_new_user(
    api_repo: AsyncCreditRepository,
) -> None:
    """Racing upserts of a brand-new user must not lose a pending count."""
    await asyncio.gather(*(api_repo.create_pending_credit(credit("u_new")) for _ in range(20)))

    user = await api_repo.get_user("u_new")
    assert user is not None
    assert user.pending_credits == 20


async def test_decimal_precision_is_exact(
    api_repo: AsyncCreditRepository, worker_repo: CreditRepository
) -> None:
    for amount in ("0.10", "0.20"):
        created, _ = await api_repo.create_pending_credit(credit(amount=amount))
        worker_repo.apply_credit(created.task_id)

    user = await api_repo.get_user("u_123")
    assert user is not None
    assert user.balance == Decimal("0.30")
    assert user.model_dump(mode="json")["balance"] == "0.30"
