.DEFAULT_GOAL := check
.PHONY: services test typecheck lint check

services:  ## Start the PostgreSQL and Redis the integration tests need
	docker compose up -d --wait

test: services  ## Unit and integration tests; no model calls
	uv run pytest

typecheck:
	uv run mypy

lint:
	uv run ruff check
	uv run ruff format --check

check: lint typecheck test  ## Everything that must pass before a commit
