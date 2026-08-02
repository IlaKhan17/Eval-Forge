"""Application factory."""

from __future__ import annotations

import logging
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException

from evalforge_api.api.routes import health
from evalforge_api.db.session import dispose_engine, get_sessionmaker, init_engine
from evalforge_api.errors import (
    ApiError,
    BadRequestError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    PayloadTooLargeError,
    UnauthorizedError,
    UnprocessableError,
    problem_response,
    validation_response,
)
from evalforge_api.settings import Settings, get_settings

logger = logging.getLogger("evalforge.api")

_STATUS_ERRORS = {
    400: BadRequestError,
    401: UnauthorizedError,
    403: ForbiddenError,
    404: NotFoundError,
    409: ConflictError,
    413: PayloadTooLargeError,
    422: UnprocessableError,
}

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings or get_settings()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        init_engine(config)
        try:
            yield
        finally:
            await dispose_engine()

    app = FastAPI(
        title="EvalForge API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if not config.is_production else None,
        redoc_url=None,
    )
    app.state.settings = config

    _install_middleware(app, config)
    _install_error_handlers(app)
    app.include_router(health.router)
    return app


def _install_middleware(app: FastAPI, config: Settings) -> None:
    if config.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=config.cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
        )

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # A caller-supplied id is echoed for trace continuity but never trusted as
        # a log key beyond a length cap.
        incoming = request.headers.get("x-request-id", "")[:64]
        request_id = incoming or secrets.token_hex(8)
        request.state.request_id = request_id

        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > config.max_request_bytes:
            # Reject on the declared size before reading the body: streaming a
            # gigabyte only to reject it is the denial of service.
            response: Response = problem_response(
                request,
                PayloadTooLargeError(
                    f"Request body exceeds the {config.max_request_bytes} byte limit."
                ),
            )
        else:
            started = time.perf_counter()
            response = await call_next(request)
            response.headers["X-Response-Time-Ms"] = f"{(time.perf_counter() - started) * 1000:.1f}"

        response.headers["X-Request-Id"] = request_id
        for header, value in SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        return response


def _install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, exc: Exception) -> Response:
        assert isinstance(exc, ApiError)
        return problem_response(request, exc)

    @app.exception_handler(RequestValidationError)
    async def handle_validation(request: Request, exc: Exception) -> Response:
        assert isinstance(exc, RequestValidationError)
        return validation_response(request, exc)

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(request: Request, exc: Exception) -> Response:
        """Route-level errors — 404, 405, and anything raised as HTTPException.

        Without this they bypass the problem-details format entirely, so a client
        parsing errors would need two code paths and "always RFC 9457" would be a
        claim the API does not keep.
        """
        assert isinstance(exc, StarletteHTTPException)
        mapped = _STATUS_ERRORS.get(exc.status_code)
        detail = exc.detail or "Request failed."
        if mapped is not None:
            return problem_response(request, mapped(detail))

        generic = ApiError(detail)
        generic.status_code = exc.status_code
        generic.error_type = f"http_{exc.status_code}"
        generic.title = detail
        return problem_response(request, generic)

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> Response:  # noqa: ARG001
        # Log the detail, return none of it. An unexpected exception's message can
        # contain a connection string or a row of user data.
        logger.exception(
            "unhandled error", extra={"request_id": getattr(request.state, "request_id", None)}
        )
        return problem_response(request, ApiError("An unexpected error occurred."))


async def check_database() -> bool:
    try:
        async with get_sessionmaker()() as session:
            await session.execute(text("SELECT 1"))
    except Exception:
        return False
    return True
