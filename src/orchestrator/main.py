"""FastAPI application factory for the orchestration API."""

from fastapi import FastAPI

from orchestrator.api.routes.approvals import router as approvals_router
from orchestrator.api.routes.memory import router as memory_router
from orchestrator.api.routes.replay import router as replay_router
from orchestrator.api.routes.tasks import router as tasks_router
from orchestrator.api.routes.traces import router as traces_router
from orchestrator.config import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Agent Orchestration System", version="0.1.0")
    app.include_router(tasks_router)
    app.include_router(memory_router)
    app.include_router(approvals_router)
    app.include_router(traces_router)
    app.include_router(replay_router)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "run_mode": settings.run_mode}

    return app


app = create_app()
