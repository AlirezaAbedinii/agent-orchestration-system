"""SQLAlchemy ORM models.

Phase 0 ships only the declarative base (empty schema baseline); tables for
tasks, plans, subtasks, and tool invocations arrive in Phase 1.
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
