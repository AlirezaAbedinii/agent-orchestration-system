"""FastAPI application factory for the orchestration API."""

from fastapi import FastAPI

from orchestrator.config import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Agent Orchestration System", version="0.1.0")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "run_mode": settings.run_mode}

    return app


app = create_app()
