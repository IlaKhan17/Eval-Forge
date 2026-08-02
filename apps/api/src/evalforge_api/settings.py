"""Application settings.

Environment-driven, with production safety checks that run at startup rather than
at first use. A service that boots with a development secret and only reveals it
when someone forges a token has failed in the worst possible way.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["development", "test", "staging", "production"]

DEV_SECRET_MARKERS = ("dev", "change", "insecure", "secret", "test", "example")
MIN_SECRET_LENGTH = 32


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

    redis_url: str = "redis://127.0.0.1:6379/0"

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

    @property
    def sqlalchemy_url(self) -> str:
        if self.database_url:
            return self.database_url
        return (
            f"postgresql+psycopg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

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

        if problems:
            joined = "\n  - ".join(problems)
            msg = f"refusing to start in production:\n  - {joined}"
            raise ValueError(msg)
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
