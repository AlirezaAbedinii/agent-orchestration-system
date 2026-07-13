.PHONY: infra dev test e2e demo migrate seed sandbox review-ui trace-ui

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
	MOCK_LLM=1 $(PYTEST) tests/unit tests/integration -q -m "not live"

e2e:  ## the six guide-mandated e2e tests + full lifecycle (MOCK_LLM, no keys)
	MOCK_LLM=1 $(PYTEST) tests/e2e -q -m "not live"

demo:  ## scripted showcase scenario against the composed stack (docker compose up first)
	$(VENV)/bin/python scripts/run_demo.py

migrate:  ## apply Alembic migrations to the composed postgres
	DATABASE_URL=$(LOCAL_DATABASE_URL) $(ALEMBIC) upgrade head

seed:  ## seed the demo schema used by the db_query tool
	DATABASE_URL=$(LOCAL_DATABASE_URL) $(VENV)/bin/python -m orchestrator.db.seed_demo_data

sandbox:  ## build the code-execution sandbox image
	docker build -t orchestrator-sandbox -f docker/sandbox.Dockerfile docker/

review-ui:  ## human review queue UI (port 8511; 8501 tends to be taken)
	ORCHESTRATOR_API_URL=http://localhost:8080 $(VENV)/bin/streamlit run ui/review_app.py --server.port 8511 --server.headless true

trace-ui:  ## trace explorer: span trees, costs, replay (port 8512)
	ORCHESTRATOR_API_URL=http://localhost:8080 $(VENV)/bin/streamlit run ui/trace_explorer.py --server.port 8512 --server.headless true
