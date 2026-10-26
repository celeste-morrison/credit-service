"""Error responses.

Every failure uses one JSON shape::

    {"error": {"code": "...", "message": "...", "details": [...]}}

``details`` is present only for validation errors.
"""

from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from pymongo.errors import PyMongoError
from starlette.exceptions import HTTPException

from app.db import is_transient_error

log = structlog.get_logger(__name__)


class ErrorDetail(BaseModel):
    """One validation problem: where it is, what is wrong, and its type."""

    loc: list[str | int]
    msg: str
    type: str


class ErrorBody(BaseModel):
    """A stable error code and a human-readable message."""

    code: str
    message: str
    details: list[ErrorDetail] | None = None


class ErrorResponse(BaseModel):
    """The JSON body of every error response."""

    error: ErrorBody


ERROR_DESCRIPTIONS: dict[int, str] = {
    401: "Missing or invalid `X-API-Key`.",
    404: "Not found.",
    409: "`Idempotency-Key` already used for this user with a different amount.",
    411: "Request body sent without `Content-Length`.",
    413: "Request body larger than the limit (16 KB by default).",
    422: "Validation failed. `details` lists each problem.",
    429: "Client is over its rate limit. `Retry-After` gives the seconds to wait.",
    500: "Unexpected error. Details are logged, never returned.",
    503: "MongoDB or the queue is unavailable. Safe to retry with the same `Idempotency-Key`.",
}


def error_responses(*codes: int) -> dict[int | str, dict[str, Any]]:
    """Describe error responses for the OpenAPI schema.

    Args:
        codes: HTTP status codes the route can return.

    Returns:
        A ``responses`` mapping for a FastAPI route or router.
    """
    responses: dict[int | str, dict[str, Any]] = {}
    for code in codes:
        responses[code] = {"model": ErrorResponse, "description": ERROR_DESCRIPTIONS[code]}
        if code == 429:
            responses[code]["headers"] = {
                "Retry-After": {
                    "description": "Seconds until the rate-limit window resets.",
                    "schema": {"type": "integer"},
                }
            }
    return responses


class ApiError(Exception):
    """An error with a stable code, returned to the client as-is.

    Args:
        status_code: HTTP status.
        code: Machine-readable error code.
        message: Human-readable message.
        headers: Extra response headers, such as ``Retry-After``.
    """

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = headers or {}


def error_response(status_code: int, code: str, message: str, details: Any = None) -> JSONResponse:
    """Build a response in the standard error shape.

    Args:
        status_code: HTTP status.
        code: Machine-readable error code.
        message: Human-readable message.
        details: Optional structured details.

    Returns:
        The JSON error response.
    """
    body: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        body["details"] = jsonable_encoder(details)
    return JSONResponse(status_code=status_code, content={"error": body})


async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
    response = error_response(exc.status_code, exc.code, exc.message)
    response.headers.update(exc.headers)
    return response


async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    details = [{k: e[k] for k in ("loc", "msg", "type")} for e in exc.errors()]
    return error_response(422, "validation_error", "Request validation failed", details)


async def _http_error(_: Request, exc: HTTPException) -> JSONResponse:
    return error_response(exc.status_code, "http_error", str(exc.detail))


async def _mongo_error(request: Request, exc: PyMongoError) -> JSONResponse:
    """Answer 503 for outages and timeouts; treat any other MongoDB error as a bug."""
    if is_transient_error(exc):
        log.error("mongo.unavailable", error=repr(exc))
        return error_response(503, "database_unavailable", "Database is temporarily unavailable")
    return await _unhandled(request, exc)


async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
    log.error("http.unhandled_error", error=repr(exc), exc_info=exc)
    return error_response(500, "internal_error", "Internal server error")


def register_error_handlers(app: FastAPI) -> None:
    """Install the handlers that produce the standard error shape.

    Args:
        app: The application.
    """
    app.exception_handler(ApiError)(_api_error)
    app.exception_handler(RequestValidationError)(_validation_error)
    app.exception_handler(HTTPException)(_http_error)
    app.exception_handler(PyMongoError)(_mongo_error)
    app.exception_handler(Exception)(_unhandled)
