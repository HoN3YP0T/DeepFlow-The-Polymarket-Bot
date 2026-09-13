.PHONY: install test test-integration lint fmt typecheck check verify capture-fixtures \
	db-local up down migrate run api clean

install:
	python -m venv .venv
	.venv/bin/pip install -e ".[dev]"

test:
	.venv/bin/pytest

## Integration tests only. Needs a database; see DEEPFLOW_TEST_DSN.
test-integration:
	.venv/bin/pytest tests/integration -v

lint:
	.venv/bin/ruff check src tests scripts

fmt:
	.venv/bin/ruff check --fix src tests scripts
	.venv/bin/ruff format src tests scripts

typecheck:
	.venv/bin/mypy

## Everything CI runs.
check: lint typecheck test

## Prove the adapters against the live venue. No credentials needed -- every
## endpoint these touch is public. Not part of `check`: they need the network and
## the result depends on what is trading right now, which is not a pass/fail a
## commit gate can own.
verify:
	.venv/bin/python scripts/verify_slice.py
	.venv/bin/python scripts/verify_stream.py
	.venv/bin/python scripts/verify_game_join.py
	.venv/bin/python scripts/verify_phase1.py

## Refresh the captured payload corpus. Exits non-zero if the capture would test
## less than the committed one -- do not commit a warning capture.
capture-fixtures:
	.venv/bin/python scripts/capture_sports_fixtures.py

## Start the local Postgres used by the integration tests, for environments with
## no Docker. Without it those 30 tests skip -- and a skipped test reads as green,
## which is how an unverified persistence layer gets reported as passing.
db-local:
	su postgres -c "/usr/lib/postgresql/16/bin/pg_ctl -D /var/lib/postgresql/deepflow-dev -l /tmp/pg.log start" || true
	su postgres -c "/usr/lib/postgresql/16/bin/pg_isready"

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
