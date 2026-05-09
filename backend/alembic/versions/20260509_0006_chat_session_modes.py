"""add chatbot session modes

Revision ID: 20260509_0006
Revises: 20260509_0005
Create Date: 2026-05-09
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260509_0006"
down_revision: str | None = "20260509_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "chat_session_preferences",
        sa.Column("assistant_mode", sa.String(length=32), nullable=False, server_default="chat"),
    )


def downgrade() -> None:
    op.drop_column("chat_session_preferences", "assistant_mode")
