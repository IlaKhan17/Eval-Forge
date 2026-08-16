.DEFAULT_GOAL := help
SHELL := /bin/bash

.PHONY: help setup test test-unit test-integration lint fmt typecheck arch check dev down clean \
	bootstrap web web-install web-check api calibrate worker online-eval otlp-example partitions app-role \
	preflight backup keys

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
		awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

setup: ## Install the Python 3.12 toolchain and sync the workspace
	uv python install 3.12
	uv sync --all-packages
	@echo "✓ setup complete — run 'make test'"

test: ## Run unit tests (no docker required)
	uv run pytest -m "not integration and not e2e and not load and not live"

test-unit: test ## Alias for test

test-integration: ## Run integration tests (requires 'make dev')
	uv run pytest -m integration

lint: ## Lint and format-check
	uv run ruff check .
	uv run ruff format --check .

fmt: ## Auto-format
	uv run ruff format .
	uv run ruff check --fix .

typecheck: ## Strict type checking
	uv run mypy

arch: ## Enforce architectural import contracts
	uv run lint-imports --config .importlinter

check: lint typecheck arch test ## Everything CI runs on a PR

dev: ## Start local services (postgres, redis, minio)
	docker compose up -d postgres redis minio
	@echo "✓ services up — see .env for the ports (they are probed to avoid conflicts)"

bootstrap: ## Create a local org, project, and API key; write apps/web/.env.local
	uv run alembic upgrade head
	uv run python scripts/bootstrap_dev.py --write-web-env

partitions: ## Create the partitions the coming months need (needs DDL privileges)
	uv run python -c "$$PARTITIONS_SNIPPET"

worker: ## Run the background worker (online eval, rollups, retention)
	uv run arq proofstep_api.worker.main.WorkerSettings

otlp-example: ## Run the plain-OpenTelemetry example against a local API
	@test -n "$$PROOFSTEP_API_KEY" || (echo "set PROOFSTEP_API_KEY (see 'make bootstrap')" && exit 1)
	OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:8000/v1/otlp \
	OTEL_EXPORTER_OTLP_HEADERS="authorization=Bearer $$PROOFSTEP_API_KEY" \
	uv run python examples/langgraph-agent/agent.py

online-eval: ## Process one batch of online evaluations now, without the worker
	uv run python -c "$$ONLINE_EVAL_SNIPPET"

api: ## Run the API against local services
	uv run uvicorn proofstep_api.main:create_app --factory --reload --port 8000

web-install: ## Install dashboard dependencies
	pnpm install

web: ## Run the dashboard (needs 'make api' and 'make bootstrap' first)
	pnpm --dir apps/web dev

calibrate: ## Recompute the reference judge calibration from recorded verdicts (free)
	uv run proofstep calibrate evals/suites/reply-tone.yaml \
		-e acceptable_to_followup \
		--verdicts evals/calibration/reply-tone.verdicts.jsonl

web-check: ## Lint, typecheck, test, and build the dashboard
	pnpm --dir apps/web lint
	pnpm --dir apps/web typecheck
	pnpm --dir apps/web test
	pnpm --dir apps/web build

down: ## Stop local services
	docker compose down

clean: ## Remove caches and build artifacts
	rm -rf .mypy_cache .ruff_cache .pytest_cache htmlcov .coverage dist build
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

# Runs one batch in-process, for draining a backlog or checking a rule without waiting for
# the worker's cadence.
export ONLINE_EVAL_SNIPPET
define ONLINE_EVAL_SNIPPET
import asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from proofstep_api.settings import get_settings
from proofstep_api.worker import jobs

async def main():
    engine = create_async_engine(get_settings().sqlalchemy_url)
    async with async_sessionmaker(engine)() as session:
        report = await jobs.run_online_eval(session)
        await session.commit()
        print(report)
    await engine.dispose()

asyncio.run(main())
endef

export PARTITIONS_SNIPPET
define PARTITIONS_SNIPPET
import asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from proofstep_api.db.partitions import ensure_partitions, missing_partitions
from proofstep_api.settings import get_settings

async def main():
    engine = create_async_engine(get_settings().sqlalchemy_url)
    async with engine.begin() as connection:
        created = await ensure_partitions(connection)
        missing = await missing_partitions(connection)
    print(f"ensured {len(created)} partition(s); missing for this month: {missing or 'none'}")
    await engine.dispose()

asyncio.run(main())
endef

preflight: ## Check a deployment before it takes traffic
	uv run python scripts/preflight.py

backup: ## Take a verifiable database backup into ./backups
	./scripts/backup.sh

keys: ## Manage API keys (list/create/rotate/revoke) — see docs/OPERATIONS.md
	uv run python scripts/manage_keys.py --help

app-role: ## Create the unprivileged role the application should connect as
	@test -n "$$APP_ROLE_PASSWORD" || (echo "set APP_ROLE_PASSWORD (e.g. \$$(openssl rand -hex 24))" && exit 1)
	docker exec -i proofstep-postgres-1 psql -U "$$POSTGRES_USER" -d "$$POSTGRES_DB" \
		-v role_password="$$APP_ROLE_PASSWORD" < scripts/create_app_role.sql
