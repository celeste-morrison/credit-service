"""HTTP middleware: request size limits, request ids and access logging."""

import re
import time
from collections.abc import Awaitable, Callable
from uuid import uuid4

import structlog
from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.api.errors import error_response

log = structlog.get_logger("access")

REQUEST_ID_HEADER = "X-Request-ID"

_VALID_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")

QUIET_PATHS = frozenset({"/healthz", "/readyz"})
"""Probe paths that are not access-logged; orchestrators hit them every few seconds."""


class BodySizeLimit:
    """Reject request bodies larger than ``max_bytes`` before any work is done on them.

    Runs before routing, so oversized bodies are refused before authentication
    or JSON parsing. The server enforces ``Content-Length`` framing, so the
    declared length is trusted: larger bodies get 413, and chunked bodies,
    which declare no length, get 411. JSON clients always send a length.

    Args:
        app: The wrapped ASGI app.
        max_bytes: Largest body accepted.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Handle one ASGI connection.

        Args:
            scope: The ASGI connection scope.
            receive: The ASGI receive channel.
            send: The ASGI send channel.
        """
        if scope["type"] == "http":
            rejection = self._rejection(dict(scope["headers"]))
            if rejection is not None:
                await rejection(scope, receive, send)
                return
        await self.app(scope, receive, send)

    def _rejection(self, headers: dict[bytes, bytes]) -> JSONResponse | None:
        """Return the error response for an unacceptable body, or ``None`` to proceed."""
        declared = headers.get(b"content-length")
        if declared is None:
            if b"chunked" in headers.get(b"transfer-encoding", b""):
                return error_response(411, "length_required", "Content-Length is required")
            return None
        if not declared.isdigit() or int(declared) > self.max_bytes:
            return error_response(413, "payload_too_large", "Request body too large")
        return None


def _request_id(request: Request) -> str:
    """Keep a well-formed client ``X-Request-ID``, otherwise generate a new one."""
    supplied = request.headers.get(REQUEST_ID_HEADER, "")
    return supplied if _VALID_REQUEST_ID.fullmatch(supplied) else uuid4().hex


async def request_context(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Bind a request id to all logs for this request, echo it back and log access.

    A client-supplied ``X-Request-ID`` is kept only if it is short and uses safe
    characters; otherwise a new id is generated.

    Args:
        request: The incoming request.
        call_next: The next handler.

    Returns:
        The response, with ``X-Request-ID`` set.
    """
    request_id = _request_id(request)
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(request_id=request_id)
    start = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers[REQUEST_ID_HEADER] = request_id
        return response
    finally:
        if request.url.path not in QUIET_PATHS:
            log.info(
                "http.request",
                method=request.method,
                path=request.url.path,
                status=status,
                duration_ms=round((time.perf_counter() - start) * 1000, 1),
            )
