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
from pymongo.errors import PyMongoError
from starlette.exceptions import HTTPException

from app.db import is_transient_error

log = structlog.get_logger(__name__)


class ApiError(Exception):
    """An error with a stable code, returned to the client as-is.

    Args:
        status_code: HTTP status.
        code: Machine-readable error code.
        message: Human-readable message.
    """

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


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
    return error_response(exc.status_code, exc.code, exc.message)


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
