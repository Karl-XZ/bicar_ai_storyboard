from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import GUID, Base, TimestampMixin, UUIDMixin


class AuditLog(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "audit_logs"

    project_id: Mapped[str | None] = mapped_column(GUID(), index=True)
    shot_id: Mapped[str | None] = mapped_column(GUID(), index=True)
    actor_open_id: Mapped[str | None] = mapped_column(String(255))
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


class CostLog(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "cost_logs"

    project_id: Mapped[str] = mapped_column(GUID(), index=True)
    shot_id: Mapped[str | None] = mapped_column(GUID(), index=True)
    provider: Mapped[str] = mapped_column(String(128), nullable=False)
    model_id: Mapped[str] = mapped_column(String(255), nullable=False)
    usage: Mapped[dict] = mapped_column(JSON, default=dict)
    estimated_cost: Mapped[dict] = mapped_column(JSON, default=dict)
    actual_cost: Mapped[dict] = mapped_column(JSON, default=dict)
