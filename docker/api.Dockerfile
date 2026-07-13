# Orchestration API (FastAPI + uvicorn). The same image backs the one-shot
# `migrate` compose service, so it carries alembic.ini and the migrations tree.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

COPY alembic.ini ./
# Recorded LLM fixtures so the composed stack can run key-free under MOCK_LLM=1.
COPY tests/fixtures ./tests/fixtures

EXPOSE 8080
CMD ["uvicorn", "orchestrator.main:app", "--host", "0.0.0.0", "--port", "8080"]
