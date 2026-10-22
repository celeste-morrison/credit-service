"""JSON logging on stdout for the service and the libraries it runs on.

Context bound with ``structlog.contextvars`` (``request_id`` and ``client_id``
in the API, ``credit_id`` and ``request_id`` in the worker) is added to every
line. Records from stdlib loggers, such as uvicorn's error log and Celery's
worker log, go through the same renderer, so tracebacks are JSON as well.
"""

import logging
import sys

import structlog
from structlog.typing import Processor

THIRD_PARTY_LOGGERS = ("uvicorn", "uvicorn.error", "celery")
"""Loggers that install their own handlers; they are routed to the root JSON handler."""

SILENCED_LOGGERS = ("uvicorn.access",)
"""Loggers replaced by the service's own structured access log."""


def configure_logging(level: str = "INFO") -> None:
    """Configure structlog and stdlib logging to emit one JSON object per line.

    Safe to call more than once; each call replaces the previous handlers.

    Args:
        level: Minimum log level name, such as ``"INFO"``.
    """
    log_level = logging.getLevelName(level.upper())
    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]
    structlog.configure(
        processors=[
            *shared,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.PrintLoggerFactory(sys.stdout),
        cache_logger_on_first_use=True,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=[*shared, structlog.stdlib.add_logger_name],
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.format_exc_info,
                structlog.processors.JSONRenderer(),
            ],
        )
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(log_level)
    for name in THIRD_PARTY_LOGGERS:
        third_party = logging.getLogger(name)
        third_party.handlers.clear()
        third_party.propagate = True
    for name in SILENCED_LOGGERS:
        logging.getLogger(name).disabled = True
