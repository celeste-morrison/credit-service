"""Celery application and broker configuration.

Delivery is at-least-once. Tasks are acknowledged only after they finish
(``task_acks_late``), so a worker crash re-queues the task instead of losing
the credit. That is safe because applying a credit is idempotent; see
``app.repository``.

Settings worth knowing:
    ``task_ignore_result``: MongoDB is the source of truth for credit status,
    so there is no result backend.

    ``visibility_timeout``: with ``acks_late`` on Redis, an unacknowledged task
    is redelivered after this window. It must exceed the task time limit plus
    the longest retry countdown, and stays short so a killed worker's task is
    picked up within minutes.

    ``socket_timeout`` / ``socket_connect_timeout``: bound broker calls so a
    half-open Redis connection cannot block an API thread indefinitely.

    ``worker_redirect_stdouts``: off, so the JSON logs reach stdout unwrapped.

    ``beat_schedule``: every minute, re-enqueue credits stuck in ``pending``;
    every hour, release idempotency keys past their TTL.

Logging: Celery's own logging setup is replaced by ``configure_logging`` via
the ``setup_logging`` signal, so worker and beat output is JSON like the API's.
"""

from typing import Any

from celery import Celery
from celery.signals import setup_logging

from app.config import get_settings
from app.logging import configure_logging

settings = get_settings()


@setup_logging.connect
def _configure_logging(**_: Any) -> None:
    """Use the service's JSON logging instead of Celery's default handlers."""
    configure_logging(settings.log_level)


configure_logging(settings.log_level)

celery = Celery("credits", broker=settings.redis_url, include=["app.worker.tasks"])
celery.conf.update(
    task_serializer="json",
    accept_content=["json"],
    task_ignore_result=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    worker_cancel_long_running_tasks_on_connection_loss=True,
    task_time_limit=60,
    task_soft_time_limit=50,
    task_default_queue="credits",
    broker_transport_options={
        "visibility_timeout": 300,
        "socket_timeout": 5,
        "socket_connect_timeout": 5,
    },
    broker_connection_retry_on_startup=True,
    worker_redirect_stdouts=False,
    worker_hijack_root_logger=False,
    beat_schedule={
        "reconcile-pending-credits": {
            "task": "credits.reconcile",
            "schedule": settings.reconcile_interval_s,
        },
        "expire-idempotency-keys": {
            "task": "credits.expire_idempotency_keys",
            "schedule": 3600,
        },
    },
)
