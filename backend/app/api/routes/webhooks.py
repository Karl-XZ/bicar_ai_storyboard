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
        event_data = event.get("event", {})
        message = event_data.get("message", {})
        sender = event_data.get("sender", {}) or {}
        sender_id = sender.get("sender_id", {}) or {}
        text = _message_text(message)
        chat_id = message.get("chat_id") or settings.feishu_default_chat_id
        result = await handle_bot_text(
            db,
            text=text,
            chat_id=chat_id,
            chat_type=message.get("chat_type"),
            sender_open_id=sender_id.get("open_id"),
            source_message_id=message.get("message_id"),
        )
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
            return _merge_text_and_references(parsed, message)
        except json.JSONDecodeError:
            return _strip_mentions(content, message)
    if isinstance(content, dict):
        return _merge_text_and_references(content, message)
    return ""


def _merge_text_and_references(content: dict, message: dict) -> str:
    text = _strip_mentions(_extract_display_text(content), message)
    references = _extract_reference_tokens(content)
    if not text and not references:
        return "我收到了一个飞书附件或特殊格式消息，但没有解析到正文。请补充一句说明，或直接发送文档/文件链接。"
    if not references:
        return text
    parts = [part for part in [text, *references] if part]
    return "\n".join(parts).strip()


def _extract_reference_tokens(value) -> list[str]:
    refs: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in {
                "file_key",
                "file_token",
                "filetoken",
                "token",
                "image_key",
                "image_token",
                "media_id",
                "media_key",
                "resource_token",
            } and isinstance(item, str) and item.strip():
                refs.append(f"feishu://{item.strip()}")
            elif lowered in {"image_keys", "file_keys", "tokens"} and isinstance(item, list):
                for nested in item:
                    if isinstance(nested, str) and nested.strip():
                        refs.append(f"feishu://{nested.strip()}")
            elif lowered in {"url", "link", "href"} and isinstance(item, str) and item.strip():
                if item.startswith(("http://", "https://")):
                    refs.append(item.strip())
            else:
                refs.extend(_extract_reference_tokens(item))
    elif isinstance(value, list):
        for item in value:
            refs.extend(_extract_reference_tokens(item))
    return list(dict.fromkeys(refs))


def _extract_display_text(value) -> str:
    post_text = _extract_post_text(value)
    if post_text:
        return post_text
    if isinstance(value, dict):
        for key in ("text", "title", "file_name", "filename", "image_name", "name"):
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                return item.strip()
        tag = str(value.get("tag") or "").lower()
        if tag == "a":
            label = value.get("text")
            href = value.get("href")
            if isinstance(label, str) and label.strip():
                return label.strip()
            if isinstance(href, str) and href.strip():
                return href.strip()
        if tag == "at":
            for key in ("user_name", "text", "name"):
                item = value.get(key)
                if isinstance(item, str) and item.strip():
                    return item.strip()
        for item in value.values():
            extracted = _extract_display_text(item)
            if extracted:
                return extracted
        return ""
    if isinstance(value, list):
        for item in value:
            extracted = _extract_display_text(item)
            if extracted:
                return extracted
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


def _extract_post_text(value) -> str:
    post = None
    if isinstance(value, dict):
        if isinstance(value.get("post"), dict):
            post = value.get("post")
        elif str(value.get("type") or "").lower() == "post":
            post = value
    if not isinstance(post, dict):
        return ""

    locale_payloads = []
    if "content" in post or "title" in post:
        locale_payloads.append(post)
    else:
        for item in post.values():
            if isinstance(item, dict) and ("content" in item or "title" in item):
                locale_payloads.append(item)

    for payload in locale_payloads:
        rendered = _render_post_payload(payload)
        if rendered:
            return rendered
    return ""


def _render_post_payload(payload: dict) -> str:
    lines: list[str] = []
    title = payload.get("title")
    if isinstance(title, str) and title.strip():
        lines.append(title.strip())

    content = payload.get("content")
    if isinstance(content, list):
        for row in content:
            row_parts: list[str] = []
            if isinstance(row, list):
                for block in row:
                    part = _render_post_block(block)
                    if part:
                        row_parts.append(part)
            else:
                part = _render_post_block(row)
                if part:
                    row_parts.append(part)
            row_text = "".join(row_parts).strip()
            if row_text:
                lines.append(row_text)
    return "\n".join(lines).strip()


def _render_post_block(block) -> str:
    if isinstance(block, str):
        return block.strip()
    if not isinstance(block, dict):
        return ""

    tag = str(block.get("tag") or "").lower()
    if tag == "text":
        text = block.get("text")
        return text.strip() if isinstance(text, str) else ""
    if tag == "a":
        text = block.get("text")
        href = block.get("href")
        if isinstance(text, str) and text.strip():
            if isinstance(href, str) and href.strip():
                return f"{text.strip()} ({href.strip()})"
            return text.strip()
        return href.strip() if isinstance(href, str) else ""
    if tag == "at":
        for key in ("user_name", "text", "name"):
            value = block.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""
    if tag in {"md", "plain_text"}:
        text = block.get("text")
        return text.strip() if isinstance(text, str) else ""

    return _extract_display_text({k: v for k, v in block.items() if k != "tag"})
