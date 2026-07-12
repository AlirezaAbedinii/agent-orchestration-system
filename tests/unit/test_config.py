from orchestrator.config import Settings


def test_defaults_are_dev_friendly():
    settings = Settings(_env_file=None)
    assert settings.run_mode == "inline"
    assert settings.plan_confidence_threshold == 0.7
    assert settings.review_score_threshold == 3
    assert settings.max_specialist_retries == 2
    assert settings.chroma_port == 8010  # host mapping of chromadb:8000


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("RUN_MODE", "celery")
    monkeypatch.setenv("MOCK_LLM", "1")
    monkeypatch.setenv("PLAN_CONFIDENCE_THRESHOLD", "0.9")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@example:5432/db")

    settings = Settings(_env_file=None)
    assert settings.run_mode == "celery"
    assert settings.mock_llm is True
    assert settings.plan_confidence_threshold == 0.9
    assert settings.database_url.endswith("@example:5432/db")
