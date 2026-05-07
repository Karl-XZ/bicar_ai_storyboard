from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.domain.schemas import ApiResponse, ArchiveShotRequest, RejectShotRequest
from app.services.workflow import WorkflowError, WorkflowService

router = APIRouter()


@router.post("/{shot_id}/optimize-prompt", response_model=ApiResponse, status_code=status.HTTP_202_ACCEPTED)
def optimize_prompt(shot_id: UUID, request: Request, db: Session = Depends(get_db)) -> ApiResponse:
    try:
        job = WorkflowService(db).optimize_prompt(shot_id)
    except WorkflowError as exc:
        raise HTTPException(status_code=400, detail={"error_code": exc.code, "message": exc.message}) from exc
    return ApiResponse(
        request_id=request.headers.get("x-request-id", f"req_{uuid4().hex}"),
        message="Prompt 优化完成",
        data={"shot_id": str(shot_id), "job_id": str(job.id)},
    )


@router.post("/{shot_id}/generate-frames", response_model=ApiResponse, status_code=status.HTTP_202_ACCEPTED)
def generate_frames(shot_id: UUID, request: Request, db: Session = Depends(get_db)) -> ApiResponse:
    try:
        jobs = WorkflowService(db).generate_frames(shot_id)
    except WorkflowError as exc:
        raise HTTPException(status_code=400, detail={"error_code": exc.code, "message": exc.message}) from exc
    return ApiResponse(
        request_id=request.headers.get("x-request-id", f"req_{uuid4().hex}"),
        message="帧图生成完成",
        data={"shot_id": str(shot_id), "job_ids": [str(job.id) for job in jobs]},
    )


@router.post("/{shot_id}/generate-video", response_model=ApiResponse, status_code=status.HTTP_202_ACCEPTED)
def generate_video(shot_id: UUID, request: Request, db: Session = Depends(get_db)) -> ApiResponse:
    try:
        job = WorkflowService(db).generate_video(shot_id)
    except WorkflowError as exc:
        raise HTTPException(status_code=400, detail={"error_code": exc.code, "message": exc.message}) from exc
    return ApiResponse(
        request_id=request.headers.get("x-request-id", f"req_{uuid4().hex}"),
        message="视频生成完成",
        data={"shot_id": str(shot_id), "job_id": str(job.id)},
    )


@router.post("/{shot_id}/approve", response_model=ApiResponse)
def approve_shot(shot_id: UUID, request: Request, db: Session = Depends(get_db)) -> ApiResponse:
    try:
        shot = WorkflowService(db).approve_shot(shot_id)
    except WorkflowError as exc:
        raise HTTPException(status_code=400, detail={"error_code": exc.code, "message": exc.message}) from exc
    return ApiResponse(
        request_id=request.headers.get("x-request-id", f"req_{uuid4().hex}"),
        message="镜头已通过",
        data={"shot_id": str(shot.id), "status": shot.status},
    )


@router.post("/{shot_id}/reject", response_model=ApiResponse)
def reject_shot(shot_id: UUID, payload: RejectShotRequest, request: Request, db: Session = Depends(get_db)) -> ApiResponse:
    try:
        shot = WorkflowService(db).reject_shot(shot_id, payload.reason)
    except WorkflowError as exc:
        raise HTTPException(status_code=400, detail={"error_code": exc.code, "message": exc.message}) from exc
    return ApiResponse(
        request_id=request.headers.get("x-request-id", f"req_{uuid4().hex}"),
        message="镜头已驳回",
        data={"shot_id": str(shot.id), "reason": payload.reason},
    )


@router.post("/{shot_id}/archive", response_model=ApiResponse, status_code=status.HTTP_202_ACCEPTED)
def archive_shot(shot_id: UUID, payload: ArchiveShotRequest, request: Request, db: Session = Depends(get_db)) -> ApiResponse:
    try:
        archive = WorkflowService(db).archive_shot(shot_id, payload.satisfaction)
    except WorkflowError as exc:
        raise HTTPException(status_code=400, detail={"error_code": exc.code, "message": exc.message}) from exc
    return ApiResponse(
        request_id=request.headers.get("x-request-id", f"req_{uuid4().hex}"),
        message="归档完成",
        data={"shot_id": str(shot_id), "satisfaction": payload.satisfaction, "archive_asset_id": str(archive.id)},
    )
