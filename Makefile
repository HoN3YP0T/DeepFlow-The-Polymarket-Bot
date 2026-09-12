.PHONY: install test lint typecheck check fmt up down migrate run api clean

install:
	python -m venv .venv
	.venv/bin/pip install -e ".[dev]"

test:
	.venv/bin/pytest

## Integration tests only. Needs a database; see DEEPFLOW_TEST_DSN.
test-integration:
	.venv/bin/pytest tests/integration -v

lint:
	.venv/bin/ruff check src tests

fmt:
	.venv/bin/ruff check --fix src tests
	.venv/bin/ruff format src tests

typecheck:
	.venv/bin/mypy

## Everything CI runs.
check: lint typecheck test

up:
	docker compose up -d db redis

down:
	docker compose down

migrate:
	.venv/bin/alembic upgrade head

run:
	.venv/bin/deepflow

api:
	.venv/bin/uvicorn deepflow.api.app:create_app --factory --reload

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache .mypy_cache
