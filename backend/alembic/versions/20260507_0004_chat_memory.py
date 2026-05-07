"""add persistent chatbot memory

Revision ID: 20260507_0004
Revises: 20260507_0003
Create Date: 2026-05-07
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260507_0004"
down_revision: str | None = "20260507_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chat_messages",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("session_key", sa.String(length=255), nullable=False),
        sa.Column("session_type", sa.String(length=32), nullable=False),
        sa.Column("chat_id", sa.String(length=255), nullable=True),
        sa.Column("sender_open_id", sa.String(length=255), nullable=True),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chat_messages")),
    )
    op.create_index(op.f("ix_chat_messages_chat_id"), "chat_messages", ["chat_id"], unique=False)
    op.create_index(op.f("ix_chat_messages_sender_open_id"), "chat_messages", ["sender_open_id"], unique=False)
    op.create_index(op.f("ix_chat_messages_session_key"), "chat_messages", ["session_key"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_chat_messages_session_key"), table_name="chat_messages")
    op.drop_index(op.f("ix_chat_messages_sender_open_id"), table_name="chat_messages")
    op.drop_index(op.f("ix_chat_messages_chat_id"), table_name="chat_messages")
    op.drop_table("chat_messages")
