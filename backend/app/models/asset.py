from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import GUID, Base, TimestampMixin, UUIDMixin


class Asset(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "assets"

    project_id: Mapped[str] = mapped_column(GUID(), ForeignKey("projects.id"), nullable=False, index=True)
    shot_id: Mapped[str | None] = mapped_column(GUID(), ForeignKey("shots.id"), index=True)
    asset_type: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_uri: Mapped[str] = mapped_column(String(1024), nullable=False)
    public_url: Mapped[str | None] = mapped_column(String(2048))
    feishu_file_token: Mapped[str | None] = mapped_column(String(255))
    feishu_drive_token: Mapped[str | None] = mapped_column(String(255))
    provider: Mapped[str | None] = mapped_column(String(128))
    model_id: Mapped[str | None] = mapped_column(String(255))
    prompt_hash: Mapped[str | None] = mapped_column(String(128), index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
