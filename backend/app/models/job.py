from sqlalchemy import ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import JobStatus
from app.models.base import GUID, Base, TimestampMixin, UUIDMixin


class GenerationJob(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "generation_jobs"
    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_generation_jobs_idempotency_key"),)

    project_id: Mapped[str] = mapped_column(GUID(), ForeignKey("projects.id"), nullable=False, index=True)
    shot_id: Mapped[str | None] = mapped_column(GUID(), ForeignKey("shots.id"), index=True)
    job_type: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(128))
    model_id: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(64), nullable=False, default=JobStatus.QUEUED.value)
    prompt_version: Mapped[int | None] = mapped_column(Integer)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    input_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    output_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    provider_task_id: Mapped[str | None] = mapped_column(String(255), index=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[str | None] = mapped_column(String(128))
    error_message: Mapped[str | None] = mapped_column(Text)
