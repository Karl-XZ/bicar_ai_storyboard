from uuid import uuid4

from fastapi import APIRouter, Request, status

from app.domain.schemas import ApiResponse

router = APIRouter()


@router.post("/seedance", response_model=ApiResponse, status_code=status.HTTP_202_ACCEPTED)
async def seedance_webhook(request: Request) -> ApiResponse:
    return ApiResponse(
        request_id=request.headers.get("x-request-id", f"req_{uuid4().hex}"),
        message="Seedance 回调已接收",
        data={"job_id": str(uuid4())},
    )

