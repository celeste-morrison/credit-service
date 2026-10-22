"""Request/response models and status enums.

Money is always `Decimal`: requests are validated to at most two decimal places and
responses serialize amounts as strings so clients never see float rounding.
"""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, StringConstraints

MAX_CREDIT_AMOUNT = Decimal("1000000")

UserId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$"
    ),
]

CreditAmount = Annotated[
    Decimal,
    Field(gt=0, le=MAX_CREDIT_AMOUNT, max_digits=12, decimal_places=2, allow_inf_nan=False),
]

Money = Annotated[Decimal, PlainSerializer(lambda d: f"{d:.2f}", return_type=str)]


class CreditStatus(StrEnum):
    PENDING = "pending"
    APPLIED = "applied"
    FAILED = "failed"


class UserStatus(StrEnum):
    PROCESSING = "processing"
    PROCESSED = "processed"


class CreditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: UserId
    amount: CreditAmount


class CreditAccepted(BaseModel):
    task_id: str
    status: CreditStatus


class CreditView(BaseModel):
    task_id: str
    user_id: str
    amount: Money
    status: CreditStatus
    error: str | None = None
    created_at: datetime
    applied_at: datetime | None = None


class UserView(BaseModel):
    user_id: str
    balance: Money
    status: UserStatus
    pending_credits: int
