.PHONY: infra dev test migrate seed sandbox

VENV ?= .venv
UVICORN ?= $(VENV)/bin/uvicorn
PYTEST ?= $(VENV)/bin/pytest
ALEMBIC ?= $(VENV)/bin/alembic

# Compose maps service ports to localhost, so host-run processes use localhost URLs.
LOCAL_DATABASE_URL ?= postgresql+psycopg://orchestrator:orchestrator@localhost:5432/orchestrator

infra:  ## start infra services only (postgres, redis, chromadb)
	docker compose up -d postgres redis chromadb

dev:  ## run the API locally in inline mode (no worker needed)
	RUN_MODE=inline $(UVICORN) orchestrator.main:app --reload --port 8080

test:  ## unit + integration tests; deterministic, no API keys needed
	MOCK_LLM=1 $(PYTEST) tests -q -m "not live"

migrate:  ## apply Alembic migrations to the composed postgres
	DATABASE_URL=$(LOCAL_DATABASE_URL) $(ALEMBIC) upgrade head

seed:  ## seed the demo schema used by the db_query tool
	DATABASE_URL=$(LOCAL_DATABASE_URL) $(VENV)/bin/python -m orchestrator.db.seed_demo_data

sandbox:  ## build the code-execution sandbox image
	docker build -t orchestrator-sandbox -f docker/sandbox.Dockerfile docker/
