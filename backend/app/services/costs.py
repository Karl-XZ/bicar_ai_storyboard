from uuid import UUID

from sqlalchemy.orm import Session

from app.models.audit import CostLog


class CostService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def record(
        self,
        *,
        project_id: UUID,
        shot_id: UUID | None,
        provider: str,
        model_id: str,
        usage: dict,
        estimated_cost: dict | None = None,
        actual_cost: dict | None = None,
    ) -> CostLog:
        log = CostLog(
            project_id=project_id,
            shot_id=shot_id,
            provider=provider,
            model_id=model_id,
            usage=usage,
            estimated_cost=estimated_cost or {},
            actual_cost=actual_cost or {},
        )
        self.db.add(log)
        self.db.flush()
        return log

