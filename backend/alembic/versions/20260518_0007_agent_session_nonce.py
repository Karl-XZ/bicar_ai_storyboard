"""add agent session nonce

Revision ID: 20260518_0007
Revises: 20260509_0006
Create Date: 2026-05-18 00:07:00
"""

from alembic import op
import sqlalchemy as sa


revision = "20260518_0007"
down_revision = "20260509_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chat_session_preferences",
        sa.Column("agent_session_nonce", sa.Integer(), nullable=False, server_default="0"),
    )
    op.alter_column("chat_session_preferences", "agent_session_nonce", server_default=None)


def downgrade() -> None:
    op.drop_column("chat_session_preferences", "agent_session_nonce")
