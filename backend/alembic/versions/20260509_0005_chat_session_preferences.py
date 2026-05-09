"""add chatbot session preferences

Revision ID: 20260509_0005
Revises: 20260507_0004
Create Date: 2026-05-09
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260509_0005"
down_revision: str | None = "20260507_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chat_session_preferences",
        sa.Column("session_key", sa.String(length=255), nullable=False),
        sa.Column("session_type", sa.String(length=32), nullable=False),
        sa.Column("chat_id", sa.String(length=255), nullable=True),
        sa.Column("sender_open_id", sa.String(length=255), nullable=True),
        sa.Column("chatbot_text_model", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("session_key", name=op.f("pk_chat_session_preferences")),
    )
    op.create_index(op.f("ix_chat_session_preferences_chat_id"), "chat_session_preferences", ["chat_id"], unique=False)
    op.create_index(
        op.f("ix_chat_session_preferences_sender_open_id"),
        "chat_session_preferences",
        ["sender_open_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_chat_session_preferences_sender_open_id"), table_name="chat_session_preferences")
    op.drop_index(op.f("ix_chat_session_preferences_chat_id"), table_name="chat_session_preferences")
    op.drop_table("chat_session_preferences")
