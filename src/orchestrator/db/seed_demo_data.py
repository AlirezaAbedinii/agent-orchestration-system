"""Seed the `demo` schema queried by the db_query tool. Idempotent."""

from __future__ import annotations

from sqlalchemy import Engine

from orchestrator.db.session import get_engine


def seed(engine: Engine | None = None) -> None:
    engine = engine or get_engine()
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE SCHEMA IF NOT EXISTS demo")
        connection.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS demo.vector_db_stats (
                name TEXT PRIMARY KEY,
                github_stars INTEGER NOT NULL,
                license TEXT NOT NULL
            )
            """
        )
        connection.exec_driver_sql(
            """
            INSERT INTO demo.vector_db_stats (name, github_stars, license) VALUES
                ('chroma', 21000, 'Apache-2.0'),
                ('qdrant', 24000, 'Apache-2.0'),
                ('weaviate', 13000, 'BSD-3-Clause')
            ON CONFLICT (name) DO UPDATE
                SET github_stars = EXCLUDED.github_stars, license = EXCLUDED.license
            """
        )


if __name__ == "__main__":
    seed()
    print("demo schema seeded")
