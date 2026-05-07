import re
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.adapters.feishu_signature import parse_event_body
from app.core.config import settings
from app.db.session import get_db
from app.domain.schemas import ApiResponse
from app.services.bot_commands import _is_help_command, _parse_create_project_command, handle_bot_text, handle_card_action
from app.services.feishu_storyboard import FeishuStoryboardService
from app.services.projects import ProjectService

router = APIRouter()


@router.post("/events", status_code=status.HTTP_202_ACCEPTED)
async def feishu_events(request: Request, db: Session = Depends(get_db)):
    body = await request.body()
    event = parse_event_body(body, settings.feishu_encrypt_key)
    if event.get("type") == "url_verification":
        return JSONResponse({"challenge": event.get("challenge")})
    if settings.feishu_verification_token and event.get("token") not in ("", None, settings.feishu_verification_token):
        raise HTTPException(status_code=401, detail="invalid Feishu verification token")
    event_type = event.get("header", {}).get("event_type") or event.get("type")
    if event_type == "im.message.receive_v1":
        message = event.get("event", {}).get("message", {})
        text = _message_text(message)
        chat_id = message.get("chat_id") or settings.feishu_default_chat_id
        result = await handle_bot_text(db, text=text, chat_id=chat_id)
        if result:
            return ApiResponse(
                request_id=request.headers.get("x-request-id", f"req_{uuid4().hex}"),
                message=result["message"],
                data=result["data"],
            )
    return ApiResponse(
        request_id=request.headers.get("x-request-id", f"req_{uuid4().hex}"),
        message="飞书事件已接收",
        data={"event_type": event_type, "job_id": str(uuid4())},
    )


@router.post("/card-actions", response_model=ApiResponse, status_code=status.HTTP_202_ACCEPTED)
async def feishu_card_actions(request: Request, db: Session = Depends(get_db)) -> ApiResponse:
    body = await request.body()
    payload = parse_event_body(body, settings.feishu_encrypt_key)
    value = payload.get("action", {}).get("value", {}) or {}
    result = await handle_card_action(db, value=value)
    return ApiResponse(
        request_id=request.headers.get("x-request-id", f"req_{uuid4().hex}"),
        message=result["message"],
        data=result["data"],
    )


@router.post("/bitable-trigger", response_model=ApiResponse, status_code=status.HTTP_202_ACCEPTED)
async def feishu_bitable_trigger(request: Request, db: Session = Depends(get_db)) -> ApiResponse:
    payload = parse_event_body(await request.body(), settings.feishu_encrypt_key)
    event = payload.get("event", payload)
    app_token = event.get("app_token") or event.get("appToken")
    table_id = event.get("table_id") or event.get("tableId")
    project = ProjectService(db).find_by_feishu_table(app_token, table_id) if app_token else None
    processed = 0
    if project:
        service = FeishuStoryboardService(db)
        record = event.get("record")
        if record:
            await service.process_record_status(project=project, record=record)
            processed = 1
        else:
            shots = await service.sync_from_feishu(project)
            processed = len(shots)
    return ApiResponse(
        request_id=request.headers.get("x-request-id", f"req_{uuid4().hex}"),
        message="多维表格触发器已接收",
        data={"processed": processed, "job_id": str(uuid4())},
    )


def _message_text(message: dict) -> str:
    import json

    content = message.get("content") or ""
    if isinstance(content, str):
        try:
            parsed = json.loads(content)
            return _strip_mentions(str(parsed.get("text") or ""), message)
        except json.JSONDecodeError:
            return _strip_mentions(content, message)
    if isinstance(content, dict):
        return _strip_mentions(str(content.get("text") or ""), message)
    return ""


def _strip_mentions(text: str, message: dict) -> str:
    cleaned = text.strip()
    for mention in message.get("mentions") or []:
        key = mention.get("key")
        if key:
            cleaned = cleaned.replace(f"@{key}", "")
    # Feishu group messages often arrive as "@_user_1 help" or "@机器人 帮助".
    cleaned = re.sub(r"^(?:@\S+\s*)+", "", cleaned).strip()
    return cleaned
