"""Domain errors and FastAPI exception handlers.

All business-layer failures raise an :class:`AppError` subclass carrying a
stable ``code`` string so callers (and old clients) can distinguish rejection
reasons instead of guessing from HTTP status text.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class AppError(Exception):
    """Base class for all expected, explainable service errors."""

    code: str = "internal_error"
    status_code: int = 400

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class NotFound(AppError):
    code = "not_found"
    status_code = 404


class Conflict(AppError):
    code = "conflict"
    status_code = 409


class ValidationFailed(AppError):
    code = "validation_failed"
    status_code = 422


class StaleReport(AppError):
    """Report rejected because a newer state is already known."""

    code = "stale_state"
    status_code = 409


class DuplicateCommand(AppError):
    """Idempotent rejection: the same command id was already processed."""

    code = "duplicate_command"
    status_code = 409


class DeviceOffline(AppError):
    code = "device_offline"
    status_code = 409


class RuleCycle(AppError):
    code = "rule_cycle"
    status_code = 422


class BrokenReference(AppError):
    code = "broken_reference"
    status_code = 422


class RuleDisabled(AppError):
    code = "rule_disabled"
    status_code = 409


class Forbidden(AppError):
    code = "forbidden"
    status_code = 403


class Unauthorized(AppError):
    code = "unauthorized"
    status_code = 401


class DataMigrationError(AppError):
    """Persisted data has an unsupported/incompatible schema version."""

    code = "data_migration_failed"
    status_code = 500


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _handle_app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "details": exc.details,
                }
            },
        )
