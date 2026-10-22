"""Request validation and money formatting."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from app.config import Settings
from app.domain import CreditRequest, CreditStatus, CreditView, UserStatus, UserView


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        ("0.01", Decimal("0.01")),
        (50, Decimal("50")),
        (12.5, Decimal("12.5")),
        ("1000000", Decimal("1000000")),
        ("1000000.00", Decimal("1000000.00")),
    ],
)
def test_accepts_valid_amounts(amount: Any, expected: Decimal) -> None:
    req = CreditRequest.model_validate({"user_id": "u_123", "amount": amount})
    assert req.amount == expected


@pytest.mark.parametrize(
    "amount",
    [
        0,
        "0.00",
        -5,
        "10.001",
        0.005,
        "1000000.01",
        "abc",
        None,
        "NaN",
        "Infinity",
        True,
        "1_000",
        "\u0661\u0660\u0660",
        " 5 ",
        "1E+6",
        "-5",
        "+5",
        ".5",
    ],
)
def test_rejects_invalid_amounts(amount: Any) -> None:
    with pytest.raises(ValidationError):
        CreditRequest.model_validate({"user_id": "u_123", "amount": amount})


def test_json_float_amount_is_not_rounded() -> None:
    req = CreditRequest.model_validate_json('{"user_id": "u_1", "amount": 0.1}')
    assert req.amount == Decimal("0.1")


@pytest.mark.parametrize("user_id", ["", "   ", "a" * 65, "u 123", "u/123", "$where", None, 123])
def test_rejects_invalid_user_ids(user_id: Any) -> None:
    with pytest.raises(ValidationError):
        CreditRequest.model_validate({"user_id": user_id, "amount": "10"})


def test_strips_user_id_whitespace() -> None:
    req = CreditRequest.model_validate({"user_id": "  u_123 ", "amount": "10"})
    assert req.user_id == "u_123"


@pytest.mark.parametrize(
    "payload",
    [
        {"amount": "10"},
        {"user_id": "u_123"},
        {"user_id": "u_123", "amount": "10", "balance": "999"},
    ],
)
def test_rejects_missing_or_extra_fields(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        CreditRequest.model_validate(payload)


def test_money_serializes_as_two_decimal_string() -> None:
    user = UserView(
        user_id="u_123",
        balance=Decimal("150"),
        status=UserStatus.PROCESSED,
        pending_credits=0,
    )
    assert user.model_dump(mode="json")["balance"] == "150.00"

    credit = CreditView(
        task_id="t",
        user_id="u_123",
        amount=Decimal("0.3"),
        status=CreditStatus.PENDING,
        created_at=datetime.now(UTC),
    )
    assert credit.model_dump(mode="json")["amount"] == "0.30"


def test_api_keys_parse_into_client_names() -> None:
    settings = Settings(api_keys=SecretStr(" billing:k1, payouts:k2,k3,, "))

    assert settings.api_clients() == {"k1": "billing", "k2": "payouts", "k3": "default"}
