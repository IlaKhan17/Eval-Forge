"""RFC 9457 problem details.

Every error response has the same shape, always includes a `request_id`, and never
leaks internals. `request_id` is echoed in the `X-Request-Id` header and written to
the log line, which is the single most useful thing when supporting a self-hoster
who can only give you a screenshot.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

ERROR_BASE = "https://errors.proofstep.dev"
CONTENT_TYPE = "application/problem+json"


class ApiError(Exception):
    """Base for every error the API raises deliberately."""

    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    error_type: str = "internal_error"
    title: str = "Internal server error"

    def __init__(self, detail: str = "", **extra: Any) -> None:
        super().__init__(detail or self.title)
        self.detail = detail or self.title
        self.extra = extra

    def to_problem(self, request: Request) -> dict[str, Any]:
        problem: dict[str, Any] = {
            "type": f"{ERROR_BASE}/{self.error_type}",
            "title": self.title,
            "status": self.status_code,
            "detail": self.detail,
            "instance": request.url.path,
            "request_id": getattr(request.state, "request_id", None),
        }
        problem.update(self.extra)
        return problem


class BadRequestError(ApiError):
    status_code = status.HTTP_400_BAD_REQUEST
    error_type = "bad_request"
    title = "Bad request"


class UnauthorizedError(ApiError):
    status_code = status.HTTP_401_UNAUTHORIZED
    error_type = "unauthorized"
    title = "Authentication required"


class ForbiddenError(ApiError):
    """Authenticated, but lacking the permission.

    Used only *within* a tenant. Crossing a tenant boundary raises NotFoundError,
    because 403 would confirm that the resource exists.
    """

    status_code = status.HTTP_403_FORBIDDEN
    error_type = "forbidden"
    title = "Insufficient permissions"


class NotFoundError(ApiError):
    status_code = status.HTTP_404_NOT_FOUND
    error_type = "not_found"
    title = "Not found"


class ConflictError(ApiError):
    status_code = status.HTTP_409_CONFLICT
    error_type = "conflict"
    title = "Conflict"


class PayloadTooLargeError(ApiError):
    status_code = status.HTTP_413_CONTENT_TOO_LARGE
    error_type = "payload_too_large"
    title = "Payload too large"


class UnprocessableError(ApiError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    error_type = "unprocessable"
    title = "Unprocessable request"


class RateLimitedError(ApiError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    error_type = "rate_limited"
    title = "Rate limit exceeded"

    def __init__(self, detail: str = "", *, retry_after: int = 60, **extra: Any) -> None:
        super().__init__(detail, **extra)
        self.retry_after = retry_after


def problem_response(request: Request, exc: ApiError) -> JSONResponse:
    headers: dict[str, str] = {}
    if isinstance(exc, RateLimitedError):
        headers["Retry-After"] = str(exc.retry_after)
    if isinstance(exc, UnauthorizedError):
        headers["WWW-Authenticate"] = "Bearer"
    return JSONResponse(
        status_code=exc.status_code,
        content=exc.to_problem(request),
        media_type=CONTENT_TYPE,
        headers=headers,
    )


def validation_response(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Turn pydantic's errors into the same problem shape, with field paths."""
    errors = [
        {
            "field": ".".join(str(p) for p in err["loc"][1:]) or str(err["loc"][0]),
            "code": err["type"],
            "message": err["msg"],
        }
        for err in exc.errors()
    ]
    problem = UnprocessableError("The request body failed validation.").to_problem(request)
    problem["errors"] = errors[:50]
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content=problem,
        media_type=CONTENT_TYPE,
    )
