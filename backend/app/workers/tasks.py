import asyncio
from uuid import UUID

from app.db.session import SessionLocal
from app.domain.enums import FrameType
from app.models.job import GenerationJob
from app.models.shot import Shot
from app.services.workflow import WorkflowService
from app.workers.celery_app import celery_app


@celery_app.task
def optimize_prompt(job_id: str) -> None:
    with SessionLocal() as db:
        workflow = WorkflowService(db)
        job = _require_job(db, job_id)
        shot = _require_shot(db, job.shot_id)
        asyncio.run(workflow._run_prompt_job(job, shot))
        db.commit()


@celery_app.task
def generate_image(job_id: str) -> None:
    with SessionLocal() as db:
        workflow = WorkflowService(db)
        job = _require_job(db, job_id)
        shot = _require_shot(db, job.shot_id)
        payload = job.input_payload or {}
        asyncio.run(
            workflow._run_image_job(
                job,
                shot,
                FrameType(payload["frame_type"]),
                int(payload.get("variant_index", 1)),
                payload["prompt_hash"],
            )
        )
        db.commit()


@celery_app.task
def generate_video(job_id: str) -> None:
    with SessionLocal() as db:
        workflow = WorkflowService(db)
        job = _require_job(db, job_id)
        shot = _require_shot(db, job.shot_id)
        asyncio.run(workflow._run_video_job(job, shot))
        db.commit()


@celery_app.task
def poll_video(job_id: str) -> None:
    generate_video(job_id)


@celery_app.task
def backfill_feishu(job_id: str) -> None:
    with SessionLocal() as db:
        job = _require_job(db, job_id)
        job.output_payload = {**(job.output_payload or {}), "backfill": "mocked"}
        db.commit()


@celery_app.task
def archive_video(job_id: str) -> None:
    with SessionLocal() as db:
        job = _require_job(db, job_id)
        job.output_payload = {**(job.output_payload or {}), "archive": "mocked"}
        db.commit()


def _require_job(db, job_id: str) -> GenerationJob:
    job = db.get(GenerationJob, UUID(job_id))
    if not job:
        raise RuntimeError(f"job not found: {job_id}")
    return job


def _require_shot(db, shot_id) -> Shot:
    if shot_id is None:
        raise RuntimeError("job has no shot_id")
    shot = db.get(Shot, shot_id)
    if not shot:
        raise RuntimeError(f"shot not found: {shot_id}")
    return shot
