.PHONY: install lint format typecheck test test-db migrate check

install:
	pip install -e '.[dev]'

lint:
	ruff check .
	ruff format --check .

format:
	ruff format .
	ruff check --fix .

typecheck:
	mypy app

test:
	pytest -q

# Runs DB tests against an isolated database (created + dropped by scripts/test_db.sh).
test-db:
	scripts/test_db.sh

migrate:
	alembic upgrade head

check: lint typecheck test
