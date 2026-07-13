# Celery worker (+ embedded beat for the memory maintenance jobs).
# The leading layers match api.Dockerfile so the pip layer is shared.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

# Recorded LLM fixtures so the composed stack can run key-free under MOCK_LLM=1.
COPY tests/fixtures ./tests/fixtures

# Docker CLI only — code_exec launches sibling sandbox containers through the
# host socket that compose mounts into this service.
COPY --from=docker:27-cli /usr/local/bin/docker /usr/local/bin/docker

CMD ["celery", "-A", "orchestrator.workers.celery_app", "worker", "--beat", "--loglevel", "INFO"]
