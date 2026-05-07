from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.domain.enums import JobStatus, JobType
from app.models.job import GenerationJob


class JobService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get(self, job_id: UUID) -> GenerationJob | None:
        return self.db.get(GenerationJob, job_id)

    def get_or_create(
        self,
        *,
        project_id: UUID,
        shot_id: UUID | None,
        job_type: JobType,
        idempotency_key: str,
        input_payload: dict,
        provider: str | None = None,
        model_id: str | None = None,
        prompt_version: int | None = None,
    ) -> GenerationJob:
        existing = self.db.scalar(select(GenerationJob).where(GenerationJob.idempotency_key == idempotency_key))
        if existing:
            return existing

        job = GenerationJob(
            project_id=project_id,
            shot_id=shot_id,
            job_type=job_type.value,
            provider=provider,
            model_id=model_id,
            status=JobStatus.QUEUED.value,
            prompt_version=prompt_version,
            idempotency_key=idempotency_key,
            input_payload=input_payload,
            output_payload={},
        )
        self.db.add(job)
        try:
            self.db.flush()
        except IntegrityError:
            self.db.rollback()
            existing = self.db.scalar(select(GenerationJob).where(GenerationJob.idempotency_key == idempotency_key))
            if existing:
                return existing
            raise
        return job

    def mark_running(self, job: GenerationJob) -> GenerationJob:
        job.status = JobStatus.RUNNING.value
        self.db.flush()
        return job

    def mark_succeeded(self, job: GenerationJob, output_payload: dict | None = None) -> GenerationJob:
        job.status = JobStatus.SUCCEEDED.value
        if output_payload is not None:
            job.output_payload = output_payload
        self.db.flush()
        return job

    def mark_failed(self, job: GenerationJob, error_code: str, error_message: str) -> GenerationJob:
        job.status = JobStatus.FAILED.value
        job.error_code = error_code
        job.error_message = error_message
        self.db.flush()
        return job

