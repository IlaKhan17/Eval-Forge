"""Application settings.

Environment-driven, with production safety checks that run at startup rather than
at first use. A service that boots with a development secret and only reveals it
when someone forges a token has failed in the worst possible way.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["development", "test", "staging", "production"]

DEV_SECRET_MARKERS = ("dev", "change", "insecure", "secret", "test", "example")
MIN_SECRET_LENGTH = 32

#: Settings that may be supplied as `<NAME>_FILE` pointing at a file containing the value.
#:
#: This is the convention Docker secrets, Kubernetes secret volumes, and most secret managers
#: already speak, and it is worth supporting for one specific reason: an environment variable is
#: readable from `/proc/<pid>/environ`, leaks into `docker inspect`, and lands in crash reports and
#: process listings. A file has an owner and a mode.
#:
#: Only these five. An allow-list rather than "any setting", because reading arbitrary paths from
#: the environment is a wider capability than this needs.
FILE_BACKED = (
    "jwt_secret",
    "postgres_password",
    "s3_secret_key",
    "database_url",
    "migration_database_url",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    env: Environment = "development"
    debug: bool = False

    # ------------------------------------------------------------------ storage
    postgres_user: str = "evalforge"
    postgres_password: str = ""
    postgres_db: str = "evalforge"
    postgres_host: str = "127.0.0.1"
    postgres_port: int = 5432
    database_url: str | None = None

    #: Credentials for migrations and other DDL, when they differ from the application's.
    #:
    #: They should. The application role must not own the tables it reads: a non-owner is subject to
    #: RLS even without FORCE, cannot create a table that has no policy, and cannot detach a
    #: partition. Running both as one role collapses that separation and is the single most common
    #: way a deployment ends up with RLS enabled and doing nothing.
    #:
    #: Falls back to the application's URL, so a single-role development install keeps working.
    migration_database_url: str | None = None

    #: Escape hatch for running in production as a role that bypasses row-level security.
    #:
    #: Exists because there are legitimate cases — a managed Postgres that only offers a superuser,
    #: a migration window — and because a check with no escape hatch gets disabled wholesale rather
    #: than for the one case that needed it. Off by default, refused at startup, and logged as a
    #: warning on every boot when it is on, so it cannot be turned on and forgotten.
    allow_rls_bypass: bool = False

    redis_url: str = "redis://127.0.0.1:6379/0"

    # Object storage. Absent endpoint means payload offload stays in-process, which
    # keeps a bare `uvicorn` run working with nothing but Postgres.
    s3_endpoint: str | None = None
    s3_bucket: str = "evalforge-payloads"
    s3_access_key: str = ""
    s3_secret_key: str = ""

    # --------------------------------------------------------------------- auth
    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    access_token_ttl_s: int = 15 * 60
    refresh_token_ttl_s: int = 30 * 24 * 3600
    api_key_cache_ttl_s: int = Field(
        default=30,
        description=(
            "Bounded so a revoked key stops working promptly. Longer caching would "
            "trade a real security property for a trivial latency win."
        ),
    )

    # ------------------------------------------------------------------- limits
    rate_limit_ingest_per_min: int = 600
    rate_limit_read_per_min: int = 300
    rate_limit_write_per_min: int = 60
    rate_limit_auth_per_min: int = 10
    max_request_bytes: int = 5 * 1024 * 1024
    max_page_size: int = 200

    cors_origins: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _read_secret_files(cls, values: Any) -> Any:
        """Load `<NAME>_FILE` settings from disk before validation.

        A present-but-empty file is treated as absent rather than as an empty secret: an empty
        `jwt_secret` would fail the production check with a confusing message, while "the file the
        orchestrator mounted has not been populated" is the actual problem.
        """
        if not isinstance(values, dict):
            return values
        for name in FILE_BACKED:
            for key in (f"{name}_file", f"{name}_FILE".upper()):
                path = values.get(key)
                if not path:
                    continue
                try:
                    content = Path(str(path)).read_text(encoding="utf-8").strip()
                except OSError as exc:
                    msg = f"could not read {key}={path!r}: {exc}"
                    raise ValueError(msg) from exc
                if content:
                    values[name] = content
        return values

    @property
    def sqlalchemy_url(self) -> str:
        if self.database_url:
            return self.database_url
        return (
            f"postgresql+psycopg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def migration_url(self) -> str:
        """Where DDL runs from. The application URL when no separate role is configured."""
        return self.migration_database_url or self.sqlalchemy_url

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @model_validator(mode="after")
    def _refuse_unsafe_production(self) -> Settings:
        """Fail fast rather than run insecurely.

        Refusing to boot is loud and immediate. Booting with a guessable signing key
        is silent until someone mints their own token.
        """
        if not self.is_production:
            return self

        problems: list[str] = []
        secret = self.jwt_secret
        if len(secret) < MIN_SECRET_LENGTH:
            problems.append(
                f"jwt_secret must be at least {MIN_SECRET_LENGTH} characters in production"
            )
        if any(marker in secret.lower() for marker in DEV_SECRET_MARKERS):
            problems.append("jwt_secret looks like a development placeholder")
        if not self.postgres_password and not self.database_url:
            problems.append("postgres_password is empty")
        if self.debug:
            problems.append("debug must be off in production")
        if "*" in self.cors_origins:
            problems.append("cors_origins must not contain '*' in production")
        if self.migration_database_url is None:
            # A warning would be ignored. Running migrations as the application role means the
            # application owns its tables, and a table owner is exempt from its own policies unless
            # FORCE is set on every one of them — which is a property of the schema that a future
            # migration can silently drop.
            problems.append(
                "migration_database_url is not set, so migrations would run as the application "
                "role. See docs/HARDENING.md: the application must not own its tables."
            )

        if problems:
            joined = "\n  - ".join(problems)
            msg = f"refusing to start in production:\n  - {joined}"
            raise ValueError(msg)
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
