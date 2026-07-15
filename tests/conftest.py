import os

# Tests never call real providers; set before any orchestrator import.
os.environ.setdefault("MOCK_LLM", "1")
# Tests run code_exec via the isolated subprocess backend (no Docker needed).
os.environ.setdefault("CODE_EXEC_BACKEND", "subprocess")
# Tests run graphs in-process; a developer .env set to RUN_MODE=celery would
# otherwise enqueue every test task to whatever worker is listening.
os.environ["RUN_MODE"] = "inline"

import pytest
from fastapi.testclient import TestClient

from orchestrator.main import create_app


@pytest.fixture()
def client() -> TestClient:
    return TestClient(create_app())
