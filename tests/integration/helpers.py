"""Helpers shared by the integration tests."""

from decimal import Decimal

from app.domain import CreditRequest, CreditView, UserView
from app.repository import AsyncCreditRepository


def credit_request(user_id: str = "u_123", amount: str = "50.00") -> CreditRequest:
    """Build a validated credit request.

    Args:
        user_id: The user to credit.
        amount: The amount, as a decimal string.

    Returns:
        The request.
    """
    return CreditRequest(user_id=user_id, amount=Decimal(amount))


async def fetch_user(repo: AsyncCreditRepository, user_id: str = "u_123") -> UserView:
    """Fetch a user that the test expects to exist.

    Args:
        repo: The API-side repository.
        user_id: The user to fetch.

    Returns:
        The user.
    """
    user = await repo.get_user(user_id)
    assert user is not None, f"user {user_id} not found"
    return user


async def fetch_credit(repo: AsyncCreditRepository, credit_id: str) -> CreditView:
    """Fetch a credit that the test expects to exist.

    Args:
        repo: The API-side repository.
        credit_id: The credit to fetch.

    Returns:
        The credit.
    """
    credit = await repo.get_credit(credit_id)
    assert credit is not None, f"credit {credit_id} not found"
    return credit
