"""Request and response models, status enums and money validation.

Money is always ``Decimal``. Requests accept a JSON number or a plain decimal
string with at most two decimal places; responses serialize amounts as strings
so clients never see float rounding.
"""

import re
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    StringConstraints,
)

MAX_CREDIT_AMOUNT = Decimal("1000000")

_PLAIN_DECIMAL = re.compile(r"[0-9]+(\.[0-9]+)?")


def _plain_amount(value: Any) -> Any:
    """Reject amount spellings that ``Decimal()`` accepts but money formats should not.

    ``Decimal`` happily parses ``"1_000"``, ``" 5 "``, ``"1E+6"`` and non-ASCII
    digits. Only plain ASCII decimal strings and JSON numbers are allowed through.

    Args:
        value: The raw ``amount`` from the request.

    Returns:
        The value unchanged, for Pydantic's own Decimal validation.

    Raises:
        ValueError: If the value is a boolean or a non-plain decimal string.
    """
    if isinstance(value, bool):
        raise ValueError("amount must be a number")
    if isinstance(value, str) and not _PLAIN_DECIMAL.fullmatch(value):
        raise ValueError("amount must be a plain decimal such as 12.50")
    return value


UserId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$"
    ),
]

CreditAmount = Annotated[
    Decimal,
    BeforeValidator(_plain_amount),
    Field(gt=0, le=MAX_CREDIT_AMOUNT, max_digits=12, decimal_places=2, allow_inf_nan=False),
]

Money = Annotated[Decimal, PlainSerializer(lambda d: f"{d:.2f}", return_type=str)]


class CreditStatus(StrEnum):
    """Lifecycle of a single credit. ``applied`` and ``failed`` are terminal."""

    PENDING = "pending"
    APPLIED = "applied"
    FAILED = "failed"


class UserStatus(StrEnum):
    """``processing`` while any credit for the user is still pending."""

    PROCESSING = "processing"
    PROCESSED = "processed"


class CreditRequest(BaseModel):
    """Body of ``POST /credits``."""

    model_config = ConfigDict(extra="forbid")

    user_id: UserId
    amount: CreditAmount


class CreditAccepted(BaseModel):
    """Response of ``POST /credits``."""

    task_id: str
    status: CreditStatus


class CreditView(BaseModel):
    """A credit as returned by ``GET /credits/{task_id}``.

    Attributes:
        redeliveries: Times the worker received this credit after it was
            already settled. Non-zero values are normal under at-least-once
            delivery and show the idempotency guard at work.
    """

    task_id: str
    user_id: str
    amount: Money
    status: CreditStatus
    error: str | None = None
    redeliveries: int = 0
    created_at: datetime
    applied_at: datetime | None = None


class UserView(BaseModel):
    """A user as returned by ``GET /users/{user_id}``."""

    user_id: str
    balance: Money
    status: UserStatus
    pending_credits: int
