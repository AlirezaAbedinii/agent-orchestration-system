import os

# Tests never call real providers; set before any orchestrator import.
os.environ.setdefault("MOCK_LLM", "1")

import pytest
from fastapi.testclient import TestClient

from orchestrator.main import create_app


@pytest.fixture()
def client() -> TestClient:
    return TestClient(create_app())
