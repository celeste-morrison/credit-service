.DEFAULT_GOAL := help

# Load local overrides (ports, URLs) for tests and scripts.
-include .env
export

.PHONY: help install deps up down clean logs lint format test test-unit test-e2e demo

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-12s %s\n", $$1, $$2}'

install: ## Install dependencies into .venv
	uv sync

deps: ## Start MongoDB and Redis only
	docker compose up -d --wait mongo redis

up: ## Build and start the full stack (api, worker, mongo, redis)
	docker compose up -d --build --wait

down: ## Stop the stack
	docker compose down

clean: ## Stop the stack and delete volumes
	docker compose down -v

logs: ## Tail api and worker logs
	docker compose logs -f api worker

lint: ## Ruff lint + format check + mypy
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy

format: ## Auto-format and fix lint
	uv run ruff format .
	uv run ruff check --fix .

test-unit: ## Unit tests (no services needed)
	uv run pytest tests/unit

test: deps ## Unit + integration tests
	uv run pytest

test-e2e: up ## End-to-end tests against the running stack
	uv run pytest -m e2e

demo: up ## Send sample credits and show the result
	./scripts/demo.sh
