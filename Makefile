.DEFAULT_GOAL := help

GIT_SHA := $(shell git rev-parse --short HEAD 2>/dev/null || echo dev)$(shell git status --porcelain 2>/dev/null | grep -q . && echo -dirty)
API_PORT ?= 8000
.PHONY: help install hooks infra-up infra-down infra-logs clear-llm-cache build up smoke logs migrate migration migrations-check run \
	worker lint format typecheck test test-unit test-cov check clean

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "} {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# --- Setup ---------------------------------------------------------------------------------------

install: ## Install dependencies from uv.lock
	uv sync --locked

hooks: ## Install pre-commit and pre-push git hooks
	uvx pre-commit install

# --- Infrastructure ------------------------------------------------------------------------------

infra-up: ## Start PostgreSQL and Redis, wait until healthy
	docker compose up -d --wait db redis

infra-down: ## Stop all containers (data volumes are kept)
	docker compose down

infra-logs: ## Follow PostgreSQL and Redis logs
	docker compose logs -f db redis

clear-llm-cache: ## Forget remembered LLM extraction failures (in the compose Redis)
	docker compose exec -T redis sh -c "redis-cli --scan --pattern 'llm-failure:*' | xargs -r redis-cli del"

build: ## Build the app image, tagged with the current commit (GIT_SHA)
	docker compose build --build-arg GIT_SHA=$(GIT_SHA)

up: build ## Build and start the full stack: migrations, then the API and the worker
	docker compose up -d --wait

smoke: ## Upload a QR-bill to the running stack and wait until it is an invoice
	python3 scripts/smoke_test.py http://localhost:$(API_PORT)

logs: ## Follow the API and worker logs
	docker compose logs -f api worker

# --- Database ------------------------------------------------------------------------------------

migrate: ## Apply all migrations
	uv run alembic upgrade head

migration: ## Create a migration: make migration m="add invoices table"
	@test -n "$(m)" || (echo 'Usage: make migration m="<message>"' && exit 1)
	uv run alembic revision --autogenerate -m "$(m)"

migrations-check: ## Apply all migrations to an empty throwaway database, then fail if the models differ
	docker compose exec -T db sh -c 'psql -U "$$POSTGRES_USER" -d postgres -v ON_ERROR_STOP=1 \
		-c "DROP DATABASE IF EXISTS migrations_check WITH (FORCE)" -c "CREATE DATABASE migrations_check"'
	POSTGRES_DB=migrations_check uv run alembic upgrade head
	POSTGRES_DB=migrations_check uv run alembic check
	docker compose exec -T db sh -c 'psql -U "$$POSTGRES_USER" -d postgres -c "DROP DATABASE migrations_check WITH (FORCE)"'

# --- Run -----------------------------------------------------------------------------------------

run: ## Run the API with auto-reload (http://localhost:8000/docs)
	uv run uvicorn src.main:app --reload --no-access-log

worker: ## Run the ingestion worker (processes uploaded files)
	uv run python -m src.worker

# --- Quality -------------------------------------------------------------------------------------

lint: ## Lint with ruff
	uv run ruff check .

format: ## Fix lint issues and format code
	uv run ruff check . --fix
	uv run ruff format .

typecheck: ## Type-check with mypy (strict)
	uv run mypy src

test: ## Run all tests
	uv run pytest

test-unit: ## Run fast unit tests only (no DB)
	uv run pytest tests/unit

test-cov: ## Run tests with a coverage report
	uv run pytest --cov --cov-report=term-missing

check: ## Run every check CI runs (must pass before a task is done)
	uv sync --locked
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy src
	uv run pytest

clean: ## Remove caches and coverage output
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage coverage.xml
	find . -type d -name __pycache__ -not -path './.venv/*' -prune -exec rm -rf {} +
