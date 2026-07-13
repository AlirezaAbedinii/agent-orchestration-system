"""spans, llm_calls, tasks.replay_of

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-13

"""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "spans",
        sa.Column("id", sa.String(16), primary_key=True),
        sa.Column("trace_id", sa.String(32), nullable=False, index=True),
        sa.Column("parent_id", sa.String(16), nullable=True),
        sa.Column("task_id", sa.String(32), nullable=False, index=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("agent", sa.String(16), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attributes", sa.JSON(), nullable=False),
        sa.Column("start_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_ms", sa.Float(), nullable=False),
    )
    op.create_table(
        "llm_calls",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("span_id", sa.String(16), nullable=True, index=True),
        sa.Column("task_id", sa.String(32), nullable=True, index=True),
        sa.Column("agent", sa.String(16), nullable=False),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("response", sa.Text(), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.add_column("tasks", sa.Column("replay_of", sa.String(32), nullable=True))


def downgrade() -> None:
    op.drop_column("tasks", "replay_of")
    op.drop_table("llm_calls")
    op.drop_table("spans")
