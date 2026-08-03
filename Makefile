.DEFAULT_GOAL := help
SHELL := /bin/bash

.PHONY: help setup test test-unit test-integration lint fmt typecheck arch check dev down clean \
	bootstrap web web-install web-check api calibrate

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

api: ## Run the API against local services
	uv run uvicorn evalforge_api.main:create_app --factory --reload --port 8000

web-install: ## Install dashboard dependencies
	pnpm install

web: ## Run the dashboard (needs 'make api' and 'make bootstrap' first)
	pnpm --dir apps/web dev

calibrate: ## Recompute the reference judge calibration from recorded verdicts (free)
	uv run evalforge calibrate evals/suites/reply-tone.yaml \
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
