# The API and the worker. One image, two commands.
#
# Two processes from one image because they share every model, service, and setting — a second
# image would duplicate the whole dependency set and then drift from it. `docker run … api` and
# `docker run … worker` pick which one runs.
#
# syntax=docker/dockerfile:1

# ---------------------------------------------------------------- builder
FROM python:3.12-slim-bookworm AS builder

# uv from its official image rather than pip-installed: it is a static binary, so this adds no
# Python-level dependency that could conflict with the application's.
COPY --from=ghcr.io/astral-sh/uv:0.9.7 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Manifests first, then source. Dependencies change far less often than code, so this layer is
# reused across almost every rebuild — the difference between a 4-second and a 90-second image.
# The READMEs come with them: each package declares `readme = "README.md"`, and hatchling refuses to
# build metadata without the file — so a manifest-only layer fails with "Readme file does not exist".
COPY pyproject.toml uv.lock README.md ./
COPY packages/shared-types/pyproject.toml packages/shared-types/README.md packages/shared-types/
COPY packages/evaluation-core/pyproject.toml packages/evaluation-core/README.md packages/evaluation-core/
COPY packages/trajectory-engine/pyproject.toml packages/trajectory-engine/README.md packages/trajectory-engine/
COPY packages/python-sdk/pyproject.toml packages/python-sdk/README.md packages/python-sdk/
COPY packages/cli/pyproject.toml packages/cli/README.md packages/cli/
COPY apps/api/pyproject.toml apps/api/

# `--no-install-project` so only third-party dependencies land in this layer; the workspace packages
# are installed below, after their source is copied.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project --package proofstep-api

COPY packages/ packages/
COPY apps/api/ apps/api/
COPY infra/ infra/
# The operational scripts the entrypoint dispatches to: `migrate` provisions the application role
# from scripts/, and `preflight` is a script too. Without this the image builds fine and those two
# commands fail at runtime with a missing file.
COPY scripts/ scripts/
COPY alembic.ini ./

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package proofstep-api

# ---------------------------------------------------------------- runtime
FROM python:3.12-slim-bookworm AS runtime

# Non-root. A container process that can write its own code is one exploit away from persistence,
# and nothing here needs to.
RUN groupadd --system --gid 1001 proofstep \
    && useradd --system --uid 1001 --gid proofstep --home /app proofstep

WORKDIR /app

# The virtualenv and the source, owned by the unprivileged user but not writable by it.
COPY --from=builder --chown=root:root /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ENV=production

USER proofstep
EXPOSE 8000

# Liveness only — deliberately not /readyz. Readiness depends on the database, and a container
# orchestrator that kills a healthy process because Postgres blipped turns a recoverable dependency
# outage into a restart storm. Readiness is the orchestrator's separate probe.
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status == 200 else 1)"

COPY --chmod=755 infra/docker/entrypoint.sh /usr/local/bin/entrypoint.sh
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["api"]
