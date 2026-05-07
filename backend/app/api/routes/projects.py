from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.domain.schemas import ApiResponse, CreateProjectRequest, GenerateBatchRequest
from app.services.feishu_storyboard import FeishuStoryboardService
from app.services.projects import ProjectService
from app.services.workflow import WorkflowError, WorkflowService

router = APIRouter()


@router.post("", response_model=ApiResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_project(payload: CreateProjectRequest, request: Request, db: Session = Depends(get_db)) -> ApiResponse:
    project = ProjectService(db).create_project(payload)
    return ApiResponse(
        request_id=request.headers.get("x-request-id", f"req_{uuid4().hex}"),
        message="项目已创建",
        data={"project_id": str(project.id), "project_name": payload.name},
    )


@router.post("/{project_id}/sync", response_model=ApiResponse, status_code=status.HTTP_202_ACCEPTED)
async def sync_project(project_id: UUID, request: Request, db: Session = Depends(get_db)) -> ApiResponse:
    project = ProjectService(db).get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")
    shots = await FeishuStoryboardService(db).sync_from_feishu(project)
    return ApiResponse(
        request_id=request.headers.get("x-request-id", f"req_{uuid4().hex}"),
        message="分镜表已同步",
        data={"project_id": str(project_id), "shots": len(shots)},
    )


@router.post("/{project_id}/generate-current-batch", response_model=ApiResponse, status_code=status.HTTP_202_ACCEPTED)
async def generate_current_batch(
    project_id: UUID,
    payload: GenerateBatchRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> ApiResponse:
    try:
        project = ProjectService(db).get_project(project_id)
        if project and project.feishu_app_token:
            shots = await FeishuStoryboardService(db).generate_current_batch(project=project, batch_no=payload.batch_no)
            jobs = []
        else:
            jobs = await WorkflowService(db).generate_batch_frames_async(project_id, payload.batch_no)
            shots = []
    except WorkflowError as exc:
        raise HTTPException(status_code=400, detail={"error_code": exc.code, "message": exc.message}) from exc
    return ApiResponse(
        request_id=request.headers.get("x-request-id", f"req_{uuid4().hex}"),
        message="当前批次帧生成完成",
        data={
            "project_id": str(project_id),
            "batch_no": payload.batch_no,
            "job_ids": [str(job.id) for job in jobs],
            "shots": len(shots),
        },
    )
