"""JSON logging on stdout.

Context bound with ``structlog.contextvars`` (``request_id`` in the API,
``credit_id`` in the worker) is added to every log line.
"""

import logging
import sys

import structlog


def configure_logging(level: str = "INFO") -> None:
    """Configure stdlib logging and structlog to emit JSON lines.

    Args:
        level: Minimum log level name, such as ``"INFO"``.
    """
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level.upper())
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        logger_factory=structlog.PrintLoggerFactory(sys.stdout),
        cache_logger_on_first_use=True,
    )
