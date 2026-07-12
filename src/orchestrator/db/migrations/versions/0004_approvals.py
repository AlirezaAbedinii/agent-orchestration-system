"""approvals queue

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-12

"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "approvals",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("task_id", sa.String(32), sa.ForeignKey("tasks.id"), nullable=False, index=True),
        sa.Column("gate_key", sa.String(64), nullable=False),
        sa.Column("trigger", sa.String(32), nullable=False),
        sa.Column("level", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("context", sa.JSON(), nullable=False),
        sa.Column("proposed_action", sa.JSON(), nullable=True),
        sa.Column("reasoning", sa.Text(), nullable=True),
        sa.Column("resolution_action", sa.String(16), nullable=True),
        sa.Column("resolution_payload", sa.JSON(), nullable=True),
        sa.Column("resolution_notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_seconds", sa.Float(), nullable=True),
        sa.UniqueConstraint("task_id", "gate_key", name="uq_approvals_task_gate"),
    )


def downgrade() -> None:
    op.drop_table("approvals")
