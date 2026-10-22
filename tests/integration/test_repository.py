"""Persistence rules, against a real MongoDB replica set."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pymongo import MongoClient

from app.db import CREDITS, USERS, Doc
from app.domain import CreditStatus, UserStatus
from app.repository import (
    ApplyResult,
    AsyncCreditRepository,
    CreditRepository,
    IdempotencyConflictError,
)
from tests.integration.helpers import credit_request, fetch_credit, fetch_user


async def test_create_pending_credit_marks_user_processing(
    api_repo: AsyncCreditRepository,
) -> None:
    created, is_new = await api_repo.create_pending_credit(credit_request())

    assert is_new
    assert created.status == CreditStatus.PENDING
    user = await fetch_user(api_repo)
    assert user.status == UserStatus.PROCESSING
    assert user.pending_credits == 1


async def test_balance_field_is_created_by_first_apply(
    api_repo: AsyncCreditRepository,
    worker_repo: CreditRepository,
    sync_client: MongoClient[Doc],
    db_name: str,
) -> None:
    created, _ = await api_repo.create_pending_credit(credit_request(amount="150.00"))
    raw = sync_client[db_name][USERS].find_one({"user_id": "u_123"})
    assert raw is not None and "balance" not in raw

    assert worker_repo.apply_credit(created.task_id) == ApplyResult.APPLIED

    user = await fetch_user(api_repo)
    assert user.balance == Decimal("150.00")
    assert user.status == UserStatus.PROCESSED
    assert user.pending_credits == 0
    applied = await fetch_credit(api_repo, created.task_id)
    assert applied.status == CreditStatus.APPLIED
    assert applied.applied_at is not None


async def test_apply_adds_to_existing_balance(
    api_repo: AsyncCreditRepository, worker_repo: CreditRepository
) -> None:
    for amount in ("100.00", "50.25"):
        created, _ = await api_repo.create_pending_credit(credit_request(amount=amount))
        worker_repo.apply_credit(created.task_id)

    user = await fetch_user(api_repo)
    assert user.balance == Decimal("150.25")


async def test_apply_twice_credits_once(
    api_repo: AsyncCreditRepository, worker_repo: CreditRepository
) -> None:
    """A redelivered task (at-least-once broker) must not double-credit."""
    created, _ = await api_repo.create_pending_credit(credit_request(amount="50.00"))

    assert worker_repo.apply_credit(created.task_id) == ApplyResult.APPLIED
    assert worker_repo.apply_credit(created.task_id) == ApplyResult.ALREADY_SETTLED

    user = await fetch_user(api_repo)
    assert user.balance == Decimal("50.00")
    assert user.pending_credits == 0
    settled = await fetch_credit(api_repo, created.task_id)
    assert settled.redeliveries == 1


async def test_concurrent_duplicate_deliveries_credit_once(
    api_repo: AsyncCreditRepository, worker_repo: CreditRepository
) -> None:
    created, _ = await api_repo.create_pending_credit(credit_request(amount="10.00"))

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(worker_repo.apply_credit, [created.task_id] * 8))

    assert results.count(ApplyResult.APPLIED) == 1
    user = await fetch_user(api_repo)
    assert user.balance == Decimal("10.00")


async def test_concurrent_credits_for_one_user_sum_exactly(
    api_repo: AsyncCreditRepository, worker_repo: CreditRepository
) -> None:
    amounts = [Decimal("0.10") + Decimal(i) / 100 for i in range(50)]
    created = await asyncio.gather(
        *(api_repo.create_pending_credit(credit_request(amount=str(a))) for a in amounts)
    )

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(worker_repo.apply_credit, [c.task_id for c, _ in created]))

    assert results == [ApplyResult.APPLIED] * 50
    user = await fetch_user(api_repo)
    assert user.balance == sum(amounts)
    assert user.pending_credits == 0
    assert user.status == UserStatus.PROCESSED


async def test_status_stays_processing_until_last_credit_settles(
    api_repo: AsyncCreditRepository, worker_repo: CreditRepository
) -> None:
    first, _ = await api_repo.create_pending_credit(credit_request(amount="1.00"))
    second, _ = await api_repo.create_pending_credit(credit_request(amount="2.00"))

    worker_repo.apply_credit(first.task_id)
    user = await fetch_user(api_repo)
    assert user.status == UserStatus.PROCESSING
    assert user.pending_credits == 1

    worker_repo.apply_credit(second.task_id)
    user = await fetch_user(api_repo)
    assert user.status == UserStatus.PROCESSED


async def test_mark_failed_releases_user_without_changing_balance(
    api_repo: AsyncCreditRepository, worker_repo: CreditRepository
) -> None:
    ok, _ = await api_repo.create_pending_credit(credit_request(amount="5.00"))
    worker_repo.apply_credit(ok.task_id)
    bad, _ = await api_repo.create_pending_credit(credit_request(amount="99.00"))

    assert worker_repo.mark_failed(bad.task_id, "boom")

    user = await fetch_user(api_repo)
    assert user.balance == Decimal("5.00")
    assert user.status == UserStatus.PROCESSED
    assert user.pending_credits == 0
    failed = await fetch_credit(api_repo, bad.task_id)
    assert failed.status == CreditStatus.FAILED
    assert failed.error == "boom"


async def test_fail_unpublished_releases_user_and_frees_idempotency_key(
    api_repo: AsyncCreditRepository,
) -> None:
    created, _ = await api_repo.create_pending_credit(credit_request(), idempotency_key="k1")

    assert await api_repo.fail_unpublished(created.task_id)
    assert not await api_repo.fail_unpublished(created.task_id)

    user = await fetch_user(api_repo)
    assert user.status == UserStatus.PROCESSED
    assert user.pending_credits == 0
    failed = await fetch_credit(api_repo, created.task_id)
    assert failed.status == CreditStatus.FAILED
    assert failed.error == "queue_unavailable"

    retried, is_new = await api_repo.create_pending_credit(credit_request(), idempotency_key="k1")
    assert is_new
    assert retried.task_id != created.task_id


async def test_failed_credit_is_never_applied(
    api_repo: AsyncCreditRepository, worker_repo: CreditRepository
) -> None:
    created, _ = await api_repo.create_pending_credit(credit_request())
    worker_repo.mark_failed(created.task_id, "boom")

    assert worker_repo.apply_credit(created.task_id) == ApplyResult.ALREADY_SETTLED
    user = await fetch_user(api_repo)
    assert user.balance == Decimal("0")


def test_apply_unknown_credit(worker_repo: CreditRepository) -> None:
    assert worker_repo.apply_credit("does-not-exist") == ApplyResult.NOT_FOUND


async def test_idempotency_key_replay_returns_original(
    api_repo: AsyncCreditRepository,
) -> None:
    first, first_new = await api_repo.create_pending_credit(credit_request(), idempotency_key="k1")
    again, again_new = await api_repo.create_pending_credit(credit_request(), idempotency_key="k1")

    assert first_new and not again_new
    assert again.task_id == first.task_id
    user = await fetch_user(api_repo)
    assert user.pending_credits == 1


async def test_idempotency_key_with_different_body_conflicts(
    api_repo: AsyncCreditRepository,
) -> None:
    await api_repo.create_pending_credit(credit_request(amount="10.00"), idempotency_key="k1")

    with pytest.raises(IdempotencyConflictError):
        await api_repo.create_pending_credit(credit_request(amount="20.00"), idempotency_key="k1")


async def test_idempotency_keys_are_scoped_per_user(
    api_repo: AsyncCreditRepository,
) -> None:
    first, _ = await api_repo.create_pending_credit(credit_request("u_a"), idempotency_key="k1")
    second, is_new = await api_repo.create_pending_credit(
        credit_request("u_b"), idempotency_key="k1"
    )

    assert is_new
    assert second.task_id != first.task_id


async def test_find_stale_pending_returns_only_old_pending_credits(
    api_repo: AsyncCreditRepository,
    worker_repo: CreditRepository,
    sync_client: MongoClient[Doc],
    db_name: str,
) -> None:
    old, _ = await api_repo.create_pending_credit(credit_request())
    old_applied, _ = await api_repo.create_pending_credit(credit_request())
    await api_repo.create_pending_credit(credit_request())
    worker_repo.apply_credit(old_applied.task_id)
    long_ago = datetime.now(UTC) - timedelta(hours=1)
    sync_client[db_name][CREDITS].update_many(
        {"_id": {"$in": [old.task_id, old_applied.task_id]}}, {"$set": {"created_at": long_ago}}
    )

    stale = worker_repo.find_stale_pending(datetime.now(UTC) - timedelta(minutes=2), limit=10)

    assert stale == [old.task_id]


async def test_concurrent_first_credits_for_new_user(
    api_repo: AsyncCreditRepository,
) -> None:
    """Racing upserts of a brand-new user must not lose a pending count."""
    await asyncio.gather(
        *(api_repo.create_pending_credit(credit_request("u_new")) for _ in range(20))
    )

    user = await fetch_user(api_repo, "u_new")
    assert user.pending_credits == 20


async def test_decimal_precision_is_exact(
    api_repo: AsyncCreditRepository, worker_repo: CreditRepository
) -> None:
    for amount in ("0.10", "0.20"):
        created, _ = await api_repo.create_pending_credit(credit_request(amount=amount))
        worker_repo.apply_credit(created.task_id)

    user = await fetch_user(api_repo)
    assert user.balance == Decimal("0.30")
    assert user.model_dump(mode="json")["balance"] == "0.30"


async def test_expire_idempotency_keys_releases_only_old_keys(
    api_repo: AsyncCreditRepository,
    worker_repo: CreditRepository,
    sync_client: MongoClient[Doc],
    db_name: str,
) -> None:
    old, _ = await api_repo.create_pending_credit(credit_request(), idempotency_key="old")
    await api_repo.create_pending_credit(credit_request(), idempotency_key="new")
    sync_client[db_name][CREDITS].update_one(
        {"_id": old.task_id}, {"$set": {"created_at": datetime.now(UTC) - timedelta(days=2)}}
    )

    assert worker_repo.expire_idempotency_keys(datetime.now(UTC) - timedelta(days=1)) == 1

    reused, is_new = await api_repo.create_pending_credit(credit_request(), idempotency_key="old")
    assert is_new
    assert reused.task_id != old.task_id
    _, replayed_new = await api_repo.create_pending_credit(credit_request(), idempotency_key="new")
    assert not replayed_new
