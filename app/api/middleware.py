import time
from collections.abc import Awaitable, Callable
from uuid import uuid4

import structlog
from fastapi import Request, Response

log = structlog.get_logger("access")

REQUEST_ID_HEADER = "X-Request-ID"
# Orchestrator probes hit these every few seconds; logging them is noise.
QUIET_PATHS = frozenset({"/healthz", "/readyz"})


async def request_context(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Bind a request id to all logs for this request, echo it back, and log access."""
    request_id = request.headers.get(REQUEST_ID_HEADER) or uuid4().hex
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(request_id=request_id)
    start = time.perf_counter()
    try:
        response = await call_next(request)
    finally:
        duration_ms = round((time.perf_counter() - start) * 1000, 1)
    response.headers[REQUEST_ID_HEADER] = request_id
    if request.url.path in QUIET_PATHS:
        return response
    log.info(
        "http.request",
        method=request.method,
        path=request.url.path,
        status=response.status_code,
        duration_ms=duration_ms,
    )
    return response
