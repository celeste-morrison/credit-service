"""Enqueueing abstraction, so the API can be tested without a broker."""

from typing import Protocol


class CreditPublisher(Protocol):
    """Something that can hand a credit to the worker."""

    def publish(self, credit_id: str) -> None:
        """Enqueue a credit.

        Args:
            credit_id: The credit to apply.

        Raises:
            Exception: The credit could not be enqueued.
        """


class CeleryCreditPublisher:
    """Publishes credits to the Celery queue."""

    def publish(self, credit_id: str) -> None:
        """Enqueue a credit on the Celery broker.

        Args:
            credit_id: The credit to apply.
        """
        from app.worker.tasks import enqueue

        enqueue(credit_id)
