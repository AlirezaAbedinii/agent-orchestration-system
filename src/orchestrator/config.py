"""Central configuration.

Every runtime flag and threshold lives here (see PROJECT_IMPLEMENTATION_PLAN.md §6.2).
Values come from the environment or a local .env file; defaults target host-run
development against `make infra` services.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # LLM providers
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    tavily_api_key: str = ""  # optional; web search falls back to DuckDuckGo

    # Storage
    database_url: str = "postgresql+psycopg://orchestrator:orchestrator@localhost:5432/orchestrator"
    redis_url: str = "redis://localhost:6379/0"
    chroma_host: str = "localhost"
    chroma_port: int = 8010  # host mapping of chromadb:8000; the composed stack sets CHROMA_PORT=8000

    # Execution
    run_mode: Literal["inline", "celery"] = "inline"
    mock_llm: bool = False
    llm_fixtures_dir: Path = Path("tests/fixtures/llm")

    # Agent → model routing ("provider:model"); the reviewer must land on a
    # different provider than the producing agent (see llm/router.py)
    model_supervisor: str = "openai:gpt-4o"
    model_specialist: str = "openai:gpt-4o-mini"
    model_reviewer: str = "anthropic:claude-sonnet-5"

    # Memory
    working_memory_ttl_s: int = 86_400  # safety net for crashed runs; normal path clears explicitly

    # Tools
    workspace_root: Path = Path("task_workspaces")
    max_tool_iterations: int = 5
    api_call_allowlist: list[str] = ["api.github.com"]
    code_exec_backend: Literal["docker", "subprocess"] = "docker"
    code_exec_image: str = "orchestrator-sandbox"
    code_exec_timeout_s: int = 20

    # Thresholds
    plan_confidence_threshold: float = 0.7
    review_score_threshold: int = 3
    max_specialist_retries: int = 2

    # Human-in-the-loop
    approval_webhook_url: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
