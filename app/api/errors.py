"""One JSON error shape for every failure: {"error": {"code", "message", "details"?}}."""

from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pymongo.errors import ConnectionFailure
from starlette.exceptions import HTTPException

log = structlog.get_logger(__name__)


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _error(status_code: int, code: str, message: str, details: Any = None) -> JSONResponse:
    body: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        body["details"] = jsonable_encoder(details)
    return JSONResponse(status_code=status_code, content={"error": body})


async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
    return _error(exc.status_code, exc.code, exc.message)


async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    details = [{k: e[k] for k in ("loc", "msg", "type")} for e in exc.errors()]
    return _error(422, "validation_error", "Request validation failed", details)


async def _http_error(_: Request, exc: HTTPException) -> JSONResponse:
    return _error(exc.status_code, "http_error", str(exc.detail))


async def _db_unavailable(_: Request, exc: ConnectionFailure) -> JSONResponse:
    log.error("mongo.unavailable", error=repr(exc))
    return _error(503, "database_unavailable", "Database is temporarily unavailable")


def register_error_handlers(app: FastAPI) -> None:
    app.exception_handler(ApiError)(_api_error)
    app.exception_handler(RequestValidationError)(_validation_error)
    app.exception_handler(HTTPException)(_http_error)
    app.exception_handler(ConnectionFailure)(_db_unavailable)
