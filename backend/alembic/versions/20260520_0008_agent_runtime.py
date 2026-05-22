"""add agent runtime preference

Revision ID: 20260520_0008
Revises: 20260518_0007
Create Date: 2026-05-20 00:08:00
"""

from alembic import op
import sqlalchemy as sa


revision = "20260520_0008"
down_revision = "20260518_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chat_session_preferences",
        sa.Column("agent_runtime", sa.String(length=32), nullable=False, server_default="codex"),
    )
    op.alter_column("chat_session_preferences", "agent_runtime", server_default=None)


def downgrade() -> None:
    op.drop_column("chat_session_preferences", "agent_runtime")
