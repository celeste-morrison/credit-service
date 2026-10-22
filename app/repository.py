"""Persistence rules for credits and balances.

Data model:
    ``credits`` holds one document per credit request (the ledger). Its
    ``status`` moves ``pending -> applied`` or ``pending -> failed`` exactly once.

    ``users`` holds ``balance``, ``status`` and ``pending_credits``, the number
    of credits accepted but not yet settled. ``status`` is ``processed`` only
    when that count is zero, so concurrent credits for one user never report
    ``processed`` early.

Every state change touches both collections inside one transaction. The ledger
transition is conditional on ``status == pending``, which makes applying a
credit idempotent: a redelivered task finds nothing pending and changes no
balance.

Every transaction runs under ``pymongo.timeout`` so a MongoDB outage surfaces
as a fast ``PyMongoError`` instead of PyMongo's default two-minute transaction
retry loop.
"""

from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import uuid4

import pymongo
from bson import Decimal128
from pymongo import AsyncMongoClient, MongoClient
from pymongo.asynchronous.client_session import AsyncClientSession
from pymongo.client_session import ClientSession
from pymongo.errors import DuplicateKeyError

from app.db import CREDITS, USERS, Doc
from app.domain import CreditRequest, CreditStatus, CreditView, UserStatus, UserView

type AsyncTxn[T] = Callable[[AsyncClientSession], Coroutine[Any, Any, T]]
type SyncTxn[T] = Callable[[ClientSession], T]

UPSERT_ATTEMPTS = 3
"""Attempts at creating a credit when first-time upserts of one user race."""

QUEUE_UNAVAILABLE = "queue_unavailable"
"""Error code stored on credits that could not be enqueued."""


class ApplyResult(StrEnum):
    """Outcome of applying a credit."""

    APPLIED = "applied"
    ALREADY_SETTLED = "already_settled"
    NOT_FOUND = "not_found"


class IdempotencyConflictError(Exception):
    """An Idempotency-Key was reused for the same user with a different amount."""


def _now() -> datetime:
    return datetime.now(UTC)


def _settle_user_pipeline(amount: Decimal128 | None) -> list[Doc]:
    """Build the user update that releases one pending credit.

    Args:
        amount: Amount to add to the balance, or ``None`` when the credit failed.

    Returns:
        An aggregation-pipeline update that decrements ``pending_credits``,
        optionally adds ``amount`` (creating ``balance`` if missing) and
        recomputes ``status`` from the new pending count.
    """
    changes: Doc = {
        "pending_credits": {"$subtract": ["$pending_credits", 1]},
        "updated_at": "$$NOW",
    }
    if amount is not None:
        changes["balance"] = {"$add": [{"$ifNull": ["$balance", Decimal128("0")]}, amount]}
    status = {
        "$cond": [
            {"$gt": ["$pending_credits", 0]},
            UserStatus.PROCESSING.value,
            UserStatus.PROCESSED.value,
        ]
    }
    return [{"$set": changes}, {"$set": {"status": status}}]


def _pending(credit_id: str) -> Doc:
    """Filter matching ``credit_id`` only while it is still pending."""
    return {"_id": credit_id, "status": CreditStatus.PENDING.value}


def _failed(error: str) -> Doc:
    return {"status": CreditStatus.FAILED.value, "error": error}


def _is_idempotency_conflict(err: DuplicateKeyError) -> bool:
    """Tell an Idempotency-Key collision apart from a racing user upsert."""
    return "idempotency_key" in (err.details or {}).get("keyPattern", {})


def to_credit_view(doc: Doc) -> CreditView:
    """Convert a ``credits`` document to its API representation.

    Args:
        doc: A document from the ``credits`` collection.

    Returns:
        The credit as returned by the API.
    """
    return CreditView(
        task_id=doc["_id"],
        user_id=doc["user_id"],
        amount=doc["amount"].to_decimal(),
        status=doc["status"],
        error=doc.get("error"),
        redeliveries=doc.get("redeliveries", 0),
        created_at=doc["created_at"],
        applied_at=doc.get("applied_at"),
    )


def to_user_view(doc: Doc) -> UserView:
    """Convert a ``users`` document to its API representation.

    A user who has never had a credit applied has no ``balance`` field yet and
    is shown with a zero balance.

    Args:
        doc: A document from the ``users`` collection.

    Returns:
        The user as returned by the API.
    """
    balance = doc.get("balance")
    return UserView(
        user_id=doc["user_id"],
        balance=balance.to_decimal() if balance is not None else Decimal("0"),
        status=doc["status"],
        pending_credits=doc.get("pending_credits", 0),
    )


class AsyncCreditRepository:
    """API-side operations on the async client.

    Args:
        client: Async MongoDB client.
        db_name: Database name.
        timeout_s: Upper bound for each operation or transaction.
    """

    def __init__(self, client: AsyncMongoClient[Doc], db_name: str, timeout_s: float) -> None:
        self._client = client
        self._credits = client[db_name][CREDITS]
        self._users = client[db_name][USERS]
        self._timeout_s = timeout_s

    async def create_pending_credit(
        self, req: CreditRequest, idempotency_key: str | None = None
    ) -> tuple[CreditView, bool]:
        """Record a pending credit and mark the user ``processing``.

        Args:
            req: The validated credit request.
            idempotency_key: Optional client key, unique per user.

        Returns:
            ``(credit, created)``. ``created`` is False when an earlier request
            with the same user and key exists, in which case nothing is written.

        Raises:
            IdempotencyConflictError: The key was used for a different amount.
            pymongo.errors.PyMongoError: MongoDB failed or timed out.
        """
        now = _now()
        credit: Doc = {
            "_id": str(uuid4()),
            "user_id": req.user_id,
            "amount": Decimal128(req.amount),
            "status": CreditStatus.PENDING.value,
            "created_at": now,
        }
        if idempotency_key is not None:
            credit["idempotency_key"] = idempotency_key

        async def txn(session: AsyncClientSession) -> None:
            await self._credits.insert_one(credit, session=session)
            await self._users.update_one(
                {"user_id": req.user_id},
                {
                    "$inc": {"pending_credits": 1},
                    "$set": {"status": UserStatus.PROCESSING.value, "updated_at": now},
                    "$setOnInsert": {"created_at": now},
                },
                upsert=True,
                session=session,
            )

        for attempt in range(1, UPSERT_ATTEMPTS + 1):
            try:
                await self._run(txn)
                return to_credit_view(credit), True
            except DuplicateKeyError as err:
                if idempotency_key is not None and _is_idempotency_conflict(err):
                    return await self._existing_for_key(idempotency_key, req), False
                if attempt == UPSERT_ATTEMPTS:
                    raise
        raise AssertionError("unreachable")

    async def fail_unpublished(self, credit_id: str) -> bool:
        """Fail a credit that could not be enqueued and free its idempotency key.

        Removing the key lets the client retry with the same Idempotency-Key and
        get a fresh credit instead of a replay of this failed one.

        Args:
            credit_id: The credit to fail.

        Returns:
            True if the credit was still pending and is now failed.

        Raises:
            pymongo.errors.PyMongoError: MongoDB failed or timed out.
        """

        async def txn(session: AsyncClientSession) -> bool:
            credit = await self._credits.find_one_and_update(
                _pending(credit_id),
                {"$set": _failed(QUEUE_UNAVAILABLE), "$unset": {"idempotency_key": ""}},
                session=session,
            )
            if credit is None:
                return False
            await self._users.update_one(
                {"user_id": credit["user_id"]}, _settle_user_pipeline(None), session=session
            )
            return True

        return await self._run(txn)

    async def get_credit(self, credit_id: str) -> CreditView | None:
        """Fetch a credit by id.

        Args:
            credit_id: The credit (and task) id.

        Returns:
            The credit, or ``None`` if it does not exist.

        Raises:
            pymongo.errors.PyMongoError: MongoDB failed or timed out.
        """
        with pymongo.timeout(self._timeout_s):
            doc = await self._credits.find_one({"_id": credit_id})
        return to_credit_view(doc) if doc else None

    async def get_user(self, user_id: str) -> UserView | None:
        """Fetch a user by id.

        Args:
            user_id: The user id.

        Returns:
            The user, or ``None`` if it does not exist.

        Raises:
            pymongo.errors.PyMongoError: MongoDB failed or timed out.
        """
        with pymongo.timeout(self._timeout_s):
            doc = await self._users.find_one({"user_id": user_id})
        return to_user_view(doc) if doc else None

    async def _existing_for_key(self, key: str, req: CreditRequest) -> CreditView:
        """Return the credit already stored under ``key`` if it matches ``req``.

        Raises:
            IdempotencyConflictError: The stored credit has a different amount.
        """
        doc = await self._credits.find_one({"user_id": req.user_id, "idempotency_key": key})
        if doc is None or doc["amount"].to_decimal() != req.amount:
            raise IdempotencyConflictError(key)
        return to_credit_view(doc)

    async def _run[T](self, txn: AsyncTxn[T]) -> T:
        """Run ``txn`` in a transaction bounded by the operation timeout."""
        with pymongo.timeout(self._timeout_s):
            async with self._client.start_session() as session:
                return await session.with_transaction(txn)


class CreditRepository:
    """Worker-side operations on the sync client.

    Args:
        client: MongoDB client.
        db_name: Database name.
        timeout_s: Upper bound for each operation or transaction.
    """

    def __init__(self, client: MongoClient[Doc], db_name: str, timeout_s: float) -> None:
        self._client = client
        self._credits = client[db_name][CREDITS]
        self._users = client[db_name][USERS]
        self._timeout_s = timeout_s

    def apply_credit(self, credit_id: str) -> ApplyResult:
        """Add a pending credit to the user's balance.

        Safe to call any number of times: only the first call on a pending
        credit changes the balance. Later calls only count the redelivery.

        Args:
            credit_id: The credit to apply.

        Returns:
            What happened to the credit.

        Raises:
            pymongo.errors.PyMongoError: MongoDB failed or timed out.
        """

        def txn(session: ClientSession) -> ApplyResult:
            credit = self._credits.find_one_and_update(
                _pending(credit_id),
                {"$set": {"status": CreditStatus.APPLIED.value, "applied_at": _now()}},
                session=session,
            )
            if credit is None:
                settled = self._credits.find_one_and_update(
                    {"_id": credit_id}, {"$inc": {"redeliveries": 1}}, session=session
                )
                return ApplyResult.ALREADY_SETTLED if settled else ApplyResult.NOT_FOUND
            self._users.update_one(
                {"user_id": credit["user_id"]},
                _settle_user_pipeline(credit["amount"]),
                session=session,
            )
            return ApplyResult.APPLIED

        return self._run(txn)

    def mark_failed(self, credit_id: str, error: str) -> bool:
        """Fail a still-pending credit and release it from the user's pending count.

        Args:
            credit_id: The credit to fail.
            error: Stable error code shown to API clients.

        Returns:
            True if the credit was still pending and is now failed.

        Raises:
            pymongo.errors.PyMongoError: MongoDB failed or timed out.
        """

        def txn(session: ClientSession) -> bool:
            credit = self._credits.find_one_and_update(
                _pending(credit_id), {"$set": _failed(error)}, session=session
            )
            if credit is None:
                return False
            self._users.update_one(
                {"user_id": credit["user_id"]}, _settle_user_pipeline(None), session=session
            )
            return True

        return self._run(txn)

    def find_stale_pending(self, older_than: datetime, limit: int) -> list[str]:
        """List credits still pending since before ``older_than``.

        Args:
            older_than: Creation-time cutoff.
            limit: Maximum number of ids to return.

        Returns:
            Credit ids, oldest first.

        Raises:
            pymongo.errors.PyMongoError: MongoDB failed or timed out.
        """
        query = {"status": CreditStatus.PENDING.value, "created_at": {"$lt": older_than}}
        with pymongo.timeout(self._timeout_s):
            cursor = self._credits.find(query, {"_id": 1}).sort("created_at").limit(limit)
            return [doc["_id"] for doc in cursor]

    def _run[T](self, txn: SyncTxn[T]) -> T:
        """Run ``txn`` in a transaction bounded by the operation timeout."""
        with pymongo.timeout(self._timeout_s), self._client.start_session() as session:
            return session.with_transaction(txn)
