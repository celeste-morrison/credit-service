"""HTTP middleware: request size limits, request ids and access logging."""

import re
import time
from collections.abc import Awaitable, Callable
from uuid import uuid4

import structlog
from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.errors import error_response

log = structlog.get_logger("access")

REQUEST_ID_HEADER = "X-Request-ID"

_VALID_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")

QUIET_PATHS = frozenset({"/healthz", "/readyz"})
"""Probe paths that are not access-logged; orchestrators hit them every few seconds."""


class _BodyTooLargeError(Exception):
    """Raised inside the receive channel once a body passes the size limit."""


class BodySizeLimit:
    """Cap request bodies at ``max_bytes`` before any work is done on them.

    Runs before routing, so oversized bodies are refused before authentication
    or JSON parsing. Two layers:

    1. Headers: a declared ``Content-Length`` above the limit gets 413, and a
       body sent with any ``Transfer-Encoding`` but no ``Content-Length`` gets
       411. JSON clients always send a length.
    2. Byte counting: the receive channel counts body bytes and stops reading
       once the limit is passed, whatever the headers said. The app's own
       response is discarded and 413 is sent instead.

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

        Raises:
            Exception: Anything the wrapped app raises, unless the body overflowed.
        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        rejection = self._header_rejection(dict(scope["headers"]))
        if rejection is not None:
            await rejection(scope, receive, send)
            return

        received = 0
        overflowed = False

        async def counted_receive() -> Message:
            nonlocal received, overflowed
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    overflowed = True
                    raise _BodyTooLargeError
            return message

        async def guarded_send(message: Message) -> None:
            if not overflowed:
                await send(message)

        try:
            await self.app(scope, counted_receive, guarded_send)
        except Exception:
            if not overflowed:
                raise
        if overflowed:
            await _too_large()(scope, receive, send)

    def _header_rejection(self, headers: dict[bytes, bytes]) -> JSONResponse | None:
        """Return the error response for unacceptable body headers, or ``None``."""
        declared = headers.get(b"content-length")
        if declared is None:
            if b"transfer-encoding" in headers:
                return error_response(411, "length_required", "Content-Length is required")
            return None
        if not declared.isdigit() or int(declared) > self.max_bytes:
            return _too_large()
        return None


def _too_large() -> JSONResponse:
    """Build the 413 response."""
    return error_response(413, "payload_too_large", "Request body too large")


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
