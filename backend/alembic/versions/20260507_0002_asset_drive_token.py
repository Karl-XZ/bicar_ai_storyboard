"""add drive token for uploaded assets

Revision ID: 20260507_0002
Revises: 20260506_0001
Create Date: 2026-05-07
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260507_0002"
down_revision: str | None = "20260506_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("assets", sa.Column("feishu_drive_token", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("assets", "feishu_drive_token")
