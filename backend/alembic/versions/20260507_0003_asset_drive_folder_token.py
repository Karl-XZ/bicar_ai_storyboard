"""Add drive folder token to assets."""

from alembic import op
import sqlalchemy as sa


revision = "20260507_0003"
down_revision = "20260507_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("assets", sa.Column("feishu_drive_folder_token", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("assets", "feishu_drive_folder_token")
