from typing import Protocol


class CreditPublisher(Protocol):
    def publish(self, credit_id: str) -> None:
        """Enqueue a credit for the worker. Raises if the broker can't accept it."""


class CeleryCreditPublisher:
    def publish(self, credit_id: str) -> None:
        from app.worker.tasks import apply_credit

        # The credit id doubles as the Celery task id: one id to poll, log, and dedupe.
        apply_credit.apply_async(
            args=(credit_id,),
            task_id=credit_id,
            retry=True,
            retry_policy={
                "max_retries": 3,
                "interval_start": 0,
                "interval_step": 0.2,
                "interval_max": 1,
            },
        )
