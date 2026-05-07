from uuid import UUID

from sqlalchemy.orm import Session

from app.domain.enums import JobStatus
from app.models.job import GenerationJob


class RecoveryService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def cancel_job(self, job_id: UUID) -> GenerationJob:
        job = self._require_job(job_id)
        job.status = JobStatus.CANCELLED.value
        self.db.commit()
        return job

    def mark_stale(self, job_id: UUID) -> GenerationJob:
        job = self._require_job(job_id)
        job.status = JobStatus.STALE.value
        self.db.commit()
        return job

    def move_to_retrying(self, job_id: UUID) -> GenerationJob:
        job = self._require_job(job_id)
        job.status = JobStatus.RETRYING.value
        job.retry_count += 1
        self.db.commit()
        return job

    def _require_job(self, job_id: UUID) -> GenerationJob:
        job = self.db.get(GenerationJob, job_id)
        if not job:
            raise ValueError(f"job not found: {job_id}")
        return job

