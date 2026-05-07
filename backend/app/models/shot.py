from sqlalchemy import ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import ShotStatus
from app.models.base import GUID, Base, TimestampMixin, UUIDMixin


class Shot(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "shots"

    project_id: Mapped[str] = mapped_column(GUID(), ForeignKey("projects.id"), nullable=False, index=True)
    feishu_record_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    shot_no: Mapped[str] = mapped_column(String(64), nullable=False)
    batch_no: Mapped[str] = mapped_column(String(64), nullable=False, default="batch_001")
    scene_description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    prompts: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(64), nullable=False, default=ShotStatus.DRAFT.value)
    prompt_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    error_code: Mapped[str | None] = mapped_column(String(128))
    error_message: Mapped[str | None] = mapped_column(Text)
