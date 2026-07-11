"""add active project binding to chat preferences

Revision ID: 20260527_0009
Revises: 20260520_0008
Create Date: 2026-05-27 00:09:00
"""

from alembic import op
import sqlalchemy as sa


revision = "20260527_0009"
down_revision = "20260520_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chat_session_preferences",
        sa.Column("active_project_id", sa.String(length=36), nullable=True),
    )
    op.create_index(
        "ix_chat_session_preferences_active_project_id",
        "chat_session_preferences",
        ["active_project_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_chat_session_preferences_active_project_id", table_name="chat_session_preferences")
    op.drop_column("chat_session_preferences", "active_project_id")
