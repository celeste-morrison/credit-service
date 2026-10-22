"""Celery application.

Delivery is at-least-once: tasks are acknowledged only after they finish
(``task_acks_late``), so a worker crash re-queues the task rather than losing the
credit. That is safe because applying a credit is idempotent (see app.repository).
"""

from celery import Celery

from app.config import get_settings
from app.logging import configure_logging

settings = get_settings()
configure_logging(settings.log_level)

celery = Celery("credits", broker=settings.redis_url, include=["app.worker.tasks"])
celery.conf.update(
    task_serializer="json",
    accept_content=["json"],
    # MongoDB is the source of truth for credit status; no result backend needed.
    task_ignore_result=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_time_limit=60,
    task_soft_time_limit=50,
    task_default_queue="credits",
    # With acks_late on Redis, an unacked task is redelivered after this window.
    # It must exceed the longest task runtime (time_limit + retry backoff).
    broker_transport_options={"visibility_timeout": 3600},
    broker_connection_retry_on_startup=True,
    # Our JSON logs go straight to stdout instead of being re-wrapped by Celery.
    worker_redirect_stdouts=False,
)
