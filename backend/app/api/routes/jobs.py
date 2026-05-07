from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.domain.schemas import ApiResponse
from app.services.jobs import JobService

router = APIRouter()


@router.get("/{job_id}", response_model=ApiResponse)
def get_job(job_id: UUID, request: Request, db: Session = Depends(get_db)) -> ApiResponse:
    job = JobService(db).get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    return ApiResponse(
        request_id=request.headers.get("x-request-id", f"req_{uuid4().hex}"),
        message="任务状态",
        data={
            "job_id": str(job.id),
            "status": job.status,
            "job_type": job.job_type,
            "provider": job.provider,
            "output_payload": job.output_payload,
            "error_code": job.error_code,
            "error_message": job.error_message,
        },
    )


@router.post("/{job_id}/retry", response_model=ApiResponse, status_code=status.HTTP_202_ACCEPTED)
def retry_job(job_id: UUID, request: Request) -> ApiResponse:
    return ApiResponse(
        request_id=request.headers.get("x-request-id", f"req_{uuid4().hex}"),
        message="重试任务已创建",
        data={"job_id": str(job_id), "retry_job_id": str(uuid4())},
    )
