"""统一业务异常。"""


class DomainError(Exception):
    """业务错误基类，status_code 映射到 HTTP 响应。"""

    status_code = 400
    code = "domain_error"

    def __init__(self, message: str, *, detail: dict | None = None):
        super().__init__(message)
        self.message = message
        self.detail = detail or {}


class NotFoundError(DomainError):
    status_code = 404
    code = "not_found"


class ConflictError(DomainError):
    status_code = 409
    code = "conflict"


class StaleDataError(DomainError):
    status_code = 409
    code = "stale_data"


class ValidationFailedError(DomainError):
    status_code = 422
    code = "validation_failed"


class ForbiddenError(DomainError):
    status_code = 403
    code = "forbidden"


class UnauthorizedError(DomainError):
    status_code = 401
    code = "unauthorized"


class MigrationError(DomainError):
    status_code = 500
    code = "migration_error"
