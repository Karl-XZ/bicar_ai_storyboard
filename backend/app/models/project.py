from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDMixin


class Project(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "projects"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    feishu_app_token: Mapped[str | None] = mapped_column(String(255))
    feishu_table_id: Mapped[str | None] = mapped_column(String(255))
    feishu_folder_token: Mapped[str | None] = mapped_column(String(255))
    model_config: Mapped[dict] = mapped_column(JSON, default=dict)
    workflow_config: Mapped[dict] = mapped_column(JSON, default=dict)

