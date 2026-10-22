"""All credit/balance persistence rules live here.

Data model
----------
- ``credits``: one document per credit request (the ledger). ``status`` moves
  ``pending -> applied`` or ``pending -> failed`` exactly once.
- ``users``: ``balance``, ``status`` and ``pending_credits``, the number of credits
  accepted but not yet settled. ``status`` is ``processed`` only when that count is 0,
  so concurrent credits for one user never report ``processed`` too early.

Every state change touches both collections inside one transaction. The ledger
transition is conditional on ``status == pending``, which makes applying a credit
idempotent: a redelivered task finds nothing pending and changes nothing.
"""

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import uuid4

from bson import Decimal128
from pymongo import AsyncMongoClient, MongoClient
from pymongo.asynchronous.client_session import AsyncClientSession
from pymongo.client_session import ClientSession
from pymongo.errors import DuplicateKeyError

from app.db import CREDITS, USERS, Doc
from app.domain import CreditRequest, CreditStatus, CreditView, UserStatus, UserView

# Concurrent first-time upserts of the same user can race on the unique index.
_UPSERT_ATTEMPTS = 3


class ApplyResult(StrEnum):
    APPLIED = "applied"
    ALREADY_SETTLED = "already_settled"
    NOT_FOUND = "not_found"


class IdempotencyConflictError(Exception):
    """An Idempotency-Key was reused with a different request body."""


def _now() -> datetime:
    return datetime.now(UTC)


def _settle_user_pipeline(amount: Decimal128 | None) -> list[Doc]:
    """Release one pending credit (optionally adding ``amount``) and recompute status."""
    changes: Doc = {
        "pending_credits": {"$subtract": ["$pending_credits", 1]},
        "updated_at": "$$NOW",
    }
    if amount is not None:
        changes["balance"] = {"$add": [{"$ifNull": ["$balance", Decimal128("0")]}, amount]}
    return [
        {"$set": changes},
        {
            "$set": {
                "status": {
                    "$cond": [
                        {"$gt": ["$pending_credits", 0]},
                        UserStatus.PROCESSING.value,
                        UserStatus.PROCESSED.value,
                    ]
                }
            }
        },
    ]


def _pending_filter(credit_id: str) -> Doc:
    return {"_id": credit_id, "status": CreditStatus.PENDING.value}


def _failed_update(error: str) -> Doc:
    return {"$set": {"status": CreditStatus.FAILED.value, "error": error[:1000]}}


def _is_idempotency_conflict(err: DuplicateKeyError) -> bool:
    return "idempotency_key" in (err.details or {}).get("keyPattern", {})


def to_credit_view(doc: Doc) -> CreditView:
    return CreditView(
        task_id=doc["_id"],
        user_id=doc["user_id"],
        amount=doc["amount"].to_decimal(),
        status=doc["status"],
        error=doc.get("error"),
        created_at=doc["created_at"],
        applied_at=doc.get("applied_at"),
    )


def to_user_view(doc: Doc) -> UserView:
    balance = doc.get("balance")
    return UserView(
        user_id=doc["user_id"],
        balance=balance.to_decimal() if balance is not None else Decimal("0"),
        status=doc["status"],
        pending_credits=doc.get("pending_credits", 0),
    )


class AsyncCreditRepository:
    """API-side operations (async client)."""

    def __init__(self, client: AsyncMongoClient[Doc], db_name: str) -> None:
        self._client = client
        db = client[db_name]
        self._credits = db[CREDITS]
        self._users = db[USERS]

    async def create_pending_credit(
        self, req: CreditRequest, idempotency_key: str | None = None
    ) -> tuple[CreditView, bool]:
        """Record a pending credit and mark the user ``processing``.

        Returns ``(credit, created)``; ``created`` is False when an earlier request
        with the same idempotency key already exists (and nothing was written).
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

        for attempt in range(1, _UPSERT_ATTEMPTS + 1):
            try:
                async with self._client.start_session() as session:
                    await session.with_transaction(txn)
                return to_credit_view(credit), True
            except DuplicateKeyError as err:
                if idempotency_key is not None and _is_idempotency_conflict(err):
                    return await self._existing_for_key(idempotency_key, req), False
                if attempt == _UPSERT_ATTEMPTS:
                    raise
        raise AssertionError("unreachable")

    async def _existing_for_key(self, key: str, req: CreditRequest) -> CreditView:
        doc = await self._credits.find_one({"idempotency_key": key})
        if doc is None:  # pragma: no cover - the key was just reported as a duplicate
            raise IdempotencyConflictError(key)
        existing = to_credit_view(doc)
        if existing.user_id != req.user_id or existing.amount != req.amount:
            raise IdempotencyConflictError(key)
        return existing

    async def mark_failed(self, credit_id: str, error: str) -> bool:
        """Fail a still-pending credit and release it from the user's pending count."""

        async def txn(session: AsyncClientSession) -> bool:
            credit = await self._credits.find_one_and_update(
                _pending_filter(credit_id), _failed_update(error), session=session
            )
            if credit is None:
                return False
            await self._users.update_one(
                {"user_id": credit["user_id"]}, _settle_user_pipeline(None), session=session
            )
            return True

        async with self._client.start_session() as session:
            return await session.with_transaction(txn)

    async def get_credit(self, credit_id: str) -> CreditView | None:
        doc = await self._credits.find_one({"_id": credit_id})
        return to_credit_view(doc) if doc else None

    async def get_user(self, user_id: str) -> UserView | None:
        doc = await self._users.find_one({"user_id": user_id})
        return to_user_view(doc) if doc else None


class CreditRepository:
    """Worker-side operations (sync client)."""

    def __init__(self, client: MongoClient[Doc], db_name: str) -> None:
        self._client = client
        db = client[db_name]
        self._credits = db[CREDITS]
        self._users = db[USERS]

    def apply_credit(self, credit_id: str) -> ApplyResult:
        """Add a pending credit to the user's balance. Safe to call any number of times."""

        def txn(session: ClientSession) -> ApplyResult:
            credit = self._credits.find_one_and_update(
                _pending_filter(credit_id),
                {"$set": {"status": CreditStatus.APPLIED.value, "applied_at": _now()}},
                session=session,
            )
            if credit is None:
                exists = self._credits.count_documents({"_id": credit_id}, session=session)
                return ApplyResult.ALREADY_SETTLED if exists else ApplyResult.NOT_FOUND
            self._users.update_one(
                {"user_id": credit["user_id"]},
                _settle_user_pipeline(credit["amount"]),
                session=session,
            )
            return ApplyResult.APPLIED

        with self._client.start_session() as session:
            return session.with_transaction(txn)

    def mark_failed(self, credit_id: str, error: str) -> bool:
        """Fail a still-pending credit and release it from the user's pending count."""

        def txn(session: ClientSession) -> bool:
            credit = self._credits.find_one_and_update(
                _pending_filter(credit_id), _failed_update(error), session=session
            )
            if credit is None:
                return False
            self._users.update_one(
                {"user_id": credit["user_id"]}, _settle_user_pipeline(None), session=session
            )
            return True

        with self._client.start_session() as session:
            return session.with_transaction(txn)
