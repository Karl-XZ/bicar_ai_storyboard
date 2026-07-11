from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import re
import shutil
import tempfile
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, unquote, urlparse
import time
from uuid import UUID

import httpx
from sqlalchemy.orm import Session

from app.adapters.feishu import FeishuApiError, FeishuClient
from app.adapters.feishu_cards import chatbot_reply_card, help_card
from app.core.config import settings
from app.core.model_aliases import IMAGE_MODEL_GPT2, IMAGE_MODEL_NANOBANANA, VIDEO_MODEL_XYQ, normalize_image_model, normalize_video_model
from app.models.project import Project
from app.services.chat_memory import ChatMemoryService, resolve_chat_session
from app.services.chat_preferences import ChatPreferenceService
from app.services.feishu_storyboard import FeishuStoryboardService
from app.services.feishu_workspace import FeishuWorkspaceService
from app.services.debug_paper_form import DebugPaperFormService, DebugPaperWorkspace
from app.services.projects import ProjectService
from app.services.video_downloads import (
    DOWNLOAD_STATUS_DONE,
    DOWNLOAD_STATUS_FAILED,
    DOWNLOAD_STATUS_RUNNING,
    VideoDownloadResult,
    VideoDownloadService,
)
from app.services.video_storyboard import VideoStoryboardError, VideoStoryboardService
from app.services.workflow import WorkflowService
from app.providers.router import ProviderRouter

LAST_MODEL_SMOKE_TEST_DATE = "2026-05-08"
ASSISTANT_MODE_CHAT = "chat"
ASSISTANT_MODE_AGENT = "agent"
ASSISTANT_MODE_STORYBOARD = "storyboard"
ASSISTANT_MODE_DEEP_RESEARCH = "deep_research"
ASSISTANT_MODE_STORYBOARD_BREAKDOWN = "storyboard_breakdown"
DIRECT_IMAGE_MODELS = {
    IMAGE_MODEL_NANOBANANA,
    IMAGE_MODEL_GPT2,
    "wanx2.1-t2i-turbo",
    "wanx-v1",
    "nano_banana_2",
    "gpt_image_2",
    "openai/gpt-5.4-image-2",
    "google/gemini-3.1-flash-image-preview",
}
DIRECT_VIDEO_MODELS = {
    "wan2.2-kf2v-flash",
    "wanx2.1-kf2v-plus",
    "wanx2.1-i2v-turbo",
    "wan2.2-t2v-plus",
    "seedance_2_0",
    VIDEO_MODEL_XYQ,
    "xyq_nest_video",
}
DIRECT_FIELD_ALIASES = {
    "model": {"模型", "model"},
    "prompt": {"提示词", "prompt"},
    "negative_prompt": {"负面提示词", "negative_prompt", "negative"},
    "size": {"尺寸", "size"},
    "duration_seconds": {"时长", "duration", "duration_seconds"},
    "keyframe_time_seconds": {"关键帧时间点", "关键帧时间", "keyframe_time", "keyframe_time_seconds"},
    "reference_images": {"参考图", "图片", "文件", "文件地址", "飞书地址", "reference", "reference_images"},
    "first_frame": {"首帧", "first_frame"},
    "last_frame": {"尾帧", "last_frame"},
    "keyframes": {"关键帧", "keyframes"},
}
STREAM_REPLY_TARGET_CHARS = 900
STREAM_REPLY_MAX_CHARS = 1300
LOCAL_ARTIFACT_EXTENSIONS = ("png", "jpg", "jpeg", "webp", "gif", "bmp", "mp4", "mov", "m4v", "webm", "pdf", "docx", "txt", "md")


@dataclass(frozen=True)
class DirectGenerationCommand:
    kind: str
    model: str
    prompt: str
    negative_prompt: str = ""
    size: str | None = None
    duration_seconds: float | None = None
    keyframe_time_seconds: float | None = None
    reference_images: tuple[str, ...] = ()
    first_frame: tuple[str, ...] = ()
    last_frame: tuple[str, ...] = ()
    keyframes: tuple[str, ...] = ()


@dataclass(frozen=True)
class VideoStoryboardCommand:
    video_reference: str
    project_name: str | None = None
    parent_folder_url: str | None = None
    sample_count: int = 10
    target_shots: int | None = None


@dataclass(frozen=True)
class AssistantModeCommand:
    mode: str
    prompt: str = ""
    agent_runtime: str | None = None


@dataclass(frozen=True)
class DeepResearchResult:
    markdown: str
    raw_text: str = ""
    raw_payload: Any | None = None


@dataclass(frozen=True)
class ChatRequestContext:
    session_key: str
    request_id: int


class StaleChatRequest(RuntimeError):
    pass


_ACTIVE_CHAT_REQUESTS: dict[str, int] = {}
_ACTIVE_OPENCLAW_PROCESSES: dict[str, asyncio.subprocess.Process] = {}
_RECENT_MODE_SWITCHES: dict[str, tuple[str, float]] = {}
_OPENCLAW_BUNDLED_NODE = Path("/Users/applemima111/.openclaw/tools/node-v22.22.0/bin/node")
_OPENCLAW_BUNDLED_ENTRYPOINT = Path("/Users/applemima111/.openclaw/tools/node-v22.22.0/lib/node_modules/openclaw/dist/index.js")

AGENT_RUNTIME_CODEX = "codex"
AGENT_RUNTIME_DEEPSEEK = "deepseek"
AGENT_RUNTIMES = {AGENT_RUNTIME_CODEX, AGENT_RUNTIME_DEEPSEEK}


def _begin_chat_request(*, chat_id: str | None, chat_type: str | None, sender_open_id: str | None) -> ChatRequestContext:
    session = resolve_chat_session(chat_id=chat_id, chat_type=chat_type, sender_open_id=sender_open_id)
    next_request_id = _ACTIVE_CHAT_REQUESTS.get(session.session_key, 0) + 1
    _ACTIVE_CHAT_REQUESTS[session.session_key] = next_request_id
    return ChatRequestContext(session_key=session.session_key, request_id=next_request_id)


def _invalidate_chat_session(*, chat_id: str | None, chat_type: str | None, sender_open_id: str | None) -> None:
    session = resolve_chat_session(chat_id=chat_id, chat_type=chat_type, sender_open_id=sender_open_id)
    _ACTIVE_CHAT_REQUESTS[session.session_key] = _ACTIVE_CHAT_REQUESTS.get(session.session_key, 0) + 1


def _terminate_openclaw_process_for_session(session_key: str) -> bool:
    process = _ACTIVE_OPENCLAW_PROCESSES.pop(session_key, None)
    if not process:
        return False
    try:
        if process.returncode is None:
            process.terminate()
            return True
    except ProcessLookupError:
        return False
    return False


def _normalize_agent_runtime(value: str | None) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"deepseek", "ds"}:
        return AGENT_RUNTIME_DEEPSEEK
    return AGENT_RUNTIME_CODEX


def _note_mode_switch(session_key: str, mode: str) -> None:
    _RECENT_MODE_SWITCHES[session_key] = (mode, time.monotonic())


def _is_duplicate_mode_switch(session_key: str, mode: str, window_seconds: float = 2.0) -> bool:
    previous = _RECENT_MODE_SWITCHES.get(session_key)
    if not previous:
        return False
    previous_mode, previous_at = previous
    return previous_mode == mode and (time.monotonic() - previous_at) <= window_seconds


def _is_request_current(request_context: ChatRequestContext | None) -> bool:
    if request_context is None:
        return True
    return _ACTIVE_CHAT_REQUESTS.get(request_context.session_key) == request_context.request_id


def _assert_request_current(request_context: ChatRequestContext | None) -> None:
    if not _is_request_current(request_context):
        raise StaleChatRequest("stale chat request")


def _resolve_openclaw_command() -> list[str]:
    if _OPENCLAW_BUNDLED_NODE.exists() and _OPENCLAW_BUNDLED_ENTRYPOINT.exists():
        return [str(_OPENCLAW_BUNDLED_NODE), str(_OPENCLAW_BUNDLED_ENTRYPOINT)]
    binary = shutil.which("openclaw")
    if binary:
        return [binary]
    raise RuntimeError("OpenClaw 未安装或不在 PATH 中。")


async def handle_bot_text(
    db: Session,
    *,
    text: str,
    chat_id: str | None = None,
    chat_type: str | None = None,
    sender_open_id: str | None = None,
    source_message_id: str | None = None,
) -> dict[str, Any] | None:
    target_chat = chat_id
    session_chat_id = chat_id
    feishu = FeishuClient()
    preferences = ChatPreferenceService(db)
    _maybe_bind_project_from_message(
        db,
        text=text,
        chat_id=session_chat_id,
        chat_type=chat_type,
        sender_open_id=sender_open_id,
        preferences=preferences,
    )
    command_text = _command_text(text)
    current_mode = preferences.get_assistant_mode(
        chat_id=session_chat_id,
        chat_type=chat_type,
        sender_open_id=sender_open_id,
    )

    natural_project_command = _parse_natural_project_query_command(command_text) if current_mode != ASSISTANT_MODE_AGENT else None
    if natural_project_command:
        project = _current_chat_project(
            db,
            chat_id=target_chat,
            chat_type=chat_type,
            sender_open_id=sender_open_id,
            preferences=preferences,
        )
        if not project:
            if target_chat:
                await feishu.send_text(target_chat, "还没有可操作的分镜项目，请先发送：新建分镜项目：项目名")
            return {"message": "项目不存在", "data": {"command": natural_project_command["action"]}}
        result = await handle_card_action(
            db,
            value={
                "action": natural_project_command["action"],
                "project_id": str(project.id),
                "batch_no": natural_project_command["batch_no"],
                **({"enabled": natural_project_command["enabled"] == "true"} if "enabled" in natural_project_command else {}),
            },
            chat_id=target_chat,
        )
        if target_chat:
            await feishu.send_text(target_chat, result["message"])
        return result

    if not _is_slash_command(text):
        if not str(text or "").strip():
            if target_chat:
                await feishu.send_text(target_chat, "我收到了一个图片、附件或特殊格式消息，但没有解析到可读正文。请补充一句说明，或直接发送文档/文件链接。")
            return {"message": "chatbot 未解析到正文", "data": {"chat_id": target_chat}}
        request_context = _begin_chat_request(
            chat_id=session_chat_id,
            chat_type=chat_type,
            sender_open_id=sender_open_id,
        )

        async def progress_notifier(message: str) -> None:
            if target_chat and message and _is_request_current(request_context):
                await feishu.send_text(target_chat, message)

        try:
            reply = await _reply_with_timeout_hint(
                db,
                text=text,
                chat_id=session_chat_id,
                chat_type=chat_type,
                sender_open_id=sender_open_id,
                source_message_id=source_message_id,
                progress_notifier=progress_notifier,
                hint_enabled=current_mode == ASSISTANT_MODE_CHAT,
                feishu=feishu,
                request_context=request_context,
            )
        except StaleChatRequest:
            return {"message": "chatbot 旧回复已抑制", "data": {"chat_id": target_chat}}
        except Exception as exc:
            if target_chat:
                await feishu.send_text(target_chat, _chatbot_failure_message(exc))
            return {"message": "chatbot 回复失败", "data": {"chat_id": target_chat, "error": str(exc)}}
        if target_chat and reply and _is_request_current(request_context):
            await _send_reply_segmented(
                feishu=feishu,
                target_chat=target_chat,
                content=reply,
                title=_assistant_card_title(current_mode),
                chat_id=session_chat_id,
                chat_type=chat_type,
                sender_open_id=sender_open_id,
                request_context=request_context,
            )
        return {"message": "chatbot 已回复", "data": {"chat_id": target_chat}}

    if _is_help_command(command_text):
        if target_chat:
            await feishu.send_card(target_chat, help_card())
        return {"message": "帮助已发送", "data": {"chat_id": target_chat}}

    if _is_debug_paper_qr_command(command_text):
        try:
            workspace = await DebugPaperFormService().ensure_workspace()
            reply = _debug_paper_qr_reply(workspace)
        except Exception as exc:
            reply = "\n".join(
                [
                    "调试纸飞书表单入口创建失败。",
                    "",
                    f"原因：`{type(exc).__name__}: {exc}`",
                    "",
                    "这一步需要飞书应用具备云文档/多维表格/文件夹相关权限。权限补齐后再次发送 `/调试纸二维码` 即可重试。",
                ]
            )
        if target_chat:
            await feishu.send_card(
                target_chat,
                chatbot_reply_card(content=reply, chat_id=target_chat, chat_type=chat_type, sender_open_id=sender_open_id),
            )
        return {"message": "调试纸二维码入口已发送", "data": {"chat_id": target_chat}}

    if _is_new_session_command(command_text):
        _invalidate_chat_session(
            chat_id=session_chat_id,
            chat_type=chat_type,
            sender_open_id=sender_open_id,
        )
        ChatPreferenceService(db).bump_agent_session_nonce(
            chat_id=session_chat_id,
            chat_type=chat_type,
            sender_open_id=sender_open_id,
        )
        cleared = ChatMemoryService(
            db,
            chat_id=session_chat_id,
            chat_type=chat_type,
            sender_open_id=sender_open_id,
        ).clear()
        db.commit()
        if target_chat:
            await feishu.send_text(target_chat, "当前会话的聊天记录已重置。")
        return {"message": "聊天记录已重置", "data": {"chat_id": target_chat, "cleared_messages": cleared}}

    if _is_stop_command(command_text):
        session = resolve_chat_session(chat_id=session_chat_id, chat_type=chat_type, sender_open_id=sender_open_id)
        _invalidate_chat_session(
            chat_id=session_chat_id,
            chat_type=chat_type,
            sender_open_id=sender_open_id,
        )
        ChatPreferenceService(db).bump_agent_session_nonce(
            chat_id=session_chat_id,
            chat_type=chat_type,
            sender_open_id=sender_open_id,
        )
        db.commit()
        terminated = _terminate_openclaw_process_for_session(session.session_key)
        if target_chat:
            await feishu.send_text(target_chat, "当前 Agent 任务已停止。" if terminated else "已请求停止当前会话任务；如仍有旧回复，将被自动抑制。")
        return {"message": "当前任务已停止", "data": {"chat_id": target_chat, "terminated": terminated}}

    mode_command = _parse_assistant_mode_command(command_text)
    if mode_command:
        session = resolve_chat_session(chat_id=session_chat_id, chat_type=chat_type, sender_open_id=sender_open_id)
        preferences = ChatPreferenceService(db)
        current_mode = preferences.get_assistant_mode(
            chat_id=session_chat_id,
            chat_type=chat_type,
            sender_open_id=sender_open_id,
        )
        current_runtime = preferences.get_agent_runtime(
            chat_id=session_chat_id,
            chat_type=chat_type,
            sender_open_id=sender_open_id,
        )
        duplicate_mode_switch = _is_duplicate_mode_switch(session.session_key, mode_command.mode)
        runtime_changed = False
        if mode_command.agent_runtime:
            normalized_runtime = _normalize_agent_runtime(mode_command.agent_runtime)
            runtime_changed = normalized_runtime != _normalize_agent_runtime(current_runtime)
            preferences.set_agent_runtime(
                chat_id=session_chat_id,
                chat_type=chat_type,
                sender_open_id=sender_open_id,
                runtime=normalized_runtime,
            )
        if duplicate_mode_switch and not runtime_changed and not mode_command.prompt and current_mode == mode_command.mode:
            return {"message": "聊天模式重复切换已忽略", "data": {"mode": mode_command.mode}}
        _invalidate_chat_session(
            chat_id=session_chat_id,
            chat_type=chat_type,
            sender_open_id=sender_open_id,
        )
        preferences.set_assistant_mode(
            chat_id=session_chat_id,
            chat_type=chat_type,
            sender_open_id=sender_open_id,
            mode=mode_command.mode,
        )
        if runtime_changed or current_mode == ASSISTANT_MODE_AGENT or mode_command.mode != ASSISTANT_MODE_AGENT:
            preferences.bump_agent_session_nonce(
                chat_id=session_chat_id,
                chat_type=chat_type,
                sender_open_id=sender_open_id,
            )
            _terminate_openclaw_process_for_session(session.session_key)
        db.commit()
        _note_mode_switch(session.session_key, mode_command.mode)
        if not mode_command.prompt:
            if target_chat:
                await feishu.send_text(
                    target_chat,
                    _assistant_mode_activation_hint(
                        mode_command.mode,
                        agent_runtime=preferences.get_agent_runtime(
                            chat_id=session_chat_id,
                            chat_type=chat_type,
                            sender_open_id=sender_open_id,
                        ),
                    ),
                )
            return {"message": "聊天模式已切换", "data": {"mode": mode_command.mode}}
        request_context = _begin_chat_request(
            chat_id=session_chat_id,
            chat_type=chat_type,
            sender_open_id=sender_open_id,
        )

        async def progress_notifier(message: str) -> None:
            if target_chat and message and _is_request_current(request_context):
                await feishu.send_text(target_chat, message)

        try:
            reply = await _reply_with_timeout_hint(
                db,
                text=mode_command.prompt,
                chat_id=session_chat_id,
                chat_type=chat_type,
                sender_open_id=sender_open_id,
                source_message_id=source_message_id,
                progress_notifier=progress_notifier,
                hint_enabled=mode_command.mode == ASSISTANT_MODE_CHAT,
                feishu=feishu,
                request_context=request_context,
            )
        except StaleChatRequest:
            return {"message": "聊天模式已切换，旧回复已抑制", "data": {"mode": mode_command.mode, "chat_id": target_chat}}
        except Exception as exc:
            if target_chat:
                await feishu.send_text(target_chat, _chatbot_failure_message(exc))
            return {"message": "聊天模式已切换但回复失败", "data": {"mode": mode_command.mode, "chat_id": target_chat, "error": str(exc)}}
        if target_chat and reply and _is_request_current(request_context):
            await _send_reply_segmented(
                feishu=feishu,
                target_chat=target_chat,
                content=reply,
                title=_assistant_card_title(mode_command.mode),
                chat_id=session_chat_id,
                chat_type=chat_type,
                sender_open_id=sender_open_id,
                request_context=request_context,
            )
        return {"message": "聊天模式已切换并回复", "data": {"mode": mode_command.mode, "chat_id": target_chat}}

    direct_command = _parse_direct_generation_command(command_text)
    if direct_command:
        return await _handle_direct_generation_command(
            db,
            feishu=feishu,
            target_chat=target_chat,
            chat_type=chat_type,
            sender_open_id=sender_open_id,
            command=direct_command,
        )

    video_storyboard_command = _parse_video_storyboard_command(command_text)
    if video_storyboard_command:
        async def progress_notifier(message: str) -> None:
            if target_chat and message:
                await feishu.send_text(target_chat, message)

        try:
            result = await VideoStoryboardService(db, feishu=feishu).create_project_from_video(
                video_reference=video_storyboard_command.video_reference,
                project_name=video_storyboard_command.project_name,
                chat_id=target_chat,
                parent_folder_url=video_storyboard_command.parent_folder_url,
                sample_count=video_storyboard_command.sample_count,
                target_shots=video_storyboard_command.target_shots,
                progress_notifier=progress_notifier,
            )
        except VideoStoryboardError as exc:
            if target_chat:
                await feishu.send_text(target_chat, f"视频拆分镜失败：{exc}")
            return {"message": "视频拆分镜失败", "data": {"error": str(exc)}}
        except Exception as exc:
            if target_chat:
                await feishu.send_text(target_chat, f"视频拆分镜失败：{type(exc).__name__}: {exc}")
            return {"message": "视频拆分镜失败", "data": {"error": str(exc), "type": type(exc).__name__}}
        preferences.set_active_project_id(
            chat_id=session_chat_id,
            chat_type=chat_type,
            sender_open_id=sender_open_id,
            project_id=str(result.project.id),
        )
        db.commit()
        reply = "\n".join(
            [
                "视频拆分镜完成，已创建可编辑分镜项目。",
                f"- 项目：`{result.project.name}`",
                f"- 源视频：`{result.source_filename}`",
                f"- 抽帧数：{result.frame_count}",
                f"- 分镜行数：{result.shot_count}",
                f"- 视觉模型：`{result.vision_model}`",
                f"- 表格链接：{result.table_url or '未返回'}",
                f"- 项目文件夹：{result.folder_url or '未返回'}",
                "",
                "我没有自动启动图片或视频生成。请先检查表格内容，确认后再用 `/生成全部图片` 或表格状态启动生成。",
            ]
        )
        if target_chat:
            await feishu.send_card(
                target_chat,
                chatbot_reply_card(content=reply, chat_id=target_chat, chat_type=chat_type, sender_open_id=sender_open_id),
            )
        return {"message": "视频拆分镜完成", "data": {"project_id": str(result.project.id), "table_url": result.table_url}}

    video_download_command = _parse_video_download_command(command_text)
    if video_download_command is not None:
        service = VideoDownloadService()
        request = service.parse_request_from_conversation(
            video_download_command,
            recent_texts=[],
            source_session=_session_source_label(chat_id=session_chat_id, chat_type=chat_type, sender_open_id=sender_open_id),
        )
        if not request:
            if target_chat:
                await feishu.send_text(target_chat, "请在命令后附上可下载的视频链接，例如：`/下载视频 https://...`，也可以顺手写上命名要求。")
            return {"message": "视频下载命令缺少有效链接", "data": {}}
        workspace = await service.ensure_workspace()
        result = await service.create_chat_download_task_in_workspace(workspace=workspace, request=request)
        reply = _video_download_reply(workspace=workspace, result=result)
        if target_chat:
            await feishu.send_card(
                target_chat,
                chatbot_reply_card(content=reply, chat_id=target_chat, chat_type=chat_type, sender_open_id=sender_open_id),
            )
        return {"message": "视频下载工作流已触发", "data": {"status": result.status, "record_id": result.record_id}}

    switch_model = _parse_chatbot_model_command(command_text)
    if switch_model:
        ChatPreferenceService(db).set_chatbot_text_model(
            chat_id=session_chat_id,
            chat_type=chat_type,
            sender_open_id=sender_open_id,
            model=switch_model,
        )
        db.commit()
        if target_chat:
            await feishu.send_text(target_chat, f"chatbot 文本模型已切换为：{switch_model}")
        return {"message": "chatbot 模型已切换", "data": {"model": switch_model}}

    switch_project_text = _parse_switch_current_project_command(command_text)
    if switch_project_text is not None:
        project = _find_project_from_message_links(db, switch_project_text)
        if not project:
            message = "没有从这条命令里识别到可绑定的分镜表链接，或该链接还没有对应到已知项目。请发送：`/切换当前项目 <表格链接>`。"
            if target_chat:
                await feishu.send_text(target_chat, message)
            return {"message": "切换当前项目失败", "data": {"reason": "project_not_found"}}
        preferences.set_active_project_id(
            chat_id=session_chat_id,
            chat_type=chat_type,
            sender_open_id=sender_open_id,
            project_id=str(project.id),
        )
        db.commit()
        storyboard = FeishuStoryboardService(db)
        table_url = storyboard._project_table_url(project) or "未配置"
        folder_url = storyboard._project_folder_url(project) or "未配置"
        confirmation = "\n".join(
            [
                f"当前会话已切换到项目：`{project.name}`",
                f"- 表格链接：{table_url}",
                f"- 项目文件夹链接：{folder_url}",
            ]
        )
        if target_chat:
            await feishu.send_text(target_chat, confirmation)
        return {"message": "当前项目已切换", "data": {"project_id": str(project.id)}}

    agent_runtime = _parse_agent_runtime_command(command_text)
    if agent_runtime:
        session = resolve_chat_session(chat_id=session_chat_id, chat_type=chat_type, sender_open_id=sender_open_id)
        preferences = ChatPreferenceService(db)
        preferences.set_agent_runtime(
            chat_id=session_chat_id,
            chat_type=chat_type,
            sender_open_id=sender_open_id,
            runtime=agent_runtime,
        )
        _invalidate_chat_session(
            chat_id=session_chat_id,
            chat_type=chat_type,
            sender_open_id=sender_open_id,
        )
        preferences.bump_agent_session_nonce(
            chat_id=session_chat_id,
            chat_type=chat_type,
            sender_open_id=sender_open_id,
        )
        _terminate_openclaw_process_for_session(session.session_key)
        db.commit()
        if target_chat:
            runtime_label = "Codex" if agent_runtime == AGENT_RUNTIME_CODEX else "DeepSeek"
            await feishu.send_text(target_chat, f"Agent 运行后端已切换为：{runtime_label}。当前会话下次进入 `/Agent` 或继续 Agent 对话时生效。")
        return {"message": "agent runtime 已切换", "data": {"runtime": agent_runtime}}

    project_name = _parse_create_project_command(command_text)
    if project_name:
        project_name_text, folder_url = _parse_create_project_command_parts(command_text)
        provisioned = await FeishuStoryboardService(db).create_project_from_bot(
            project_name=project_name_text,
            chat_id=target_chat,
            parent_folder_url=folder_url,
        )
        preferences.set_active_project_id(
            chat_id=session_chat_id,
            chat_type=chat_type,
            sender_open_id=sender_open_id,
            project_id=str(provisioned.project.id),
        )
        db.commit()
        return {
            "message": "项目已创建",
            "data": {"project_id": str(provisioned.project.id), "table_url": provisioned.table_url},
        }

    command = _parse_project_command(command_text)
    if command:
        project = _current_chat_project(
            db,
            chat_id=target_chat,
            chat_type=chat_type,
            sender_open_id=sender_open_id,
            preferences=preferences,
        )
        if not project:
            if target_chat:
                await feishu.send_text(target_chat, "还没有可操作的分镜项目，请先发送：新建分镜项目：项目名")
            return {"message": "项目不存在", "data": {"command": command["action"]}}
        result = await handle_card_action(
            db,
            value={
                "action": command["action"],
                "project_id": str(project.id),
                "batch_no": command["batch_no"],
                **({"enabled": command["enabled"] == "true"} if "enabled" in command else {}),
            },
            chat_id=target_chat,
        )
        if target_chat:
            await feishu.send_text(target_chat, result["message"])
        return result

    return None


async def handle_card_action(db: Session, *, value: dict[str, Any], chat_id: str | None = None) -> dict[str, Any]:
    resolved_chat_id = chat_id or value.get("chat_id")
    action = value.get("action")
    project_id = value.get("project_id")
    batch_no = value.get("batch_no") or "batch_001"
    chat_type = value.get("chat_type")
    sender_open_id = value.get("sender_open_id")
    service = FeishuStoryboardService(db)
    if not action:
        return {"message": "卡片动作已接收", "data": {"action": action}}

    if action == "assistant.clear_session":
        _invalidate_chat_session(
            chat_id=resolved_chat_id,
            chat_type=chat_type,
            sender_open_id=sender_open_id,
        )
        ChatPreferenceService(db).bump_agent_session_nonce(
            chat_id=resolved_chat_id,
            chat_type=chat_type,
            sender_open_id=sender_open_id,
        )
        cleared = ChatMemoryService(
            db,
            chat_id=resolved_chat_id,
            chat_type=chat_type,
            sender_open_id=sender_open_id,
        ).clear()
        db.commit()
        if resolved_chat_id:
            await FeishuClient().send_text(resolved_chat_id, "当前会话的聊天记录已重置。")
        return {"message": "聊天记录已重置", "data": {"chat_id": resolved_chat_id, "cleared_messages": cleared}}

    if action == "assistant.upload_recent_artifact":
        reply = await _agent_upload_recent_artifact_to_feishu(
            db,
            text="保存到飞书",
            chat_id=resolved_chat_id,
            chat_type=chat_type,
            sender_open_id=sender_open_id,
        )
        if not reply:
            reply = "当前会话最近没有可上传的本地产物。请先让 Agent 生成文件，或直接发送本地文件路径。"
        if resolved_chat_id:
            await FeishuClient().send_card(
                resolved_chat_id,
                chatbot_reply_card(
                    content=reply,
                    title="哔车AI助手 · Agent",
                    chat_id=resolved_chat_id,
                    chat_type=chat_type,
                    sender_open_id=sender_open_id,
                ),
            )
        return {"message": "Agent 产物上传已处理", "data": {"chat_id": resolved_chat_id}}

    if action == "assistant.set_mode":
        mode = str(value.get("mode") or ASSISTANT_MODE_CHAT)
        session = resolve_chat_session(chat_id=resolved_chat_id, chat_type=chat_type, sender_open_id=sender_open_id)
        preferences = ChatPreferenceService(db)
        current_mode = preferences.get_assistant_mode(
            chat_id=resolved_chat_id,
            chat_type=chat_type,
            sender_open_id=sender_open_id,
        )
        if _is_duplicate_mode_switch(session.session_key, mode) and current_mode == mode:
            return {"message": "重复模式切换已忽略", "data": {"chat_id": resolved_chat_id, "mode": mode}}
        _invalidate_chat_session(
            chat_id=resolved_chat_id,
            chat_type=chat_type,
            sender_open_id=sender_open_id,
        )
        preferences.set_assistant_mode(
            chat_id=resolved_chat_id,
            chat_type=chat_type,
            sender_open_id=sender_open_id,
            mode=mode,
        )
        if current_mode == ASSISTANT_MODE_AGENT or mode != ASSISTANT_MODE_AGENT:
            preferences.bump_agent_session_nonce(
                chat_id=resolved_chat_id,
                chat_type=chat_type,
                sender_open_id=sender_open_id,
            )
            _terminate_openclaw_process_for_session(session.session_key)
        db.commit()
        _note_mode_switch(session.session_key, mode)
        if resolved_chat_id:
            await FeishuClient().send_text(
                resolved_chat_id,
                _assistant_mode_activation_hint(
                    mode,
                    agent_runtime=preferences.get_agent_runtime(
                        chat_id=resolved_chat_id,
                        chat_type=chat_type,
                        sender_open_id=sender_open_id,
                    ),
                ),
            )
        return {"message": "聊天模式已切换", "data": {"chat_id": resolved_chat_id, "mode": mode}}

    if not project_id:
        return {"message": "卡片动作已接收", "data": {"action": action}}

    project = ProjectService(db).get_project(project_id)
    if not project:
        return {"message": "项目不存在", "data": {"project_id": project_id}}
    ChatPreferenceService(db).set_active_project_id(
        chat_id=resolved_chat_id,
        chat_type=chat_type,
        sender_open_id=sender_open_id,
        project_id=str(project.id),
    )
    db.commit()

    if action == "batch.generate_frames":
        shots = await service.generate_current_batch(project=project, batch_no=batch_no)
        return {"message": "当前批次帧图已生成", "data": {"project_id": project_id, "batch_no": batch_no, "shots": len(shots)}}

    if action == "project.generate_all_images":
        shots = await service.generate_all_images(project=project)
        return {"message": "全部图片已生成", "data": {"project_id": project_id, "shots": len(shots)}}

    if action == "project.generate_all_videos":
        shots = await service.generate_all_videos(project=project)
        return {"message": "全部视频已生成", "data": {"project_id": project_id, "shots": len(shots)}}

    if action == "project.generate_all_media":
        stats = await service.generate_all_images_and_videos(project=project)
        return {"message": "全部图片和视频已生成", "data": {"project_id": project_id, **stats}}

    if action in {"project.enable_transition_alignment", "project.set_transition_alignment"}:
        enabled = _bool_value(value.get("enabled"), default=True)
        synced = await service.set_transition_alignment(project, enabled)
        if resolved_chat_id:
            await service.send_progress(project, chat_id=resolved_chat_id)
        return {
            "message": "首尾帧同步已开启" if enabled else "首尾帧同步已关闭",
            "data": {"project_id": project_id, "synced": synced, "enabled": enabled},
        }

    if action in {"project.enable_keyframes", "project.set_keyframes"}:
        enabled = _bool_value(value.get("enabled"), default=True)
        await service.set_keyframe_generation(project, enabled)
        if resolved_chat_id:
            await service.send_progress(project, chat_id=resolved_chat_id)
        return {
            "message": "关键帧生成已开启" if enabled else "关键帧生成已关闭",
            "data": {"project_id": project_id, "enabled": enabled},
        }

    if action == "batch.optimize_prompt":
        shots = await service.optimize_current_batch(project=project, batch_no=batch_no)
        return {"message": "当前批次 Prompt 已优化", "data": {"project_id": project_id, "batch_no": batch_no, "shots": len(shots)}}

    if action == "project.progress":
        stats = await service.send_progress(project, chat_id=resolved_chat_id)
        return {"message": "项目进度已发送", "data": stats}

    if action == "project.sync":
        shots = await service.sync_from_feishu(project)
        return {"message": "分镜表已同步", "data": {"shots": len(shots)}}

    return {"message": "未知卡片动作", "data": {"action": action}}


def _bool_value(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off", "否"}


def _parse_create_project_command(text: str) -> str | None:
    parsed = _parse_create_project_command_parts(text)
    return parsed[0] if parsed else None


def _parse_create_project_command_parts(text: str) -> tuple[str, str | None] | None:
    normalized = text.strip()
    prefixes = [
        "新建分镜项目：",
        "新建分镜项目:",
        "新建 AI 分镜项目：",
        "新建 AI 分镜项目:",
        "新建项目：",
        "新建项目:",
        "新建：",
        "新建:",
        "new：",
        "new:",
    ]
    for prefix in prefixes:
        if normalized.startswith(prefix):
            name = normalized[len(prefix) :].strip()
            if not name:
                return None
            return _split_project_name_and_folder_url(name)
    if normalized == "新建 AI 分镜项目":
        return ("未命名 AI 分镜项目", None)
    return None


def _split_project_name_and_folder_url(raw: str) -> tuple[str, str | None]:
    match = re.search(r"(https?://\S+/drive/folder/\S+)", raw)
    if not match:
        return raw.strip(), None
    url = match.group(1).strip()
    name = raw[: match.start()].strip()
    return name or "未命名 AI 分镜项目", url


def _is_help_command(text: str) -> bool:
    normalized = text.strip().lower()
    return normalized in {"帮助", "help", "菜单", "命令", "指令", "使用说明", "说明"}


def _is_new_session_command(text: str) -> bool:
    normalized = _normalize_command_phrase(text)
    return normalized in {"new session", "newsession", "重置聊天", "重置会话", "清空聊天记录", "清空会话"}


def _is_stop_command(text: str) -> bool:
    normalized = _normalize_command_phrase(text)
    return normalized in {"stop", "停止", "停止当前任务", "停止当前agent任务", "停止当前智能体任务", "取消", "取消当前任务"}


def _is_debug_paper_qr_command(text: str) -> bool:
    normalized = _normalize_command_phrase(text)
    return normalized in {
        "调试纸二维码",
        "调试纸复制二维码",
        "创建调试纸二维码",
        "二维码创建调试纸",
        "二维码复制调试纸",
        "debug paper qr",
    }


def _debug_paper_qr_reply(workspace: DebugPaperWorkspace) -> str:
    return "\n".join(
        [
            "调试纸副本文档创建入口已准备好。这是飞书原生表单，不依赖外部网页。",
            "",
            f"- 创建入口：{workspace.form_url}",
            f"- 处理记录表：{workspace.table_url}",
            f"- 新文档保存文件夹：{workspace.target_folder_url}",
            "",
            "用户流程：打开/扫码创建入口 → 飞书登录 → 填写“新文档名称” → 第二行“生成后的文档打开链接”不用填 → 提交。",
            f"提交后稍等片刻，机器人会自动复制 `调试纸CN.docx`，并把新文档链接写回第二行；也可以在处理记录表查看：{workspace.table_url}",
            "",
            "表单里用户只需要填第一行名字，第二行由后端自动维护。",
        ]
    )


def _parse_agent_runtime_command(text: str) -> str | None:
    normalized = re.sub(r"\s+", " ", text.strip())
    match = re.match(r"^(?:切换agent模型|切换智能体模型|agent模型|智能体模型)\s+(\S+)$", normalized, flags=re.IGNORECASE)
    if not match:
        return None
    runtime = _normalize_agent_runtime(match.group(1))
    if runtime in AGENT_RUNTIMES:
        return runtime
    return None


def _parse_assistant_mode_command(text: str) -> AssistantModeCommand | None:
    normalized = text.strip()
    patterns = [
        (r"^(?:agent|codex|智能体|agent模式)(?:\s*[:：]\s*|\s+)?(.*)$", ASSISTANT_MODE_AGENT),
        (r"^(?:deep\s*research|deepresearch)(?:\s*[:：]\s*|\s+)?(.*)$", ASSISTANT_MODE_DEEP_RESEARCH),
        (r"^(?:拆分镜需求|分镜拆解|重新拆解|拆分镜|storyboard\s*breakdown)(?:\s*[:：]\s*|\s+)?(.*)$", ASSISTANT_MODE_STORYBOARD_BREAKDOWN),
        (r"^(?:分镜助手|ai分镜助手|视频助手)(?:\s*[:：]\s*|\s+)?(.*)$", ASSISTANT_MODE_STORYBOARD),
        (r"^(?:普通助手|聊天助手|普通对话|chat)(?:\s*[:：]\s*|\s+)?(.*)$", ASSISTANT_MODE_CHAT),
    ]
    for pattern, mode in patterns:
        match = re.match(pattern, normalized, flags=re.IGNORECASE | re.DOTALL)
        if match:
            prompt = (match.group(1) or "").strip()
            agent_runtime: str | None = None
            if mode == ASSISTANT_MODE_AGENT and prompt:
                runtime_match = re.match(r"^(codex|deepseek|ds)\b(?:\s+|[:：]\s*)?(.*)$", prompt, flags=re.IGNORECASE | re.DOTALL)
                if runtime_match:
                    agent_runtime = _normalize_agent_runtime(runtime_match.group(1))
                    prompt = (runtime_match.group(2) or "").strip()
            if mode == ASSISTANT_MODE_AGENT and agent_runtime is None:
                lowered = normalized.lower()
                if lowered.startswith("codex"):
                    agent_runtime = AGENT_RUNTIME_CODEX
            return AssistantModeCommand(mode=mode, prompt=prompt, agent_runtime=agent_runtime)
    return None


def _assistant_mode_label(mode: str) -> str:
    return {
        ASSISTANT_MODE_CHAT: "普通助手",
        ASSISTANT_MODE_AGENT: "Agent",
        ASSISTANT_MODE_STORYBOARD: "分镜助手",
        ASSISTANT_MODE_DEEP_RESEARCH: "Deep Research",
        ASSISTANT_MODE_STORYBOARD_BREAKDOWN: "分镜拆解",
    }.get(mode, "普通助手")


def _assistant_card_title(mode: str) -> str:
    return {
        ASSISTANT_MODE_CHAT: "哔车AI助手",
        ASSISTANT_MODE_AGENT: "哔车AI助手 · Agent",
        ASSISTANT_MODE_STORYBOARD: "哔车AI助手 · 分镜",
        ASSISTANT_MODE_DEEP_RESEARCH: "哔车AI助手 · Deep Research",
        ASSISTANT_MODE_STORYBOARD_BREAKDOWN: "哔车AI助手 · 分镜拆解",
    }.get(mode, "哔车AI助手")


def _assistant_mode_activation_hint(mode: str, *, agent_runtime: str | None = None) -> str:
    runtime = _normalize_agent_runtime(agent_runtime)
    agent_label = "Codex" if runtime == AGENT_RUNTIME_CODEX else "DeepSeek"
    return {
        ASSISTANT_MODE_CHAT: "当前会话已切换为：普通助手。你现在可以直接继续聊天。",
        ASSISTANT_MODE_AGENT: f"当前会话已切换为：Agent（{agent_label}）。后续消息会交给 {agent_label} Agent 独立处理；群聊和私聊各自隔离。请继续发送你的任务。",
        ASSISTANT_MODE_STORYBOARD: "当前会话已切换为：分镜助手。请继续发送分镜需求、项目命令或素材说明。",
        ASSISTANT_MODE_DEEP_RESEARCH: "当前会话已切换为：Deep Research。请继续发送研究主题、文档链接或文件。",
        ASSISTANT_MODE_STORYBOARD_BREAKDOWN: "当前会话已切换为：分镜拆解。请继续发送文档、文件或要拆解的需求文本。",
    }.get(mode, "当前会话已切换为：普通助手。你现在可以直接继续聊天。")


def _default_chatbot_text_model() -> str:
    return {
        "dashscope": settings.dashscope_text_model,
        "openai": settings.openai_text_model,
        "deepseek": settings.deepseek_text_model,
        "openrouter": settings.openrouter_text_model,
    }.get(settings.default_text_provider, settings.deepseek_text_model)


def _default_chatbot_image_model() -> str:
    return {
        "dashscope": settings.dashscope_image_model,
        "openai": settings.openai_image_model,
        "nano_banana_2": IMAGE_MODEL_NANOBANANA,
        "openrouter": IMAGE_MODEL_NANOBANANA,
    }.get(settings.default_image_provider, settings.dashscope_image_model)


def _default_chatbot_video_model() -> str:
    return {
        "dashscope": settings.dashscope_video_model,
        "seedance_2_0": settings.seedance_model_id or settings.dashscope_video_model,
        "xyq_nest": settings.xyq_video_model,
    }.get(settings.default_video_provider, settings.dashscope_video_model)


def _is_slash_command(text: str) -> bool:
    return text.strip().startswith("/")


def _command_text(text: str) -> str:
    return text.strip()[1:].strip() if _is_slash_command(text) else text.strip()


def _normalize_command_phrase(text: str) -> str:
    normalized = re.sub(r"\s+", " ", text.strip()).lower()
    normalized = normalized.rstrip("。.!！?？,，;；:：")
    return normalized


def _is_agent_upload_to_feishu_request(text: str) -> bool:
    normalized = _normalize_command_phrase(text)
    phrases = (
        "存到飞书",
        "保存到飞书",
        "上传到飞书",
        "传到飞书",
        "发到飞书",
        "存到ai生成",
        "保存到ai生成",
        "上传到ai生成",
        "存到ai生成文件夹",
    )
    return any(phrase in normalized for phrase in phrases)


def _extract_local_artifact_candidates(text: str) -> list[str]:
    raw = str(text or "")
    candidates: list[str] = []

    for url in re.findall(r"\((file://[^\s)]+)\)", raw):
        candidates.append(url)
    for url in re.findall(r"(file://\S+)", raw):
        candidates.append(url)
    for path in re.findall(r"(/Users/[^\s)\]>]+)", raw):
        candidates.append(path)
    for path in re.findall(r"(/[^\s)\]>]+\.(?:%s))" % "|".join(LOCAL_ARTIFACT_EXTENSIONS), raw, flags=re.IGNORECASE):
        candidates.append(path)
    for match in re.findall(r"\[([^\]]+\.(?:%s))\]\([^)]+\)" % "|".join(LOCAL_ARTIFACT_EXTENSIONS), raw, flags=re.IGNORECASE):
        candidates.append(match)
    for name in re.findall(r"\b([A-Za-z0-9._-]+\.(?:%s))\b" % "|".join(LOCAL_ARTIFACT_EXTENSIONS), raw, flags=re.IGNORECASE):
        candidates.append(name)

    deduped: list[str] = []
    for item in candidates:
        value = str(item or "").strip()
        if value and value not in deduped:
            deduped.append(value)
    return deduped


def _resolve_local_artifact_path(candidate: str) -> Path | None:
    value = str(candidate or "").strip()
    if not value:
        return None
    if value.startswith("file://"):
        path = Path(value.replace("file://", "", 1))
        return path if path.exists() and path.is_file() else None
    direct = Path(value)
    if direct.is_absolute():
        return direct if direct.exists() and direct.is_file() else None

    roots = [
        Path.cwd(),
        Path("/Users/applemima111/.openclaw/workspace"),
        Path("/Users/applemima111/Desktop/动画/bicaraifilm"),
        Path("/Users/applemima111/bicar_runtime"),
    ]
    matches: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        try:
            found = list(root.rglob(value))
        except OSError:
            continue
        for item in found:
            if item.is_file():
                matches.append(item)
    if not matches:
        return None
    matches.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    return matches[0]


async def _agent_upload_recent_artifact_to_feishu(
    db: Session,
    *,
    text: str,
    chat_id: str | None,
    chat_type: str | None,
    sender_open_id: str | None,
) -> str | None:
    if not _is_agent_upload_to_feishu_request(text):
        return None

    memory = ChatMemoryService(db, chat_id=chat_id, chat_type=chat_type, sender_open_id=sender_open_id)
    recent = memory.recent_messages(rounds=8)
    candidates: list[str] = []
    for item in reversed(recent):
        if item.role != "assistant":
            continue
        for candidate in _extract_local_artifact_candidates(item.content):
            if candidate not in candidates:
                candidates.append(candidate)
        if candidates:
            break

    resolved_path: Path | None = None
    for candidate in candidates:
        resolved_path = _resolve_local_artifact_path(candidate)
        if resolved_path:
            break

    if not resolved_path:
        return (
            "我没有在当前会话最近的 Agent 产物里找到可上传的本地文件。"
            "请把本地文件路径直接发给我，或者先让我生成/给出具体文件路径后再说“存到飞书”。"
        )

    workspace = FeishuWorkspaceService()
    target_folder = _extract_drive_folder_token(text, workspace=workspace)
    upload, resolved_folder = await workspace.upload_file_with_fallback(
        target_folder=target_folder,
        name=resolved_path.name,
        content=resolved_path.read_bytes(),
    )
    file_token = _extract_feishu_file_token(upload)
    file_url = _extract_feishu_file_url(upload)
    if not file_url and file_token:
        file_url = _feishu_file_url(file_token)
    if not file_url:
        raise RuntimeError("飞书上传返回成功，但没有拿到可用的文件链接。")
    location = "指定文件夹" if target_folder else "飞书“AI生成”文件夹"
    return "\n".join(
        [
            f"已存到飞书 `{location}`，并确认上传成功。",
            "",
            f"- 文件：`{resolved_path.name}`",
            f"- 链接：[打开文件]({file_url})",
            f"- 目录 token：`{resolved_folder}`",
        ]
    )


def _session_source_label(*, chat_id: str | None, chat_type: str | None, sender_open_id: str | None) -> str:
    session = resolve_chat_session(chat_id=chat_id, chat_type=chat_type, sender_open_id=sender_open_id)
    return session.session_key


def _recent_conversation_texts(memory: ChatMemoryService, *, rounds: int = 4) -> list[str]:
    texts: list[str] = []
    for item in memory.recent_messages(rounds=rounds):
        content = str(item.content or "").strip()
        if content:
            texts.append(content)
    return texts


def _video_download_reply(
    *,
    workspace,
    result: VideoDownloadResult,
) -> str:
    lines = [
        "已按视频下载工作流处理，并已记录到飞书“视频下载”表格。",
        "",
        f"- 下载表格：{workspace.table_url}",
        f"- 存储文件夹：{workspace.folder_url}",
        f"- 任务状态：{result.status}",
    ]
    if result.file_name:
        lines.append(f"- 文件名：`{result.file_name}`")
    if result.file_url:
        lines.append(f"- 文件位置：{result.file_url}")
    if result.log and result.status != DOWNLOAD_STATUS_DONE:
        lines.extend(["", "**详细日志**", f"```text\n{result.log[-2000:]}\n```"])
    elif result.status == DOWNLOAD_STATUS_RUNNING:
        lines.append("- 已开始下载，后续状态会继续回填到视频下载表格。")
    return "\n".join(lines).strip()


async def _maybe_run_video_download_workflow(
    db: Session,
    *,
    text: str,
    chat_id: str | None,
    chat_type: str | None,
    sender_open_id: str | None,
    assistant_mode: str,
    progress_notifier: Callable[[str], Awaitable[None]] | None = None,
) -> str | None:
    if assistant_mode not in {ASSISTANT_MODE_CHAT, ASSISTANT_MODE_AGENT}:
        return None

    memory = ChatMemoryService(db, chat_id=chat_id, chat_type=chat_type, sender_open_id=sender_open_id)
    service = VideoDownloadService()
    request = service.parse_request_from_conversation(
        text,
        recent_texts=_recent_conversation_texts(memory),
        source_session=_session_source_label(chat_id=chat_id, chat_type=chat_type, sender_open_id=sender_open_id),
    )
    if not request:
        return None

    if progress_notifier:
        await progress_notifier("已识别为视频下载任务，正在写入“视频下载”工作流并开始下载。")

    workspace = await service.ensure_workspace()
    result = await service.create_chat_download_task_in_workspace(workspace=workspace, request=request)
    return _video_download_reply(workspace=workspace, result=result)


def _parse_video_download_command(text: str) -> str | None:
    normalized = text.strip()
    match = re.match(r"^(?:下载视频|视频下载)(?:\s*[:：]\s*|\s+)?(.*)$", normalized, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    return (match.group(1) or "").strip()


def _parse_video_storyboard_command(text: str) -> VideoStoryboardCommand | None:
    normalized = text.strip()
    match = re.match(
        r"^(?:视频拆分镜|视频转分镜|从视频生成分镜表|视频生成分镜表)(?:\s*[:：]\s*|\s+)?(.*)$",
        normalized,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return None
    body = (match.group(1) or "").strip()
    fields = _parse_video_storyboard_fields(body)
    video_reference = (
        fields.get("video")
        or fields.get("link")
        or _first_command_url(body, include_folder=False)
        or ""
    ).strip()
    if not video_reference:
        return VideoStoryboardCommand(video_reference="")

    parent_folder_url = (fields.get("folder") or fields.get("parent_folder") or "").strip()
    if not parent_folder_url:
        parent_folder_url = _first_command_url(body, include_folder=True, folder_only=True) or None
    project_name = (fields.get("project_name") or fields.get("name") or "").strip() or None
    sample_count = _parse_positive_int(fields.get("sample_count"), default=10, minimum=3, maximum=20)
    target_shots = _parse_positive_int(fields.get("target_shots"), default=0, minimum=3, maximum=30) or None
    return VideoStoryboardCommand(
        video_reference=video_reference,
        project_name=project_name,
        parent_folder_url=parent_folder_url,
        sample_count=sample_count,
        target_shots=target_shots,
    )


def _parse_video_storyboard_fields(body: str) -> dict[str, str]:
    aliases = {
        "video": {"视频", "视频链接", "文件", "文件链接", "飞书文件", "video", "file"},
        "link": {"链接", "地址", "url", "link"},
        "project_name": {"项目名", "项目名称", "分镜项目名", "project", "project_name"},
        "name": {"名称", "name"},
        "folder": {"目录", "文件夹", "目标文件夹", "folder", "parent_folder"},
        "sample_count": {"抽帧数", "采样帧数", "sample", "sample_count", "frames"},
        "target_shots": {"镜头数", "分镜数", "行数", "shots", "target_shots"},
    }
    alias_to_field = {
        alias.lower(): field_name
        for field_name, field_aliases in aliases.items()
        for alias in field_aliases
    }
    pattern = re.compile(
        r"(?P<key>%s)\s*[:=：]\s*" % "|".join(re.escape(alias) for alias in sorted(alias_to_field, key=len, reverse=True)),
        flags=re.IGNORECASE,
    )
    matches = list(pattern.finditer(body))
    fields: dict[str, str] = {}
    for index, item in enumerate(matches):
        field_name = alias_to_field[item.group("key").lower()]
        start = item.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        fields[field_name] = body[start:end].strip().strip("；;")
    return fields


def _first_command_url(raw: str, *, include_folder: bool, folder_only: bool = False) -> str | None:
    patterns = [
        r"https?://[^\s，,。；;]+/drive/folder/[A-Za-z0-9]+",
        r"https?://[^\s，,。；;]+/file/[A-Za-z0-9]+",
        r"https?://[^\s，,。；;]+",
    ]
    for pattern in patterns:
        for item in re.finditer(pattern, raw):
            url = item.group(0).strip()
            is_folder = "/drive/folder/" in url
            if folder_only and not is_folder:
                continue
            if is_folder and not include_folder:
                continue
            return url
    return None


def _parse_positive_int(value: str | None, *, default: int, minimum: int, maximum: int) -> int:
    raw = str(value or "").strip()
    if not raw:
        return default
    match = re.search(r"\d+", raw)
    if not match:
        return default
    return max(minimum, min(int(match.group(0)), maximum))


def _parse_chatbot_model_command(text: str) -> str | None:
    normalized = re.sub(r"\s+", " ", text.strip())
    match = re.match(r"^(?:切换chatbot模型|切换聊天模型|切换文本模型|chatbot模型)\s+(\S+)$", normalized, flags=re.IGNORECASE)
    if not match:
        return None
    model = match.group(1)
    return model if model in {
        "qwen-plus",
        "qwen-max",
        "gpt-5.4",
        "deepseek-v4-pro",
        "deepseek-v4-flash",
        "google/gemini-3.1-pro-preview",
        "google/gemini-3.1-flash-lite-preview",
    } else None


def _parse_switch_current_project_command(text: str) -> str | None:
    normalized = re.sub(r"\s+", " ", text.strip())
    match = re.match(r"^(?:切换当前项目|切换项目|绑定当前项目|绑定项目)\s+(.+)$", normalized, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    return (match.group(1) or "").strip()


def _parse_project_command(text: str) -> dict[str, str] | None:
    batch_no = _extract_batch_no(text) or "batch_001"
    command_text = re.sub(r"batch[_-]?\d+", "", text, flags=re.IGNORECASE)
    normalized = re.sub(r"\s+", "", command_text.strip().lower())
    if normalized in {"优化当前批次prompt", "优化当前批次", "优化prompt", "优化提示词", "ai优化提示词"}:
        return {"action": "batch.optimize_prompt", "batch_no": batch_no}
    if normalized in {"生成当前批次帧", "生成当前批次", "生成帧图", "出图", "生成图片", "生成全部图片"}:
        return {"action": "project.generate_all_images", "batch_no": batch_no}
    if normalized in {"生成全部视频", "生成视频"}:
        return {"action": "project.generate_all_videos", "batch_no": batch_no}
    if normalized in {"生成全部图片和视频", "生成图片和视频", "图片和视频"}:
        return {"action": "project.generate_all_media", "batch_no": batch_no}
    if normalized in {"启动首尾帧同步", "首尾帧同步", "同步首尾帧"}:
        return {"action": "project.set_transition_alignment", "batch_no": batch_no}
    if normalized in {"关闭首尾帧同步", "停用首尾帧同步"}:
        return {"action": "project.set_transition_alignment", "batch_no": batch_no, "enabled": "false"}
    if normalized in {"启动关键帧生成", "关键帧生成", "生成关键帧", "开启关键帧生成"}:
        return {"action": "project.set_keyframes", "batch_no": batch_no}
    if normalized in {"关闭关键帧生成", "停用关键帧生成"}:
        return {"action": "project.set_keyframes", "batch_no": batch_no, "enabled": "false"}
    if normalized in {"同步表格", "同步分镜表", "同步"}:
        return {"action": "project.sync", "batch_no": batch_no}
    if normalized in {"查看进度", "查询进度", "进度", "项目进度", "查询项目进度", "表格链接", "查看表格", "项目表格", "项目文件夹", "文件夹链接"}:
        return {"action": "project.progress", "batch_no": batch_no}
    return None


def _parse_natural_project_query_command(text: str) -> dict[str, str] | None:
    batch_no = _extract_batch_no(text) or "batch_001"
    normalized = re.sub(r"\s+", "", text.strip().lower())
    if normalized in {"查看进度", "查询进度", "进度", "项目进度", "查询项目进度", "表格链接", "查看表格", "项目表格", "项目文件夹", "文件夹链接"}:
        return {"action": "project.progress", "batch_no": batch_no}
    return None


def _parse_direct_generation_command(text: str) -> DirectGenerationCommand | None:
    normalized = text.strip()
    kind = None
    body = ""
    for prefix, command_kind in (
        ("直接生成图片", "image"),
        ("直接出图", "image"),
        ("直接生成视频", "video"),
        ("直接出视频", "video"),
    ):
        if normalized.startswith(prefix):
            kind = command_kind
            body = normalized[len(prefix) :].strip()
            break
    if not kind:
        return None

    fields = _parse_direct_command_fields(body)
    model = str(fields.get("model") or "").strip()
    prompt = str(fields.get("prompt") or "").strip()
    if not model or not prompt:
        return DirectGenerationCommand(kind=kind, model=model, prompt=prompt)

    normalized_model = normalize_image_model(model) if kind == "image" else normalize_video_model(model)
    if kind == "image" and normalized_model not in DIRECT_IMAGE_MODELS:
        return DirectGenerationCommand(kind=kind, model=model, prompt=prompt)
    if kind == "video" and normalized_model not in DIRECT_VIDEO_MODELS:
        return DirectGenerationCommand(kind=kind, model=model, prompt=prompt)

    duration = _parse_seconds_field(fields.get("duration_seconds"))
    keyframe_time = _parse_seconds_field(fields.get("keyframe_time_seconds"))

    return DirectGenerationCommand(
        kind=kind,
        model=normalized_model,
        prompt=prompt,
        negative_prompt=str(fields.get("negative_prompt") or "").strip(),
        size=str(fields.get("size") or "").strip() or None,
        duration_seconds=duration,
        keyframe_time_seconds=keyframe_time,
        reference_images=tuple(_split_command_urls(str(fields.get("reference_images") or ""))),
        first_frame=tuple(_split_command_urls(str(fields.get("first_frame") or ""))),
        last_frame=tuple(_split_command_urls(str(fields.get("last_frame") or ""))),
        keyframes=tuple(_split_command_urls(str(fields.get("keyframes") or ""))),
    )


def _parse_direct_command_fields(body: str) -> dict[str, str]:
    alias_to_field = {
        alias.lower(): field_name
        for field_name, aliases in DIRECT_FIELD_ALIASES.items()
        for alias in aliases
    }
    pattern = re.compile(
        r"(?P<key>%s)\s*[:=：]\s*" % "|".join(re.escape(alias) for alias in sorted(alias_to_field, key=len, reverse=True)),
        flags=re.IGNORECASE,
    )
    matches = list(pattern.finditer(body))
    fields: dict[str, str] = {}
    for index, match in enumerate(matches):
        field_name = alias_to_field[match.group("key").lower()]
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        value = body[start:end].strip().strip("；;")
        fields[field_name] = value
    return fields


def _split_command_urls(raw: str) -> list[str]:
    items = []
    for item in re.split(r"[\n,，]+", raw):
        value = item.strip()
        if value:
            items.append(value)
    return items


def _extract_batch_no(text: str) -> str | None:
    match = re.search(r"batch[_-]?\d+", text, flags=re.IGNORECASE)
    if not match:
        return None
    return match.group(0).lower().replace("-", "_")


async def _chatbot_reply(
    db: Session,
    *,
    text: str,
    chat_id: str | None,
    chat_type: str | None,
    sender_open_id: str | None,
    source_message_id: str | None = None,
    progress_notifier: Callable[[str], Awaitable[None]] | None = None,
    request_context: ChatRequestContext | None = None,
) -> str:
    preferences = ChatPreferenceService(db)
    assistant_mode = preferences.get_assistant_mode(
        chat_id=chat_id,
        chat_type=chat_type,
        sender_open_id=sender_open_id,
    )
    normalized_text = text.strip()
    _assert_request_current(request_context)

    if assistant_mode == ASSISTANT_MODE_AGENT:
        memory = ChatMemoryService(db, chat_id=chat_id, chat_type=chat_type, sender_open_id=sender_open_id)
        reply = await _agent_reply(
            db,
            text=normalized_text,
            chat_id=chat_id,
            chat_type=chat_type,
            sender_open_id=sender_open_id,
            source_message_id=source_message_id,
            request_context=request_context,
        )
        _assert_request_current(request_context)
        reply = _normalize_assistant_reply(reply)
        memory.append_turn(user_text=normalized_text, assistant_text=reply)
        db.commit()
        return reply

    preferred_model = preferences.get_chatbot_text_model(
        chat_id=chat_id,
        chat_type=chat_type,
        sender_open_id=sender_open_id,
    )
    model = str(preferred_model or _default_chatbot_text_model())
    memory = ChatMemoryService(db, chat_id=chat_id, chat_type=chat_type, sender_open_id=sender_open_id)
    messages = [{"role": "system", "content": _chatbot_system_prompt(session_type=memory.session_type, active_model=model, assistant_mode=assistant_mode)}]
    messages.extend(memory.as_llm_messages())
    messages.append({"role": "user", "content": normalized_text})

    if assistant_mode == ASSISTANT_MODE_DEEP_RESEARCH:
        reply = await _deep_research_reply(
            query=normalized_text,
            messages=messages,
            active_model=model,
            progress_notifier=progress_notifier,
            request_context=request_context,
        )
        _assert_request_current(request_context)
        memory.append_turn(user_text=normalized_text, assistant_text=reply)
        preferences.set_assistant_mode(
            chat_id=chat_id,
            chat_type=chat_type,
            sender_open_id=sender_open_id,
            mode=ASSISTANT_MODE_CHAT,
        )
        db.commit()
        return reply

    if assistant_mode == ASSISTANT_MODE_STORYBOARD_BREAKDOWN:
        reply = await _storyboard_breakdown_reply(
            query=normalized_text,
            messages=messages,
            active_model=model,
            progress_notifier=progress_notifier,
            request_context=request_context,
        )
        _assert_request_current(request_context)
        memory.append_turn(user_text=normalized_text, assistant_text=reply)
        preferences.set_assistant_mode(
            chat_id=chat_id,
            chat_type=chat_type,
            sender_open_id=sender_open_id,
            mode=ASSISTANT_MODE_CHAT,
        )
        db.commit()
        return reply

    reply = await _generate_chat_response(
        model=model,
        messages=messages,
        text=normalized_text,
        assistant_mode=assistant_mode,
    )
    _assert_request_current(request_context)
    reply = _normalize_assistant_reply(reply)

    memory.append_turn(user_text=normalized_text, assistant_text=reply)
    db.commit()
    return reply


async def _reply_with_timeout_hint(
    db: Session,
    *,
    text: str,
    chat_id: str | None,
    chat_type: str | None,
    sender_open_id: str | None,
    source_message_id: str | None = None,
    progress_notifier: Callable[[str], Awaitable[None]] | None,
    hint_enabled: bool,
    feishu: FeishuClient,
    request_context: ChatRequestContext | None,
) -> str:
    reply_task = asyncio.create_task(
        _chatbot_reply(
            db,
            text=text,
            chat_id=chat_id,
            chat_type=chat_type,
            sender_open_id=sender_open_id,
            source_message_id=source_message_id,
            progress_notifier=progress_notifier,
            request_context=request_context,
        )
    )
    if not hint_enabled or not chat_id:
        return await reply_task
    done, _ = await asyncio.wait({reply_task}, timeout=300)
    if done:
        return await reply_task
    if not _is_request_current(request_context):
        raise StaleChatRequest("stale chat request")
    await feishu.send_text(
        chat_id,
        "普通助手超过 5 分钟仍未回复。你可以稍后重试；如果本次最终失败，我会继续返回具体原因。",
    )
    return await reply_task


async def _generate_chat_response(
    *,
    model: str,
    messages: list[dict[str, str]],
    text: str,
    assistant_mode: str,
) -> str:
    if model.startswith("deepseek-v4") and settings.deepseek_api_key and _chatbot_should_search(model=model, text=text, assistant_mode=assistant_mode):
        messages = await _inject_web_search_context(messages, query=text)

    if model.startswith("qwen") and settings.dashscope_api_key:
        return await _dashscope_chat(model=model, messages=messages)
    if model.startswith("deepseek-v4") and settings.deepseek_api_key:
        return await _deepseek_chat(model=model, messages=messages)
    if _chatbot_uses_openrouter(model) and settings.openrouter_api_key:
        return await _openrouter_chat(
            model=model,
            messages=messages,
            enable_web_search=_chatbot_openrouter_web_search_enabled(model) and _chatbot_should_search(
                model=model,
                text=text,
                assistant_mode=assistant_mode,
            ),
        )
    if model.startswith("gpt") and settings.openai_api_key:
        return await _openai_chat(model=model, messages=messages)
    return "我可以继续帮你梳理问题、给建议，或者你也可以发 `/help` 看可用命令。"


def _chatbot_failure_message(exc: Exception) -> str:
    reason = str(exc).strip() or type(exc).__name__
    if len(reason) > 500:
        reason = reason[:497] + "..."
    return f"本次请求处理失败，请重试。\n原因：`{reason}`"


def _split_reply_for_streaming(content: str) -> list[str]:
    normalized = (content or "").strip()
    if not normalized:
        return ["我暂时没有生成有效回复。"]
    if len(normalized) <= STREAM_REPLY_MAX_CHARS:
        return [normalized]

    paragraphs = [part.strip() for part in re.split(r"\n{2,}", normalized) if part.strip()]
    chunks: list[str] = []
    current = ""

    def flush() -> None:
        nonlocal current
        if current.strip():
            chunks.append(current.strip())
        current = ""

    for paragraph in paragraphs:
        candidate = f"{current}\n\n{paragraph}".strip() if current else paragraph
        if len(candidate) <= STREAM_REPLY_TARGET_CHARS:
            current = candidate
            continue
        if current:
            flush()
        if len(paragraph) <= STREAM_REPLY_MAX_CHARS:
            current = paragraph
            continue
        units = re.split(r"(?<=[。！？!?；;])\s*|\n", paragraph)
        local = ""
        for unit in [item.strip() for item in units if item.strip()]:
            candidate_local = f"{local}\n{unit}".strip() if local else unit
            if len(candidate_local) <= STREAM_REPLY_MAX_CHARS:
                local = candidate_local
                continue
            if local:
                chunks.append(local.strip())
                local = ""
            if len(unit) <= STREAM_REPLY_MAX_CHARS:
                local = unit
                continue
            start = 0
            while start < len(unit):
                chunks.append(unit[start : start + STREAM_REPLY_MAX_CHARS].strip())
                start += STREAM_REPLY_MAX_CHARS
        if local:
            current = local
        else:
            current = ""
    flush()
    return chunks or [normalized]


async def _send_reply_segmented(
    *,
    feishu: FeishuClient,
    target_chat: str,
    content: str,
    title: str,
    chat_id: str | None,
    chat_type: str | None,
    sender_open_id: str | None,
    request_context: ChatRequestContext | None,
) -> int:
    chunks = _split_reply_for_streaming(content)
    sent = 0
    try:
        for index, chunk in enumerate(chunks, start=1):
            _assert_request_current(request_context)
            chunk_title = title if len(chunks) == 1 else f"{title}（{index}/{len(chunks)}）"
            await feishu.send_card(
                target_chat,
                chatbot_reply_card(
                    content=chunk,
                    title=chunk_title,
                    chat_id=chat_id,
                    chat_type=chat_type,
                    sender_open_id=sender_open_id,
                    include_actions=index == len(chunks),
                ),
            )
            sent += 1
        return sent
    except StaleChatRequest:
        raise
    except Exception:
        _assert_request_current(request_context)
        if sent:
            await feishu.send_text(target_chat, "分段发送中断，下面补发完整回复。")
        await feishu.send_card(
            target_chat,
            chatbot_reply_card(
                content=content,
                title=title,
                chat_id=chat_id,
                chat_type=chat_type,
                sender_open_id=sender_open_id,
                include_actions=True,
            ),
        )
        return sent + 1


async def _handle_direct_generation_command(
    db: Session,
    *,
    feishu: FeishuClient,
    target_chat: str | None,
    chat_type: str | None,
    sender_open_id: str | None,
    command: DirectGenerationCommand,
) -> dict[str, Any]:
    if not command.model or not command.prompt:
        if target_chat:
            await feishu.send_card(
                target_chat,
                chatbot_reply_card(
                    content=_direct_command_help(command.kind),
                    chat_id=target_chat,
                    chat_type=chat_type,
                    sender_open_id=sender_open_id,
                ),
            )
        return {"message": "直接生成命令缺少参数", "data": {"kind": command.kind}}

    project = _current_chat_project(
        db,
        chat_id=target_chat,
        chat_type=chat_type,
        sender_open_id=sender_open_id,
    )
    workflow = WorkflowService(db)
    provider_name = workflow._infer_provider(kind=command.kind, provider="auto", model_id=command.model)
    if command.kind == "image" and command.model not in DIRECT_IMAGE_MODELS:
        if target_chat:
            await feishu.send_text(target_chat, f"图片模型暂不支持：{command.model}")
        return {"message": "图片模型不支持", "data": {"model": command.model}}
    if command.kind == "video" and command.model not in DIRECT_VIDEO_MODELS:
        if target_chat:
            await feishu.send_text(target_chat, f"视频模型暂不支持：{command.model}")
        return {"message": "视频模型不支持", "data": {"model": command.model}}
    if command.kind == "image" and command.reference_images and provider_name not in {"nano_banana_2", "openrouter"}:
        if target_chat:
            await feishu.send_text(target_chat, "当前这个图片模型不支持带参考图的直接生成。图生图请优先使用 `nanobanana`。")
        return {"message": "图片模型不支持参考图", "data": {"model": command.model}}

    if target_chat:
        await feishu.send_text(target_chat, f"已收到，正在直接生成{ '图片' if command.kind == 'image' else '视频' }：{command.model}")

    temp_paths: list[Path] = []
    try:
        reference_images = await _prepare_media_sources(feishu, command.reference_images, temp_paths)
        first_frame = await _prepare_media_sources(feishu, command.first_frame, temp_paths)
        last_frame = await _prepare_media_sources(feishu, command.last_frame, temp_paths)
        keyframes = await _prepare_media_sources(feishu, command.keyframes, temp_paths)

        if command.kind == "image":
            result = await ProviderRouter().image(provider_name).generate_image(
                {
                    "model": command.model,
                    "prompt": command.prompt,
                    "negative_prompt": command.negative_prompt,
                    "size": command.size or _default_direct_image_size(project),
                    "reference_images": reference_images,
                    "reference_image_urls": reference_images,
                }
            )
            filename = _direct_filename(prefix="direct_image", mime_type=result.mime_type, fallback_suffix=".png")
            folder_token = _direct_output_folder(project=project, kind="image")
            upload = await _upload_direct_output(feishu, folder_token=folder_token, filename=filename, content=result.bytes_data)
            file_token = _extract_feishu_file_token(upload)
            file_url = _feishu_file_url(file_token)
            content = "\n".join(
                [
                    "**直接生成图片已完成**",
                    f"- 模型：`{command.model}`",
                    f"- Provider：`{provider_name}`",
                    f"- 尺寸：`{command.size or _default_direct_image_size(project)}`",
                    f"- 参考图：{len(reference_images)} 张" if reference_images else "- 参考图：未使用",
                    f"- 结果链接：[打开图片]({file_url})",
                ]
            )
        else:
            result = await _generate_direct_video(
                provider_name=provider_name,
                command=command,
                reference_images=reference_images,
                first_frame=first_frame,
                last_frame=last_frame,
                keyframes=keyframes,
            )
            filename = _direct_filename(prefix="direct_video", mime_type=result["mime_type"], fallback_suffix=".mp4")
            folder_token = _direct_output_folder(project=project, kind="video")
            upload = await _upload_direct_output(feishu, folder_token=folder_token, filename=filename, content=result["video_bytes"])
            file_token = _extract_feishu_file_token(upload)
            file_url = _feishu_file_url(file_token)
            content = "\n".join(
                [
                    "**直接生成视频已完成**",
                    f"- 模型：`{command.model}`",
                    f"- Provider：`{provider_name}`",
                    f"- 时长：`{_format_seconds(command.duration_seconds or _default_direct_duration(project))} 秒`",
                    f"- 首帧：{len(first_frame)} 张" if first_frame else "- 首帧：未使用",
                    f"- 尾帧：{len(last_frame)} 张" if last_frame else "- 尾帧：未使用",
                    f"- 参考图/关键帧：{len(reference_images) + len(keyframes)} 张",
                    f"- 结果链接：[打开视频]({file_url})",
                ]
            )

        if target_chat:
            await feishu.send_card(
                target_chat,
                chatbot_reply_card(content=content, chat_id=target_chat, chat_type=chat_type, sender_open_id=sender_open_id),
            )
        return {"message": "直接生成已完成", "data": {"kind": command.kind, "model": command.model}}
    except Exception as exc:
        if target_chat:
            await feishu.send_card(
                target_chat,
                chatbot_reply_card(
                    content="\n".join(
                        [
                            f"**直接生成{ '图片' if command.kind == 'image' else '视频' }失败**",
                            f"- 模型：`{command.model}`",
                            f"- 原因：`{str(exc)}`",
                        ]
                    ),
                    chat_id=target_chat,
                    chat_type=chat_type,
                    sender_open_id=sender_open_id,
                ),
            )
        return {"message": "直接生成失败", "data": {"kind": command.kind, "error": str(exc)}}
    finally:
        _cleanup_temp_paths(temp_paths)


async def _generate_direct_video(
    *,
    provider_name: str,
    command: DirectGenerationCommand,
    reference_images: list[str],
    first_frame: list[str],
    last_frame: list[str],
    keyframes: list[str],
) -> dict:
    provider = ProviderRouter().video(provider_name)
    payload = {
        "model": command.model,
        "prompt": command.prompt,
        "negative_prompt": command.negative_prompt,
        "duration_seconds": command.duration_seconds or None,
        "keyframe_time_seconds": command.keyframe_time_seconds,
        "first_frame_url": first_frame[0] if first_frame else None,
        "last_frame_url": last_frame[0] if last_frame else None,
        "reference_image_url": reference_images[0] if reference_images else None,
        "keyframe_urls": keyframes,
    }
    task = await provider.create_video_task(payload)
    return await provider.poll_video_task(task.provider_task_id)


async def _prepare_media_sources(feishu: FeishuClient, sources: tuple[str, ...], temp_paths: list[Path]) -> list[str]:
    prepared: list[str] = []
    for source in sources:
        value = str(source).strip()
        if not value:
            continue
        if value.startswith("file://"):
            prepared.append(value)
            continue
        parsed = urlparse(value)
        if parsed.scheme in {"http", "https"} and "feishu.cn" in parsed.netloc:
            path = parsed.path or ""
            if "/drive/folder/" in path:
                raise RuntimeError("暂不支持直接使用飞书文件夹链接，请改成具体文件链接。")
            token = _extract_feishu_file_token_from_url(value)
            if token:
                filename, content, _mime_type = await feishu.download_drive_file(token)
                suffix = Path(filename).suffix or ".bin"
                temp_file = Path(tempfile.mkstemp(prefix="biche-bot-media-", suffix=suffix)[1])
                temp_file.write_bytes(content)
                temp_paths.append(temp_file)
                prepared.append(f"file://{temp_file}")
                continue
        prepared.append(value)
    return prepared


def _extract_feishu_file_token_from_url(url: str) -> str | None:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if "file" in parts:
        index = parts.index("file")
        if len(parts) > index + 1:
            return parts[index + 1]
    return None


def _cleanup_temp_paths(paths: list[Path]) -> None:
    for path in paths:
        try:
            if path.exists():
                path.unlink()
        except OSError:
            continue


def _direct_output_folder(*, project: Project | None, kind: str) -> str:
    folders = (project.workflow_config or {}).get("folders", {}) if project else {}
    if kind == "image":
        return str(folders.get("frames") or settings.feishu_root_folder_token)
    return str(folders.get("videos") or settings.feishu_root_folder_token)


async def _upload_direct_output(feishu: FeishuClient, *, folder_token: str, filename: str, content: bytes) -> dict:
    try:
        return await feishu.upload_file(folder_token, filename, content)
    except FeishuApiError as exc:
        if not _is_missing_parent_folder_error(exc):
            raise
        workspace = FeishuWorkspaceService(feishu=feishu)
        ensured = await workspace.ensure_default_workspace_folder()
        fallback_token = str(ensured.get("folder_token") or settings.feishu_root_folder_token or "root")
        if not fallback_token or fallback_token == folder_token:
            raise
        return await feishu.upload_file(fallback_token, filename, content)


def _is_missing_parent_folder_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return "parent node not exist" in message or ("parent node" in message and "not exist" in message)


def _default_direct_image_size(project: Project | None) -> str:
    aspect_ratio = ((project.workflow_config or {}).get("aspect_ratio") if project else None) or "16:9"
    return {"16:9": "1280*720", "9:16": "720*1280", "1:1": "1024*1024"}.get(aspect_ratio, settings.dashscope_image_size)


def _parse_seconds_field(value) -> float | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    match = re.search(r"\d+(?:\.\d+)?", raw)
    if not match:
        return None
    parsed = float(match.group(0))
    return parsed if parsed > 0 else None


def _format_seconds(value) -> str:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return str(value)
    if seconds.is_integer():
        return str(int(seconds))
    return f"{seconds:.2f}".rstrip("0").rstrip(".")


def _default_direct_duration(project: Project | None) -> float:
    return float(((project.workflow_config or {}).get("duration_seconds") if project else None) or 5)


def _direct_filename(*, prefix: str, mime_type: str, fallback_suffix: str) -> str:
    suffix = mimetypes.guess_extension(mime_type or "") or fallback_suffix
    safe_suffix = suffix if suffix.startswith(".") else fallback_suffix
    return f"{prefix}_{next(tempfile._get_candidate_names())}{safe_suffix}"


def _extract_feishu_file_token(response: dict) -> str:
    data = response.get("data", {})
    return str(data.get("file_token") or data.get("file", {}).get("file_token") or "")


def _extract_feishu_file_url(response: dict) -> str:
    data = response.get("data", {})
    file_data = data.get("file") or {}
    return str(data.get("url") or file_data.get("url") or "")


def _feishu_file_url(file_token: str) -> str:
    domain = settings.feishu_base_url.replace("https://open.", "").replace("http://open.", "")
    return f"https://{domain}/file/{file_token}"


def _direct_command_help(kind: str) -> str:
    if kind == "image":
        return (
            "**直接生成图片命令格式**\n"
            "- `/直接生成图片`\n"
            "- `模型=nanobanana`\n"
            "- `提示词=夕阳下的海边咖啡馆`\n"
            "- 可选：`尺寸=1280*720`\n"
            "- 图生图可选：`参考图=https://xxx.feishu.cn/file/FILE_TOKEN`\n"
            "- 图生图建议优先使用：`nanobanana`"
        )
    return (
        "**直接生成视频命令格式**\n"
        "- `/直接生成视频`\n"
        f"- `模型={VIDEO_MODEL_XYQ}`\n"
        "- `提示词=镜头缓慢推近，蒸汽自然上升`\n"
        "- 可选：`时长=5` 或 `时长=3.5`\n"
        "- 图生视频可选：`首帧=https://xxx.feishu.cn/file/A`、`尾帧=https://xxx.feishu.cn/file/B`\n"
        "- 也可选：`参考图=`、`关键帧=`、`关键帧时间点=2.5`"
    )


def _chatbot_uses_openrouter(model: str) -> bool:
    normalized = (model or "").strip().lower()
    return normalized.startswith(
        (
            "google/",
            "openai/",
            "anthropic/",
            "x-ai/",
            "meta-llama/",
            "mistralai/",
            "moonshotai/",
        )
    )


def _chatbot_openrouter_web_search_enabled(model: str) -> bool:
    return _chatbot_uses_openrouter(model)


def _chatbot_should_search(*, model: str, text: str, assistant_mode: str) -> bool:
    if assistant_mode == ASSISTANT_MODE_DEEP_RESEARCH:
        return True
    if assistant_mode == ASSISTANT_MODE_AGENT:
        return False
    if assistant_mode == ASSISTANT_MODE_STORYBOARD_BREAKDOWN:
        return False
    normalized = (text or "").strip().lower()
    keywords = (
        "最新",
        "最近",
        "今天",
        "昨天",
        "今年",
        "新闻",
        "官网",
        "公开资料",
        "行业动态",
        "市场",
        "融资",
        "公司",
        "品牌",
        "竞品",
        "政策",
        "research",
        "research plan",
        "deep research",
    )
    return any(keyword in normalized for keyword in keywords)


def _chatbot_system_prompt(*, session_type: str, active_model: str, assistant_mode: str) -> str:
    session_summary = "当前会话是私聊，上下文只属于当前私聊用户。" if session_type == "private" else "当前会话是群聊，上下文属于当前群聊。"
    capability_summary = _chatbot_capability_summary()
    web_search_summary = (
        "当前聊天模型通过 OpenRouter 接入了联网搜索。只要用户询问最新信息、公司公开资料、新闻、市场动态、竞品进展、政策变化、时间敏感事实或明确要求你查资料，"
        "你就应该优先使用 web_search 获取公开网页信息，再基于搜索结果作答，并尽量给出来源链接。"
        if _chatbot_openrouter_web_search_enabled(active_model)
        else (
            "当前聊天模型没有原生联网工具。"
            "如果用户需要公开网页资料，而当前模型是 DeepSeek 路径，系统会先做外部网页搜索再把结果提供给你；"
            "除此之外，要明确说明你无法自行联网，只能基于已有上下文给建议。"
        )
    )
    if assistant_mode == ASSISTANT_MODE_AGENT:
        return (
            "你现在处于 Agent 模式。这个模式下的实际执行由外部 Codex Agent 负责，"
            "当前普通聊天提示词不应接管这一轮回复。"
            f"\n{session_summary}"
        )
    if assistant_mode == ASSISTANT_MODE_DEEP_RESEARCH:
        return (
            "你现在处于 Deep Research 助手模式。"
            "你的职责是围绕用户给定主题执行深度研究、整理公开资料、读取用户提供的飞书文档或文件、输出结构化中文研究报告或研究计划。"
            "不要把当前对话当成飞书分镜项目的一部分，也不要主动提及项目、镜号、图片/视频工作流，除非用户明确要求把研究结果转成分镜。"
            f"{web_search_summary}\n"
            "回答要求：中文优先，尽量结构化；事实不确定时明确标注“待验证”；最终重点是完整研究内容，而不是只返回来源列表。"
            f"\n{session_summary}"
        )
    if assistant_mode == ASSISTANT_MODE_STORYBOARD_BREAKDOWN:
        return (
            "你现在处于分镜拆解模式。"
            "你的职责是读取用户提供的飞书云文档、飞书文件或正文材料，把内容拆解成可执行的分镜需求。"
            "你要输出结构化中文 Markdown，重点不是生成最终视频，而是把原始需求转成分镜规划。"
            "不要把当前对话绑定到现有飞书分镜项目，也不要假装已经创建项目、生成图片或生成视频。"
            "除非系统明确告诉你某个文档已经存在历史拆解记录，否则不要擅自声称“这个文档之前已经拆解过”。"
            "如果用户发来了文档链接或文件，而你判断应该进入拆解流程，就直接继续拆解，或者明确告诉用户发送 `/分镜拆解 文档链接` 或 `/重新拆解 文档链接`；"
            "不要反问一个系统里没有对应按钮或命令的问题。"
            "如果用户后续要把拆解结果真正落到分镜工作流，可以提醒他切到 `/分镜助手` 或新建项目。"
            "输出至少包含："
            "1. 需求摘要；"
            "2. 推荐片长与节奏；"
            "3. 场景/段落拆分；"
            "4. 分镜清单（镜号、画面内容、景别、机位/运镜、时长、旁白/字幕、音效/音乐、备注）；"
            "5. 视觉风格建议；"
            "6. 需要补充确认的信息。"
            "如果原文信息不足，不要编造，明确标注“待补充”。"
            "如果用户给的是脚本、策划案、采访稿、PPT 提纲、文档或 docx，请优先按文档内容拆解。"
            f"\n{session_summary}"
        )
    if assistant_mode == ASSISTANT_MODE_STORYBOARD:
        return (
            "你是哔车AI助手的分镜模式，服务于“飞书 AI 分镜 -> 图片/视频生成”工作流。\n"
            "你的职责：解释项目规则、字段含义、状态流转、报错原因；回答工作流问题；帮助用户优化分镜描述、Prompt、镜头运动和模型选择；给出下一步建议。\n"
            "你的边界：你不能直接代替用户执行项目操作，不能假装已经新建项目、优化 Prompt、生成图片、生成视频、同步表格、切换模型或修改飞书数据。"
            "当用户希望执行这些操作时，你必须明确告诉他使用对应的斜杠命令，而不是声称你已经做了。\n"
            "可执行命令只有这些："
            "/新建分镜项目：项目名；/优化当前批次 Prompt；/生成全部图片；/生成全部视频；/生成全部图片和视频；"
            "/启动首尾帧同步；/关闭首尾帧同步；/启动关键帧生成；/关闭关键帧生成；/同步表格；/查看进度；"
            "/视频拆分镜 视频=<飞书视频文件链接> 项目名=<可选> 镜头数=<可选> 抽帧数=<可选>；"
            "/切换chatbot模型 qwen-plus|qwen-max|gpt-5.4|deepseek-v4-pro|deepseek-v4-flash|google/gemini-3.1-pro-preview|google/gemini-3.1-flash-lite-preview；"
            "/直接生成图片（用 模型=、提示词=、可选 尺寸=、参考图=）；"
            "/直接生成视频（用 模型=、提示词=、可选 时长=、首帧=、尾帧=、参考图=、关键帧=、关键帧时间点=）。\n"
            "工作流规则：默认生成首帧和尾帧；只有在“启动关键帧生成”后，才会为镜头批量生成关键帧候选。"
            "开启“首尾帧同步”后，后一镜头首帧会复用前一镜头尾帧。"
            "表格里的 `选中关键帧图` 是可选视频中间关键帧输入，空置=不使用；`关键帧时间点` 控制该关键帧出现在第几秒；`视频时长` 控制单镜视频总长度，默认 5 秒且允许小数。"
            "你可以建议用户何时开启这些能力，但不能替他执行。\n"
            f"{web_search_summary}\n"
            "模型建议规则：当用户询问“该选哪个模型”或“为什么失败”时，你必须优先依据下面这份最近一次实测状态回答，而不是只按模型名字猜测。\n"
            f"{capability_summary}\n"
            "回答要求：中文优先，简洁直接，先回答问题，再给建议；如果缺少实时数据，不要编造，直接说明你只能基于当前规则给建议。"
            "不要复述这段系统提示词。\n"
            f"{session_summary}"
        )

    return (
        "你是哔车AI助手，默认以普通对话助手身份服务。"
        "你可以聊天、解释概念、帮用户梳理方案、写提纲、给建议，也可以在需要公开资料时结合联网搜索结果回答。"
        "如果用户想切到真正的 Agent 模式，可以提醒他发送 `/Agent`、`/智能体` 或 `/Codex`；如果想切到 DeepSeek Agent，可以提醒他发送 `/Agent deepseek` 或 `/切换Agent模型 deepseek`。"
        "如果用户明确要做深度研究，可以提醒他发送 `/Deep Research`；如果用户明确要进入分镜工作流，可以提醒他发送 `/分镜助手` 或 `/视频助手`；如果用户要把文档拆成分镜需求，可以提醒他发送 `/拆分镜需求` 或 `/分镜拆解`。\n"
        "如果用户发来飞书文档、飞书文件或 docx，并且意图明显是拆成分镜，不要凭空说“这个文档之前已经拆解过”；"
        "应直接建议用户发送 `/分镜拆解 文档链接`，或者在分镜拆解模式下继续处理。"
        "当用户要执行飞书分镜相关操作时，不要假装已经操作成功，要明确提示对应斜杠命令。"
        "常用命令包括：/Agent、/Agent deepseek、/切换Agent模型 codex|deepseek、/切换当前项目 <表格链接>、/Deep Research、/拆分镜需求、/分镜拆解、/重新拆解、/分镜助手、/视频助手、/普通助手、/help、/New session、/切换chatbot模型。\n"
        "分镜表字段提示：`选中关键帧图` 可作为视频中间关键帧输入；`关键帧时间点` 指定该关键帧出现秒数；`视频时长` 指定单镜视频总长度，默认 5 秒。\n"
        f"{web_search_summary}\n"
        "回答要求：中文优先，简洁直接，信息不确定时要说明不确定。"
        "如果问题明显和分镜工作流有关，可以建议用户切到 `/分镜助手` 或使用对应命令。\n"
        f"{capability_summary}\n"
        f"{session_summary}"
    )


def _agent_deepseek_system_prompt(*, session_type: str) -> str:
    session_summary = "当前会话是私聊，上下文只属于当前私聊用户。" if session_type == "private" else "当前会话是群聊，上下文只属于当前群聊。"
    return (
        "你现在是哔车AI助手的 Agent（DeepSeek）模式。"
        "你的目标是像一个可靠的执行型助手一样思考和回答：先理解用户意图，再给出清晰结论、步骤、风险和下一步建议。"
        "你没有 Codex 的本地工具和代码执行能力，不要假装已经查了本地代码、跑了命令、改了文件或操作了飞书。"
        "如果用户明确要使用当前项目已有功能，要优先指向现有命令和体系，而不是自己模拟："
        "分镜工作流用 `/分镜助手` 或 `/视频助手`；深度研究用 `/Deep Research`；分镜拆解用 `/分镜拆解`；普通聊天用 `/普通助手`。"
        "分镜表字段里，`选中关键帧图` 是可选视频中间关键帧输入，`关键帧时间点` 是它应出现在第几秒，`视频时长` 是单镜视频总长度，默认 5 秒并允许小数。"
        "如果用户是在排查这些功能的逻辑、报错或实现细节，你可以解释已知规则，但要明确说明你当前不能直接读取本地代码。"
        "当用户要求修改已有文案、整理现有文档或润色已经写好的稿件时，默认采用修订模式：保留原文并加删除线，在后面追加替换文本，并用黄色高亮标出新增内容。"
        "如果文档里已经存在删除线、高亮、批注或其他手动修订痕迹，不要继续直接改写；先在聊天里输出一个简短修改计划，等用户确认后再统一执行。"
        "除非用户明确要求全文重写，否则不要把整份文案删掉重写。"
        "回答要求：中文优先，直接、结构化、别装作已经执行成功。"
        f"\n{session_summary}"
    )


def _openclaw_agent_session_id(
    *,
    chat_id: str | None,
    chat_type: str | None,
    sender_open_id: str | None,
    nonce: int,
) -> str:
    session = resolve_chat_session(chat_id=chat_id, chat_type=chat_type, sender_open_id=sender_open_id)
    digest = hashlib.sha1(f"{session.session_key}:{nonce}".encode("utf-8")).hexdigest()[:24]
    return f"feishu-agent-{digest}"


def _openclaw_agent_session_file(session_id: str) -> Path:
    return Path.home() / ".openclaw" / "agents" / "main" / "sessions" / f"{session_id}.jsonl"


def _agent_project_context(
    db: Session,
    *,
    chat_id: str | None,
    chat_type: str | None,
    sender_open_id: str | None,
) -> str:
    if not chat_id and not sender_open_id:
        return ""
    project = _current_chat_project(db, chat_id=chat_id, chat_type=chat_type, sender_open_id=sender_open_id)
    if not project:
        return ""
    storyboard = FeishuStoryboardService(db)
    table_url = storyboard._project_table_url(project)
    folder_url = storyboard._project_folder_url(project)
    stats = storyboard.progress_stats(project)
    lines = [
        "当前聊天绑定的项目上下文（这是默认候选项目，不是唯一真相；用于回答进度、表格链接、文件夹链接等问题；引用时优先使用这些真实链接，不要省略；给用户回链时直接输出完整 URL，不要只用 Markdown 链接文本）：",
        f"- 项目名：{project.name}",
        f"- 表格链接：{table_url or '未配置'}",
        f"- 项目文件夹链接：{folder_url or '未配置'}",
        f"- 镜头总数：{stats.get('total', 0)}",
        f"- Prompt 已优化：{stats.get('prompt_optimized', 0)} / {stats.get('prompt_pending', 0)} 待优化",
        f"- 图片生成：{stats.get('image_done', 0)} 完成 / {stats.get('image_generating', 0)} 进行中",
        f"- 视频生成：{stats.get('video_done', 0)} 完成 / {stats.get('video_generating', 0)} 进行中",
        f"- 待审核：{stats.get('pending_review', 0)}",
        f"- 待验收：{stats.get('pending_acceptance', 0)}",
        f"- 错误数：{stats.get('errors', 0)}",
        "- 分镜表新字段说明：`选中关键帧图` 是可选视频中间关键帧输入，空置表示不使用；`关键帧时间点` 是该关键帧应出现在视频第几秒，允许小数；`视频时长` 控制单镜视频总长度，默认 5 秒，允许小数。",
        "- 若用户要求调整关键帧/时长，应更新对应表格字段后再启动生成；若用户手动上传了 `选中关键帧图`，也要把它当作视频生成输入。",
        "- 默认视频文件夹 `03_视频` 会保留每个镜头当前最新视频，旧版本/未选中视频会移动到 `03_视频/ARCHIVED`，不要把 ARCHIVED 当作失败或删除。",
        "- 重要规则：如果用户当前消息、上一条材料、飞书链接、表格标题或描述明显指向另一个分镜表/项目，不要机械使用这里这个项目。应优先根据用户当前上下文确认真正要处理的表格；不确定时先明确追问或先复述你识别到的候选表格。",
    ]
    return "\n".join(lines)


def _current_chat_project(
    db: Session,
    *,
    chat_id: str | None,
    chat_type: str | None = None,
    sender_open_id: str | None = None,
    preferences: ChatPreferenceService | None = None,
) -> Project | None:
    project_service = ProjectService(db)
    prefs = preferences or ChatPreferenceService(db)
    active_project_id = prefs.get_active_project_id(
        chat_id=chat_id,
        chat_type=chat_type,
        sender_open_id=sender_open_id,
    )
    if active_project_id:
        try:
            project = project_service.get_project(UUID(active_project_id))
        except Exception:
            project = None
        if project and (not chat_id or (project.workflow_config or {}).get("chat_id") == chat_id):
            return project
    return project_service.latest_for_chat(chat_id)


def _maybe_bind_project_from_message(
    db: Session,
    *,
    text: str,
    chat_id: str | None,
    chat_type: str | None,
    sender_open_id: str | None,
    preferences: ChatPreferenceService | None = None,
) -> Project | None:
    if not text or not chat_id:
        return None
    project = _find_project_from_message_links(db, text)
    if not project:
        return None
    prefs = preferences or ChatPreferenceService(db)
    prefs.set_active_project_id(
        chat_id=chat_id,
        chat_type=chat_type,
        sender_open_id=sender_open_id,
        project_id=str(project.id),
    )
    db.commit()
    return project


def _find_project_from_message_links(db: Session, text: str) -> Project | None:
    project_service = ProjectService(db)
    for url in _extract_message_urls(text):
        parsed = urlparse(url)
        if "/base/" not in (parsed.path or ""):
            continue
        parts = [part for part in (parsed.path or "").split("/") if part]
        if "base" not in parts:
            continue
        index = parts.index("base")
        if len(parts) <= index + 1:
            continue
        app_token = parts[index + 1]
        table_id = parse_qs(parsed.query).get("table", [None])[0]
        project = project_service.find_by_feishu_table(app_token, table_id)
        if project:
            return project
    return None


def _agent_document_revision_policy() -> str:
    return (
        "文档/文案修订规则（强约束）：\n"
        "- 当用户要求修改已有文案、整理已有稿件、润色现有文档时，默认进入“修订模式”，不要直接整段删除再重写。\n"
        "- 对需要替换的原文：保留原文并加删除线；紧跟在后面插入替换文本，并用黄色高亮标出新增内容。\n"
        "- 对未改动的内容：保持原有结构、段落顺序、编号和格式，不要为了省事整体重写。\n"
        "- 只有当用户明确要求“整段重写”“全文重写”“重做一版”时，才允许整体重写。\n"
        "- 如果目标文档已经存在明显修订痕迹（删除线、高亮、批注、修订版标记，或用户手动改过），先不要继续批量修改。先在聊天里给出简短修改计划，说明准备改哪些段、保留哪些已有修订，等待用户确认后再统一执行。\n"
        "- 如果当前工具或接口不能可靠表达“删除线 + 黄色高亮”，先明确说明限制并给出修改计划，不要退化成直接删除原文再重写。"
    )


def _agent_video_download_workflow_policy() -> str:
    return (
        "视频下载工作流（Agent 强约束）：\n"
        "- 当用户要求下载视频、保存 YouTube/外部视频、下载文档批注/评论里的视频链接，必须走当前项目的正式视频下载工作流，不要自己直接运行 `yt-dlp`、`videodl`、`curl` 或写临时 shell 脚本把文件只存到本地。\n"
        "- 正式工作流代码入口：`/Users/applemima111/Desktop/动画/bicaraifilm/bicar_ai_storyboard/backend/app/services/video_downloads.py`，核心类是 `VideoDownloadService`。它会保存到飞书 `AI生成/视频下载`，并登记到飞书“视频下载”多维表格。\n"
        "- 文件夹规则：如果视频来自某个飞书文档的正文/批注/评论，必须在 `AI生成/视频下载` 下为该文档创建或复用一个以“飞书文档原名”命名的子文件夹，禁止命名成 `文档_<token>` 这类机器名；所有来自同一文档的视频放在同一个文档原名子文件夹。如果不是文档来源，就放进 `AI生成/视频下载/非文档视频下载`。下载表格会用 `目标文件夹` 字段记录实际文件夹。\n"
        "- 如果是单个或少量视频：在 `backend` 目录用 `.venv311/bin/python3` 调用 `VideoDownloadService.ensure_workspace()` 和 `create_chat_download_task_in_workspace(...)`，让服务创建表格记录、下载、上传、回填状态与文件位置。\n"
        "- 下载前必须去重：先按 YouTube video_id 或规范化 URL 检查“视频下载”表格是否已有同一链接；已有 `已下载` 记录时直接返回现有文件位置，已有 `正在下载/未开始/启动/下载失败` 记录时复用原记录处理，不要再新建重复行、重复上传同名文件。\n"
        "- 文件命名规则：不要把文件命名成 `主题_YouTubeID.mp4` 这种弱名字。优先级为：用户明确指定文件名 > 同一条飞书批注/评论里和 YouTube 链接同时出现的文字 > YouTube 原标题 > 飞书批注/评论旁边的原文语境。只要某个批注/回复里同时有文字和 YouTube 链接，就把该文字当作视频名传给下载工作流，不要再覆盖成 YouTube 原标题。只有没有人工文字时，才使用 YouTube 原标题，并在标题后追加 `（批注内容：xxxxx）`；若仍然重名，由正式服务追加 video_id 短后缀，确保云盘里没有重名文件。\n"
        "- 如果是批量/长列表视频：不要在一个 Agent 回合里同步下载全部视频。应先读取飞书文档正文和批注/评论里的真实视频链接，对链接按 YouTube video_id 或规范化 URL 去重，再把每条唯一链接登记到“视频下载”表格，状态设为 `启动` 或 `未开始`，写入 `comments`、`来源会话`、`创建时间`。如果来自飞书文档，`comments` 必须使用统一结构：`来源文档：<文档原名> <文档链接>`、`批注 ID：<id>`、`批注位置：<quote>`、`批注说明：<同条链接旁人工文字>`、`原始链接：<url>`；不得只写 `文档批注引用`、`克尔维特文档评论` 这类不可追溯备注。`文件名` 字段只有在用户明确指定文件名、或源飞书批注里同条链接旁边有人工文字说明时才填写，不允许填 `克尔维特_VIDEO_ID` 这类机器名。登记后让表格工作流逐条处理；回复用户表格链接、排队数量、已识别链接数和去重后的唯一链接数。\n"
        "- 下载质量与重试由正式服务负责：YouTube 默认走最高画质 `yt-dlp` 路径，失败会自动重试 3 次并在 `log` 字段保留完整失败原因；不要绕过服务自行下载低清版本。\n"
        "- 任何对话发起的视频下载，都必须能在“视频下载”表格里查到记录；最终回复必须给出下载表格链接或文件位置，不能只给本地路径。\n"
        "- 读取飞书文档批注/划线评论/评论内容时，不要只读正文。先从 `/docx/<doc_token>` 提取 `doc_token`，`preview_comment_id` 只是定位评论的参数，不是文档 token。\n"
        "- 飞书评论读取方法：使用项目的 `FeishuClient` 获取 tenant token 后调用 `GET /open-apis/drive/v1/files/{doc_token}/comments?file_type=docx&page_size=100`，如果 `has_more` 为真，继续带 `page_token=next_page_token` 翻页。\n"
        "- 如果需要写回批注，请优先使用项目内 `FeishuClient.list_file_comments(...)` 和 `FeishuClient.add_file_comment_reply(...)`，不要临时猜飞书评论接口结构。\n"
        "- 评论链接提取范围：遍历每个 comment 及 `reply_list.replies` 的 `content.elements`，同时读取 `docs_link.url` 和 `text_run.text` 里的明文 URL；如果同一个 comment/reply 中既有 YouTube 链接又有文字说明，必须把这段文字作为该链接的 `filename_hint` 或 `文件名`，并同时保留到 `comments`。\n"
        "- 判断是否需要补视频名批注时，要逐个 YouTube 链接检查它在同一个 comment/reply 元素序列里的左右文字；不是只看整条批注是否有文字。只要某个 YouTube 链接左右都没有人工说明文字，就必须解析 YouTube 标题后，在源批注线程回复 `YouTube视频名：xxxxx`；多个无名链接就逐行写 `YouTube视频名：<序号>. xxxxx`。不要改正文，也不要只把标题写到下载表格里。\n"
        "- 如果评论 API 权限不足、接口报错或确实没有链接，必须把具体接口/错误原因告诉用户，不要编造“已下载”或退回本地下载脚本。"
    )


def _agent_video_storyboard_workflow_policy() -> str:
    return (
        "视频拆分镜工作流（Agent 工具说明）：\n"
        "- 当用户明确要求“把视频拆成分镜/根据视频生成分镜表/参考视频新建分镜项目/从视频抽镜头脚本”时，不要只给空模板，也不要只凭文件名臆测内容；应调用正式的“下载视频 → 抽帧 → 视觉模型 → 生成分镜表”工作流。\n"
        "- 正式入口是显式命令：`/视频拆分镜 视频=<飞书文件链接或可下载视频链接> 项目名=<项目名> 镜头数=<可选> 抽帧数=<可选> 目录=<可选飞书文件夹>`；同义命令包括 `/视频转分镜`、`/从视频生成分镜表`。\n"
        "- 后端代码入口：`/Users/applemima111/Desktop/动画/bicaraifilm/bicar_ai_storyboard/backend/app/services/video_storyboard.py`，核心类是 `VideoStoryboardService`。它会下载视频、用 ffmpeg 抽帧、调用视觉模型分析画面、创建飞书分镜项目并写入分镜表。\n"
        "- 这是工具选择，不是关键词硬拦截：只有当用户任务语义确实是“从视频生成/更新分镜表”时才用；如果用户只是问视频能不能用、让你解释流程、或只是聊天，不要擅自启动。\n"
        "- 工作流默认只创建和填写分镜表，不自动启动图片或视频生成；完成后提醒用户先检查表格，再用 `/生成全部图片` 或表格状态启动后续生成。\n"
        "- 如果用户附的是飞书视频文件卡片，消息文本里通常会有 `https://.../file/<token>`；把这个链接作为 `视频=` 传入。若拿不到链接，要明确请求用户补发文件链接，而不是输出泛化模板。\n"
        "- 如果用户要求在指定文件夹里创建项目，把飞书文件夹链接作为 `目录=` 传入；否则使用默认 `AI生成/分镜项目` 工作区。"
    )


def _agent_storyboard_table_policy() -> str:
    return (
        "分镜表字段与生成规则（Agent 强约束）：\n"
        "- 分镜表里 `首帧图`、`尾帧图`、`选中关键帧图` 都是视频生成输入；`选中关键帧图` 为空表示不使用中间关键帧，用户手动上传附件也应被当作有效输入。\n"
        "- `关键帧图` 是系统生成的候选关键帧；`选中关键帧图` 是真正送入视频模型的关键帧。不要把两列混用。\n"
        "- `关键帧时间点` 是 `选中关键帧图` 应出现在视频第几秒，允许小数；如果用户要求某张关键帧出现在 2.5 秒处，就更新这列为 `2.5`。\n"
        "- `视频时长` 是单镜视频总长度，默认 `5` 秒，允许小数；如果用户要求 3.5 秒、6 秒等，应更新这列再启动视频生成。\n"
        "- 小云雀生成时会把首尾帧、关键帧、关键帧时间点和视频时长共同作为约束；如果结果不符合，需要调整表格字段后重新启动生成，不要只口头说明。\n"
        "- 默认 `03_视频` 文件夹会只保留每个镜头当前最新视频；旧版本或未选中视频会移动到 `03_视频/ARCHIVED`。这是归档整理，不是删除。"
    )


def _compose_openclaw_agent_message(
    *,
    session_id: str,
    memory: ChatMemoryService,
    text: str,
    project_context: str = "",
) -> str:
    sections: list[str] = []
    sections.append(_agent_document_revision_policy())
    sections.append(_agent_video_download_workflow_policy())
    sections.append(_agent_video_storyboard_workflow_policy())
    sections.append(_agent_storyboard_table_policy())
    if project_context:
        sections.append(project_context)
    if _openclaw_agent_session_file(session_id).exists():
        sections.append(text)
        return "\n\n".join(part for part in sections if part).strip()
    recent = memory.recent_messages(rounds=4)
    if recent:
        lines = ["会话延续上下文（仅供续接当前任务，不要逐字复述）："]
        for item in recent:
            role = "用户" if item.role == "user" else "助手"
            content = (item.content or "").strip()
            if not content:
                continue
            if len(content) > 500:
                content = content[:497] + "..."
            lines.append(f"{role}：{content}")
        lines.append("")
        lines.append(f"当前新请求：{text}")
        sections.append("\n".join(lines).strip())
        return "\n\n".join(part for part in sections if part).strip()
    sections.append(text)
    return "\n\n".join(part for part in sections if part).strip()


async def _run_openclaw_agent(
    *,
    session_id: str,
    message: str,
    timeout_seconds: int = 600,
    session_key: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    command = _resolve_openclaw_command()
    args = [
        *command,
        "agent",
        "--session-id",
        session_id,
        "--message",
        message,
        "--json",
        "--timeout",
        str(timeout_seconds),
    ]
    if model:
        args.extend(["--model", model])
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    if session_key:
        _ACTIVE_OPENCLAW_PROCESSES[session_key] = process
    try:
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            reason = (stderr.decode("utf-8", errors="ignore").strip() or stdout.decode("utf-8", errors="ignore").strip() or "unknown error")
            raise RuntimeError(f"OpenClaw Agent 执行失败：{reason}")
        payload = json.loads(stdout.decode("utf-8", errors="ignore") or "{}")
        if not isinstance(payload, dict):
            raise RuntimeError("OpenClaw Agent 返回了无法识别的结果。")
        return payload
    finally:
        if session_key and _ACTIVE_OPENCLAW_PROCESSES.get(session_key) is process:
            _ACTIVE_OPENCLAW_PROCESSES.pop(session_key, None)


def _extract_openclaw_agent_reply(payload: dict[str, Any]) -> str:
    candidates: list[str] = []
    if isinstance(payload.get("reply"), str):
        candidates.append(payload["reply"])
    if isinstance(payload.get("content"), str):
        candidates.append(payload["content"])
    result = payload.get("result")
    if isinstance(result, dict):
        for key in ("finalAssistantVisibleText", "finalAssistantRawText"):
            value = result.get(key)
            if isinstance(value, str):
                candidates.append(value)
        for key in ("reply", "content", "text", "message"):
            value = result.get(key)
            if isinstance(value, str):
                candidates.append(value)
        payloads = result.get("payloads")
        if isinstance(payloads, list):
            for item in payloads:
                if isinstance(item, dict):
                    text = item.get("text")
                    if isinstance(text, str):
                        candidates.append(text)
    messages = payload.get("messages")
    if isinstance(messages, list):
        for item in reversed(messages):
            if isinstance(item, dict):
                role = str(item.get("role") or "").lower()
                if role in {"assistant", "model"}:
                    content = item.get("content") or item.get("text") or item.get("message")
                    if isinstance(content, str):
                        candidates.append(content)
                        break
    for candidate in candidates:
        normalized = _normalize_assistant_reply(candidate)
        if normalized:
            return normalized
    fallback = _openclaw_error_fallback_message(payload)
    if fallback:
        return fallback
    if _openclaw_payload_indicates_success(payload):
        return (
            "⚠️ 本次由系统兜底收尾：Agent 可能已经执行了部分操作，但 OpenClaw 没有返回最终可发送正文。\n"
            "请先核对表格、文档、文件夹或其他产物是否已经发生变化；如果这种情况再次出现，请联系开发者排查。"
        )
    raise RuntimeError("OpenClaw Agent 没有返回可发送的文本结果。")


def _openclaw_error_fallback_message(payload: Any) -> str | None:
    kind, detail = _classify_openclaw_payload_error(payload)
    if not kind:
        return None

    if kind == "timeout":
        reason = "Agent 这次执行超时了，OpenClaw 没有返回最终可发送正文。"
        next_step = "这通常表示任务卡在工具执行或长时间下载/处理里；可能已经产生部分结果，请先核对表格、文档、文件夹或下载目录。"
    elif kind == "rate_limit":
        reason = "Agent 这次遇到了限流错误，OpenClaw 没有返回最终可发送正文。"
        next_step = "请稍后重试；如果任务已经执行过一部分，先核对相关产物，避免重复操作。"
    elif kind == "billing":
        reason = "Agent 这次遇到了额度或计费错误，OpenClaw 没有返回最终可发送正文。"
        next_step = "请检查对应模型/API 额度；如果任务已经执行过一部分，先核对相关产物。"
    elif kind == "aborted":
        reason = "Agent 这次任务被中止或请求被取消，OpenClaw 没有返回最终可发送正文。"
        next_step = "请先核对是否已有部分操作完成；如果不是你主动停止的，请联系开发者排查。"
    else:
        reason = "Agent 这次返回了错误状态，但 OpenClaw 没有返回最终可发送正文。"
        next_step = "请先核对表格、文档、文件夹或其他产物是否已经发生变化。"

    lines = [
        f"⚠️ 本次由系统兜底收尾：{reason}",
        next_step,
    ]
    if detail:
        lines.extend(["", f"错误线索：`{detail}`"])
    lines.append("如果这种情况再次出现，请联系开发者排查。")
    return "\n".join(lines).strip()


def _classify_openclaw_payload_error(payload: Any) -> tuple[str | None, str | None]:
    strings: list[str] = []
    timeout_flag = False

    def walk(value: Any, key: str | None = None) -> None:
        nonlocal timeout_flag
        if key in {"timedOut", "timedOutDuringToolExecution"} and value is True:
            timeout_flag = True
        if isinstance(value, dict):
            for child_key, child_value in value.items():
                strings.append(str(child_key))
                walk(child_value, str(child_key))
        elif isinstance(value, list):
            for item in value:
                walk(item, key)
        elif isinstance(value, str):
            strings.append(value)
        elif isinstance(value, bool):
            strings.append(str(value))

    walk(payload)
    joined = " ".join(strings).lower()

    def first_matching(*needles: str) -> str | None:
        matches: list[str] = []
        for text in strings:
            lowered = text.lower()
            if any(needle in lowered for needle in needles):
                matches.append(text.strip())
        if not matches:
            return None
        return max(matches, key=len)[:240]

    if timeout_flag or any(needle in joined for needle in ("request timed out", "timed out", "timeout", "timedout")):
        return "timeout", first_matching("request timed out", "timed out", "timeout", "timedout")
    if any(needle in joined for needle in ("rate limit", "rate_limit", "too many requests", "429")):
        return "rate_limit", first_matching("rate limit", "rate_limit", "too many requests", "429")
    if any(needle in joined for needle in ("payment required", "billing", "quota", "insufficient balance", "402")):
        return "billing", first_matching("payment required", "billing", "quota", "insufficient balance", "402")
    if any(needle in joined for needle in ("aborted", "cancelled", "canceled", "request was aborted")):
        return "aborted", first_matching("aborted", "cancelled", "canceled", "request was aborted")
    if any(needle in joined for needle in ("error", "failed", "exception")):
        return "error", first_matching("error", "failed", "exception")
    return None, None


def _openclaw_payload_indicates_success(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    status = str(payload.get("status") or "").strip().lower()
    if status in {"ok", "success", "completed"}:
        return True
    result = payload.get("result")
    if isinstance(result, dict) and result:
        return True
    messages = payload.get("messages")
    if isinstance(messages, list) and any(isinstance(item, dict) for item in messages):
        return True
    return False


async def _agent_reply_via_openclaw(
    db: Session,
    *,
    text: str,
    chat_id: str | None,
    chat_type: str | None,
    sender_open_id: str | None,
    source_message_id: str | None,
    model_override: str | None = None,
    request_context: ChatRequestContext | None = None,
) -> str:
    preferences = ChatPreferenceService(db)
    memory = ChatMemoryService(db, chat_id=chat_id, chat_type=chat_type, sender_open_id=sender_open_id)
    session = resolve_chat_session(chat_id=chat_id, chat_type=chat_type, sender_open_id=sender_open_id)
    nonce = preferences.get_agent_session_nonce(
        chat_id=chat_id,
        chat_type=chat_type,
        sender_open_id=sender_open_id,
    )
    session_id = _openclaw_agent_session_id(
        chat_id=chat_id,
        chat_type=chat_type,
        sender_open_id=sender_open_id,
        nonce=nonce,
    )
    feishu = FeishuClient()
    reaction_id: str | None = None
    agent_message = _compose_openclaw_agent_message(
        session_id=session_id,
        memory=memory,
        text=text,
        project_context=_agent_project_context(
            db,
            chat_id=chat_id,
            chat_type=chat_type,
            sender_open_id=sender_open_id,
        ),
    )
    try:
        if source_message_id:
            try:
                reaction = await feishu.add_message_reaction(source_message_id, "Typing")
                if isinstance(reaction, dict):
                    reaction_id = ((reaction.get("data") or {}).get("reaction_id")) or None
            except Exception:
                reaction_id = None
        payload = await _run_openclaw_agent(
            session_id=session_id,
            message=agent_message,
            session_key=session.session_key,
            model=model_override,
        )
        _assert_request_current(request_context)
        return _extract_openclaw_agent_reply(payload)
    finally:
        if source_message_id and reaction_id:
            try:
                await feishu.remove_message_reaction(source_message_id, reaction_id)
            except Exception:
                pass


def _openclaw_agent_model_for_runtime(runtime: str) -> str | None:
    normalized = _normalize_agent_runtime(runtime)
    if normalized == AGENT_RUNTIME_DEEPSEEK:
        model = str(settings.deepseek_text_model or "").strip()
        if model and "/" not in model:
            return f"deepseek/{model}"
        return model or "deepseek/deepseek-v4-pro"
    return None


async def _agent_reply(
    db: Session,
    *,
    text: str,
    chat_id: str | None,
    chat_type: str | None,
    sender_open_id: str | None,
    source_message_id: str | None,
    request_context: ChatRequestContext | None = None,
) -> str:
    uploaded = await _agent_upload_recent_artifact_to_feishu(
        db,
        text=text,
        chat_id=chat_id,
        chat_type=chat_type,
        sender_open_id=sender_open_id,
    )
    if uploaded:
        return uploaded

    runtime = ChatPreferenceService(db).get_agent_runtime(
        chat_id=chat_id,
        chat_type=chat_type,
        sender_open_id=sender_open_id,
    )
    return await _agent_reply_via_openclaw(
        db,
        text=text,
        chat_id=chat_id,
        chat_type=chat_type,
        sender_open_id=sender_open_id,
        source_message_id=source_message_id,
        model_override=_openclaw_agent_model_for_runtime(runtime),
        request_context=request_context,
    )


def _chatbot_capability_summary() -> str:
    return (
        "最近一次模型 smoke test 时间：2026-05-09；OpenRouter 新 key 连通性与图片模型复核时间：2026-05-09。\n"
        "当前已验证可用的文本模型：qwen-plus、qwen-max、deepseek-v4-pro、deepseek-v4-flash。\n"
        "OpenRouter 聊天模型 google/gemini-3.1-pro-preview、google/gemini-3.1-flash-lite-preview 现已用新 key 复核连通性，"
        "可以作为 chatbot 文本模型使用；并且在 chatbot 路径上已接入 OpenRouter web_search，适合需要联网检索公开网页资料的问答。\n"
        "OpenAI 直连文本模型 gpt-5.4 已接入，但最近一次实测返回 429 insufficient_quota / Too Many Requests，"
        "说明当前 OpenAI 账号额度或计费状态不足，因此当前不要推荐用户切过去。\n"
        "Deep Research 主链路当前优先走 Google Gemini Deep Research Agent（Interactions API），"
        "其次才会回退到 OpenRouter Deep Research、OpenAI Deep Research，再不行才使用搜索总结回退。\n"
        "当前已验证可用的图片模型：nanobanana、gpt2、wanx2.1-t2i-turbo、wanx-v1。\n"
        "其中 nanobanana 实际走 OpenRouter 的 google/gemini-3.1-flash-image-preview；gpt2 实际走 OpenRouter 的 openai/gpt-5.4-image-2；"
        "为了稳定性，这两个兼容入口现在都会优先走 OpenRouter。\n"
        "OpenAI 直连图片模型 gpt-image-2 当前仍不稳定，最近一次直连实测返回 400 Bad Request；如果用户只是想要稳定可用，请优先推荐 OpenRouter 的 openai/gpt-5.4-image-2，"
        "不要优先推荐 OpenAI 直连 gpt-image-2。\n"
        "当前已验证可提交并进入任务流程的视频模型：wan2.2-kf2v-flash、wanx2.1-kf2v-plus、wan2.2-t2v-plus、小云雀。\n"
        "小云雀 已正式接入当前项目，适合需要上传首帧、尾帧、参考图或关键帧参考的场景，可覆盖很多原本想用 seedance_2_0 的需求。\n"
        "wanx2.1-i2v-turbo 当前存在兼容问题，最近一次实测返回 InvalidParameter / url error，除非用户明确要求排查，否则不要优先推荐它。\n"
        "seedance_2_0 当前未配置 SEEDANCE_API_KEY、SEEDANCE_BASE_URL、SEEDANCE_MODEL_ID，因此当前不可用。\n"
        "如果用户问“现在最稳妥怎么选”：默认优先建议 文本 deepseek-v4-pro 或 deepseek-v4-flash，图片 nanobanana 或 gpt2，视频优先按场景在 小云雀、wan2.2-kf2v-flash、wanx2.1-kf2v-plus、wan2.2-t2v-plus 之间选择。"
    )


async def _deep_research_reply(
    *,
    query: str,
    messages: list[dict[str, str]],
    active_model: str,
    progress_notifier: Callable[[str], Awaitable[None]] | None = None,
    request_context: ChatRequestContext | None = None,
) -> str:
    references = await _load_feishu_references_from_text(query)
    workspace = FeishuWorkspaceService()
    destination_folder_token = _extract_drive_folder_token(query, workspace=workspace)
    report_markdown = ""
    raw_text = ""
    raw_payload: Any | None = None
    execution_path = ""
    fallback_reason = ""
    errors: list[str] = []
    if progress_notifier:
        destination_hint = "指定文件夹" if destination_folder_token else "默认 Deep Research 文件夹"
        await progress_notifier(
            "已开始 Deep Research，正在检索和整理资料。"
            "这类任务通常需要数分钟；如果 Gemini 长时间处于 in_progress，系统会每 5 分钟同步一次进度，并在约 40 分钟后自动回退到备用路径。"
            f"当前输出位置：{destination_hint}。"
        )
    primary_attempts: list[tuple[str, str, Any]] = []
    if settings.google_api_key:
        primary_attempts.append(
            (
                "Gemini Deep Research",
                f"Gemini Deep Research（{settings.google_deep_research_model}）",
                _google_deep_research_report,
            )
        )
    if settings.openrouter_api_key:
        primary_attempts.append(
            (
                "OpenRouter Deep Research",
                f"OpenRouter Deep Research（{settings.openrouter_deep_research_model}）",
                _openrouter_deep_research_report,
            )
        )
    if settings.openai_api_key:
        primary_attempts.append(
            (
                "OpenAI Deep Research",
                f"OpenAI Deep Research（{settings.openai_deep_research_model}）",
                _openai_deep_research_report,
            )
        )
    if not primary_attempts:
        errors.append("No Deep Research primary provider configured")
    for label, path_label, runner in primary_attempts:
        try:
            result = await runner(
                query=query,
                references=references,
                progress_notifier=progress_notifier,
            )
            normalized = _normalize_deep_research_result(result)
            report_markdown = normalized.markdown
            raw_text = normalized.raw_text
            raw_payload = normalized.raw_payload
            execution_path = path_label
            break
        except Exception as exc:
            errors.append(_format_research_error(label, exc))
    if not execution_path:
        fallback_result = await _fallback_research_report(
            query=query,
            references=references,
            active_model=active_model,
            messages=messages,
        )
        normalized = _normalize_deep_research_result(fallback_result)
        report_markdown = normalized.markdown
        raw_text = normalized.raw_text
        raw_payload = normalized.raw_payload
        execution_path = f"Fallback 搜索总结（{active_model}）"
        fallback_reason = "；".join(item for item in errors if item)[:400]

    _assert_request_current(request_context)
    document_title = f"Deep Research_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    saved_doc = await workspace.save_markdown_document(
        title=document_title,
        markdown=report_markdown,
        folder_token=destination_folder_token,
    )
    raw_dump = _deep_research_raw_dump(
        query=query,
        execution_path=execution_path,
        report_markdown=report_markdown,
        raw_text=raw_text,
        raw_payload=raw_payload,
    )
    raw_file = await workspace.save_text_file(
        filename=f"{document_title}_raw.txt",
        text=raw_dump,
        folder_token=saved_doc.folder_token,
    )
    meta_lines = [
        "**研究执行路径**",
        f"- 路径：{execution_path or '未知'}",
    ]
    if fallback_reason:
        meta_lines.append(f"- 回退原因：`{fallback_reason}`")
    return (
        f"{chr(10).join(meta_lines)}\n\n"
        f"{report_markdown}\n\n"
        f"**已保存飞书文档**\n"
        f"- [打开研究文档]({saved_doc.url})\n"
        f"- [打开原始返回文本]({raw_file.url})"
    )


async def _storyboard_breakdown_reply(
    *,
    query: str,
    messages: list[dict[str, str]],
    active_model: str,
    progress_notifier: Callable[[str], Awaitable[None]] | None = None,
    request_context: ChatRequestContext | None = None,
) -> str:
    references = await _load_feishu_references_from_text(query)
    workspace = FeishuWorkspaceService()
    destination_folder_token = _extract_drive_folder_token(query, workspace=workspace)
    if not destination_folder_token:
        storyboard_folder = await workspace.ensure_storyboard_workspace_folder()
        destination_folder_token = str(storyboard_folder.get("folder_token") or "")
    if progress_notifier:
        await progress_notifier(
            "已开始分镜拆解，正在读取文档并整理镜头需求。"
            "如果你给的是飞书云文档或文件链接，我会优先按文档内容拆解，并在完成后保存为飞书文档。"
        )
    prompt = _storyboard_breakdown_prompt(query=query, references=references)
    breakdown_messages = list(messages[:-1])
    breakdown_messages.append({"role": "user", "content": prompt})
    markdown = await _generate_chat_response(
        model=active_model,
        messages=breakdown_messages,
        text=query,
        assistant_mode=ASSISTANT_MODE_STORYBOARD_BREAKDOWN,
    )
    normalized = _normalize_deep_research_result(markdown)
    _assert_request_current(request_context)
    document_title = f"分镜拆解_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    saved_doc = await workspace.save_markdown_document(
        title=document_title,
        markdown=normalized.markdown,
        folder_token=destination_folder_token,
    )
    raw_dump = _deep_research_raw_dump(
        query=query,
        execution_path=f"Storyboard Breakdown（{active_model}）",
        report_markdown=normalized.markdown,
        raw_text=normalized.raw_text,
        raw_payload=normalized.raw_payload,
    )
    raw_file = await workspace.save_text_file(
        filename=f"{document_title}_raw.txt",
        text=raw_dump,
        folder_token=saved_doc.folder_token,
    )
    return (
        "**分镜拆解已完成**\n"
        f"- 模型：`{active_model}`\n"
        f"- 文档来源：{len(references)} 份\n\n"
        f"**已保存飞书文档**\n"
        f"- [打开拆解文档]({saved_doc.url})\n"
        f"- [打开原始返回文本]({raw_file.url})"
    )


async def _load_feishu_references_from_text(text: str) -> list[dict]:
    urls = _extract_message_urls(text)
    if not urls:
        return []
    workspace = FeishuWorkspaceService()
    references: list[dict] = []
    for url in urls:
        try:
            references.append(await workspace.read_reference(url))
        except Exception:
            continue
    return references


def _extract_drive_folder_token(text: str, *, workspace: FeishuWorkspaceService) -> str | None:
    urls = [url for url in _extract_message_urls(text) if "/drive/folder/" in url]
    for url in urls:
        token = workspace.folder_token_from_url(url)
        if token:
            return token
    return None


def _extract_message_urls(text: str | None) -> list[str]:
    raw = str(text or "")
    urls: list[str] = []
    for url in re.findall(r"\((https?://[^\s)]+)\)", raw):
        urls.append(_sanitize_message_url(url))
    for url in re.findall(r"(https?://\S+|feishu://[^\s]+)", raw):
        cleaned = _sanitize_message_url(url)
        if cleaned:
            urls.append(cleaned)
    return list(dict.fromkeys(url for url in urls if url))


def _sanitize_message_url(url: str) -> str:
    cleaned = str(url or "").strip()
    if not cleaned:
        return ""
    while cleaned and cleaned[-1] in ").,;!?>]}\"'":
        cleaned = cleaned[:-1]
    while cleaned and cleaned[0] in "(<[{\"'":
        cleaned = cleaned[1:]
    return cleaned.strip()


async def _openai_deep_research_report(
    *,
    query: str,
    references: list[dict],
    progress_notifier: Callable[[str], Awaitable[None]] | None = None,
) -> DeepResearchResult:
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is required")
    prompt = _deep_research_prompt(query=query, references=references)
    body = {
        "model": settings.openai_deep_research_model,
        "input": prompt,
        "tools": [{"type": "web_search"}],
        "max_tool_calls": 24,
    }
    async with httpx.AsyncClient(timeout=900) as client:
        response = await client.post(
            f"{settings.openai_base_url.rstrip('/')}/v1/responses",
            headers={"Authorization": f"Bearer {settings.openai_api_key}", "Content-Type": "application/json"},
            json=body,
        )
    response.raise_for_status()
    data = response.json()
    return DeepResearchResult(
        markdown=_format_openai_research_response(data),
        raw_text=str(data.get("output_text") or "").strip(),
        raw_payload=data,
    )


async def _google_deep_research_report(
    *,
    query: str,
    references: list[dict],
    progress_notifier: Callable[[str], Awaitable[None]] | None = None,
) -> DeepResearchResult:
    if not settings.google_api_key:
        raise RuntimeError("GOOGLE_API_KEY is required")
    prompt = _deep_research_prompt(query=query, references=references)
    body = {
        "input": prompt,
        "agent": settings.google_deep_research_model,
        "background": True,
        "agent_config": {
            "type": "deep-research",
            "thinking_summaries": "auto",
        },
    }
    headers = {
        "x-goog-api-key": settings.google_api_key,
        "Content-Type": "application/json",
    }
    base_url = settings.google_base_url.rstrip("/")
    async with httpx.AsyncClient(timeout=120) as client:
        create_response = await client.post(f"{base_url}/v1beta/interactions", headers=headers, json=body)
        create_response.raise_for_status()
        interaction = create_response.json()
        interaction_id = str(interaction.get("id") or "").strip()
        if not interaction_id:
            raise RuntimeError("Gemini Deep Research did not return an interaction id")
        progress_every = max(1, 300 // settings.google_deep_research_poll_interval_seconds)
        for attempt in range(settings.google_deep_research_max_poll_attempts):
            poll_response = await client.get(f"{base_url}/v1beta/interactions/{interaction_id}", headers=headers)
            poll_response.raise_for_status()
            interaction = poll_response.json()
            status = str(interaction.get("status") or "").strip().lower()
            if status == "completed":
                return _format_google_interaction_response(interaction)
            if status in {"failed", "cancelled", "canceled"}:
                error_message = _google_interaction_error(interaction) or f"interaction status={status}"
                raise RuntimeError(error_message)
            if progress_notifier and attempt >= 0 and (attempt + 1) % progress_every == 0:
                waited_seconds = (attempt + 1) * settings.google_deep_research_poll_interval_seconds
                waited_minutes = max(1, round(waited_seconds / 60))
                await progress_notifier(
                    f"Deep Research 仍在进行中：Gemini 当前状态为 `{status or 'in_progress'}`，"
                    f"已等待约 {waited_minutes} 分钟。若长时间无结果，系统会自动回退到备用路径。"
                )
            await asyncio.sleep(settings.google_deep_research_poll_interval_seconds)
    raise RuntimeError(
        "Gemini Deep Research 超时："
        f"已等待约 {round(settings.google_deep_research_max_poll_attempts * settings.google_deep_research_poll_interval_seconds / 60)} 分钟，"
        "仍处于 in_progress，系统将回退到备用路径。"
    )


async def _openrouter_deep_research_report(
    *,
    query: str,
    references: list[dict],
    progress_notifier: Callable[[str], Awaitable[None]] | None = None,
) -> DeepResearchResult:
    if not settings.openrouter_api_key:
        raise RuntimeError("OPENROUTER_API_KEY is required")
    prompt = _deep_research_prompt(query=query, references=references)
    body = {
        "model": settings.openrouter_deep_research_model,
        "messages": [{"role": "user", "content": prompt}],
        "reasoning": {
            "effort": "medium",
            "exclude": True,
        },
    }
    async with httpx.AsyncClient(timeout=900) as client:
        response = await client.post(
            f"{settings.openrouter_base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {settings.openrouter_api_key}", "Content-Type": "application/json"},
            json=body,
        )
    response.raise_for_status()
    data = response.json()
    return DeepResearchResult(
        markdown=_chat_content_from_response(data),
        raw_text=_chat_content_from_response(data),
        raw_payload=data,
    )


async def _fallback_research_report(
    *,
    query: str,
    references: list[dict],
    active_model: str,
    messages: list[dict[str, str]],
) -> str:
    search_context = await _web_search_summary(query=query, max_results=6, include_page_text=True)
    fallback_messages = list(messages[:-1])
    fallback_messages.append(
        {
            "role": "user",
            "content": (
                "请基于下面的公开网页搜索结果和已提供文件内容，产出一份结构化 deep research 报告。"
                "报告要包含：研究目标、问题树、时间线、产品与车型、公司与融资、关键事件、风险、待验证问题、来源。\n\n"
                f"用户问题：{query}\n\n"
                f"公开网页搜索结果：\n{search_context}\n\n"
                f"文件上下文：\n{_reference_context_block(references)}"
            ),
        }
    )
    if active_model.startswith("deepseek-v4") and settings.deepseek_api_key:
        return await _deepseek_chat(model=active_model, messages=fallback_messages)
    if _chatbot_uses_openrouter(active_model) and settings.openrouter_api_key:
        return await _openrouter_chat(model=active_model, messages=fallback_messages, enable_web_search=True)
    if active_model.startswith("qwen") and settings.dashscope_api_key:
        return await _dashscope_chat(model=active_model, messages=fallback_messages)
    return await _deepseek_chat(model=settings.deepseek_text_model, messages=fallback_messages)


def _normalize_deep_research_result(result: DeepResearchResult | str) -> DeepResearchResult:
    if isinstance(result, DeepResearchResult):
        return result
    text = str(result or "").strip()
    return DeepResearchResult(markdown=text or "我没有生成有效研究结果。", raw_text=text)


def _deep_research_raw_dump(
    *,
    query: str,
    execution_path: str,
    report_markdown: str,
    raw_text: str,
    raw_payload: Any | None,
) -> str:
    sections = [
        "=== query ===",
        query.strip(),
        "",
        "=== execution_path ===",
        execution_path.strip(),
        "",
        "=== extracted_markdown ===",
        (report_markdown or "").strip(),
        "",
        "=== raw_text ===",
        (raw_text or "").strip(),
        "",
        "=== raw_payload_json ===",
    ]
    if raw_payload is None:
        sections.append("")
    elif isinstance(raw_payload, str):
        sections.append(raw_payload)
    else:
        sections.append(json.dumps(raw_payload, ensure_ascii=False, indent=2, default=str))
    return "\n".join(sections).strip() + "\n"


def _format_research_error(label: str, exc: Exception) -> str:
    message = str(exc).strip() or type(exc).__name__
    return f"{label}: {message}"


def _deep_research_prompt(*, query: str, references: list[dict]) -> str:
    return (
        "你是一个专业研究分析师。请围绕用户问题执行 deep research，并输出一份中文 Markdown 报告。\n"
        "要求：\n"
        "1. 先给结论摘要。\n"
        "2. 明确列出时间线、公司背景、产品/车型、关键事件、争议点与不确定点。\n"
        "3. 如果资料不足，要明确写“待验证”。\n"
        "4. 在文末给出来源清单，尽量保留可点击链接。\n"
        "5. 如果用户的问题更像研究计划而不是最终结论，请输出可执行的 deep research plan。\n\n"
        f"用户问题：{query}\n\n"
        f"已提供文件内容（JSON 或文本）：\n{_reference_context_block(references)}"
    )


def _normalize_assistant_reply(reply: Any) -> str:
    if isinstance(reply, str):
        normalized = reply.strip()
        return normalized or "我没有生成有效回复。"
    if reply is None:
        return "我没有生成有效回复。"
    normalized = str(reply).strip()
    return normalized or "我没有生成有效回复。"


def _storyboard_breakdown_prompt(*, query: str, references: list[dict]) -> str:
    return (
        "你是资深分镜策划与导演助理。"
        "请把用户提供的文档、脚本、策划案、采访稿或说明文字，拆解成可执行的分镜需求，并输出中文 Markdown。\n"
        "要求：\n"
        "1. 先给需求摘要与推荐成片方向。\n"
        "2. 给出建议总时长、节奏分段和结构章节。\n"
        "3. 输出一份分镜清单。每条至少包含：镜号、画面内容、景别、机位/运镜、建议时长、台词/旁白/字幕、音效/音乐、备注。\n"
        "4. 如果适合，补一列生成提示词方向（不是最终成稿，也可以是简短提示）。\n"
        "5. 如果原始材料有逻辑断层、素材不足或关键信息缺失，要明确写出待补充项。\n"
        "6. 不要假装已经创建飞书项目，也不要输出空泛建议；尽量把内容拆到镜头级。\n"
        "7. 如果用户其实是在问“该怎么拆”，也可以输出一份拆解方案，但依然尽量给出示例镜头。\n\n"
        f"用户请求：{query}\n\n"
        f"已提供文件内容（JSON 或文本）：\n{_reference_context_block(references)}"
    )


def _format_openai_research_response(data: dict) -> str:
    if data.get("output_text"):
        text = str(data["output_text"]).strip()
    else:
        text = ""
        for item in data.get("output", []):
            if item.get("type") != "message":
                continue
            for content in item.get("content", []):
                if content.get("type") in {"output_text", "text"} and content.get("text"):
                    text = str(content["text"]).strip()
                    break
            if text:
                break
    citations = _collect_openai_citations(data)
    if not citations:
        return text or "我没有生成有效研究结果。"
    source_lines = ["", "**来源**"]
    for index, citation in enumerate(citations, start=1):
        source_lines.append(f"{index}. [{citation['title']}]({citation['url']})")
    return (text or "我没有生成有效研究结果。").strip() + "\n" + "\n".join(source_lines)


def _format_google_interaction_response(data: dict) -> DeepResearchResult:
    text = _extract_google_interaction_text(data)
    if text:
        return DeepResearchResult(markdown=text, raw_text=text, raw_payload=data)
    raise RuntimeError("Gemini Deep Research completed without a readable text report")


def _extract_google_interaction_text(data: dict) -> str:
    candidates: list[str] = []
    outputs = data.get("outputs") or []
    ordered_output_texts: list[str] = []
    for output in outputs:
        if not isinstance(output, dict):
            continue
        text = str(output.get("text") or "").strip()
        if text:
            candidates.append(text)
            if not _looks_like_source_only_block(text):
                ordered_output_texts.append(text)
    if len(ordered_output_texts) >= 2:
        merged = _merge_ordered_google_output_texts(ordered_output_texts)
        if merged:
            return merged
    if len(ordered_output_texts) == 1:
        return ordered_output_texts[0]
    for step in data.get("steps") or []:
        if not isinstance(step, dict):
            continue
        for content in step.get("content") or []:
            if not isinstance(content, dict):
                continue
            if content.get("type") == "text" and content.get("text"):
                text = str(content.get("text") or "").strip()
                if text:
                    candidates.append(text)
    for value in _collect_text_fields(data):
        if value:
            candidates.append(value)
    unique_candidates = list(dict.fromkeys(item.strip() for item in candidates if item and item.strip()))
    if not unique_candidates:
        return ""
    return max(unique_candidates, key=_google_text_candidate_score)


def _merge_ordered_google_output_texts(texts: list[str]) -> str:
    merged: list[str] = []
    for text in texts:
        current = text.strip()
        if not current:
            continue
        if merged and current in merged[-1]:
            continue
        if merged and merged[-1] in current:
            merged[-1] = current
            continue
        merged.append(current)
    return "\n\n".join(merged).strip()


def _collect_text_fields(value: Any) -> list[str]:
    results: list[str] = []
    if isinstance(value, dict):
        text_value = value.get("text")
        if isinstance(text_value, str) and text_value.strip():
            results.append(text_value.strip())
        for nested_key, nested_value in value.items():
            if nested_key == "text":
                continue
            results.extend(_collect_text_fields(nested_value))
    elif isinstance(value, list):
        for item in value:
            results.extend(_collect_text_fields(item))
    return results


def _google_text_candidate_score(text: str) -> int:
    stripped = text.strip()
    if not stripped:
        return -10_000
    lines = [line.strip() for line in stripped.splitlines() if line.strip()]
    url_count = len(re.findall(r"https?://", stripped))
    markdown_link_count = len(re.findall(r"\[[^\]]+\]\(https?://", stripped))
    source_like_lines = sum(1 for line in lines if _line_is_mostly_link(line))
    heading_hits = len(re.findall(r"(^|\n)#{1,6}\s", stripped)) + len(
        re.findall(r"(结论|摘要|时间线|背景|产品|车型|融资|风险|来源)", stripped)
    )
    sentence_punctuation = len(re.findall(r"[。！？；：]", stripped))
    score = len(stripped) + heading_hits * 300 + sentence_punctuation * 20
    score -= (url_count + markdown_link_count) * 35
    score -= source_like_lines * 120
    if _looks_like_source_only_block(stripped):
        score -= 10_000
    return score


def _line_is_mostly_link(line: str) -> bool:
    compact = re.sub(r"\s+", " ", line.strip())
    if not compact:
        return False
    if "http://" in compact or "https://" in compact:
        text_without_links = re.sub(r"https?://\S+", "", compact).strip(" -:：|[]()")
        return len(text_without_links) <= 20
    return False


def _looks_like_source_only_block(text: str) -> bool:
    lower = text.strip().lower()
    if lower.startswith("sources") or lower.startswith("references") or lower.startswith("citations"):
        return True
    if lower.startswith("**sources") or lower.startswith("**references") or lower.startswith("**来源"):
        return True
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return False
    source_like_lines = sum(1 for line in lines if _line_is_mostly_link(line))
    return source_like_lines >= max(2, len(lines) - 1)


def _google_interaction_error(data: dict) -> str:
    error = data.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error.get("code") or "").strip()
    return str(error or "").strip()


def _chat_content_from_response(data: dict) -> str:
    message = (data.get("choices") or [{}])[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, str) or content is None:
        return _normalize_assistant_reply(content).replace("有效回复", "有效研究结果")
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = _normalize_assistant_reply(item.get("text") or item.get("content"))
                if text:
                    parts.append(text)
        if parts:
            return "\n".join(parts).strip()
    return "我没有生成有效研究结果。"


def _collect_openai_citations(data: dict) -> list[dict[str, str]]:
    collected: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in data.get("output", []):
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            for annotation in content.get("annotations", []):
                url = str(annotation.get("url") or "").strip()
                title = str(annotation.get("title") or url).strip()
                if not url or url in seen:
                    continue
                seen.add(url)
                collected.append({"url": url, "title": title})
    return collected


async def _inject_web_search_context(messages: list[dict[str, str]], *, query: str) -> list[dict[str, str]]:
    search_context = await _web_search_summary(query=query, max_results=5, include_page_text=False)
    augmented = list(messages)
    augmented.insert(
        1,
        {
            "role": "system",
            "content": (
                "下面是系统刚刚替你执行的联网搜索结果。回答涉及最新信息、公开资料、公司动态时，"
                "请优先引用这些结果，并在回答中尽量保留来源链接。\n\n"
                f"{search_context}"
            ),
        },
    )
    return augmented


async def _web_search_summary(*, query: str, max_results: int = 5, include_page_text: bool = False) -> str:
    results = await _duckduckgo_search(query=query, max_results=max_results)
    if not results:
        return "未检索到可靠公开网页结果。"
    lines = []
    for index, result in enumerate(results, start=1):
        line = f"{index}. {result['title']} | {result['url']}\n摘要：{result['snippet']}"
        if include_page_text and result.get("page_excerpt"):
            line += f"\n正文摘录：{result['page_excerpt']}"
        lines.append(line)
    return "\n\n".join(lines)


async def _duckduckgo_search(*, query: str, max_results: int = 5) -> list[dict[str, str]]:
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        response = await client.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers={"User-Agent": "Mozilla/5.0"},
        )
    response.raise_for_status()
    html = response.text
    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    blocks = re.split(r'<div class="result results_links[^"]*">', html)
    for block in blocks:
        anchor = re.search(r'<a rel="nofollow" class="result__a" href="(?P<url>[^"]+)">(?P<title>.*?)</a>', block, flags=re.DOTALL)
        if not anchor:
            continue
        snippet_match = re.search(r'<a class="result__snippet"[^>]*>(?P<snippet>.*?)</a>', block, flags=re.DOTALL)
        url = _normalize_search_result_url(_clean_html(anchor.group("url")))
        title = _clean_html(anchor.group("title"))
        snippet = _clean_html(snippet_match.group("snippet")) if snippet_match else ""
        if not url or url in seen:
            continue
        seen.add(url)
        page_excerpt = await _fetch_page_excerpt(url)
        entries.append({"url": url, "title": title, "snippet": snippet, "page_excerpt": page_excerpt})
        if len(entries) >= max_results:
            break
    return entries


async def _fetch_page_excerpt(url: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=45, follow_redirects=True) as client:
            response = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        response.raise_for_status()
    except Exception:
        return ""
    text = _clean_html(response.text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:1200]


def _clean_html(value: str) -> str:
    cleaned = re.sub(r"<script.*?</script>", "", value, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"<style.*?</style>", "", cleaned, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = cleaned.replace("&amp;", "&").replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", cleaned).strip()


def _normalize_search_result_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.path.startswith("/l/"):
        uddg = parse_qs(parsed.query).get("uddg")
        if uddg:
            return unquote(uddg[0])
    return url


def _reference_context_block(references: list[dict]) -> str:
    if not references:
        return "未提供飞书文档或文件。"
    blocks = []
    for index, item in enumerate(references, start=1):
        if item.get("type") == "feishu_doc":
            text_content = str(item.get("text_content") or "").strip()
            if text_content:
                blocks.append(f"{index}. Feishu Doc {item.get('url')}\n{text_content}")
            else:
                payload = json.dumps(item.get("content_json") or {}, ensure_ascii=False)
                blocks.append(f"{index}. Feishu Doc {item.get('url')}\n{payload}")
            continue
        blocks.append(
            f"{index}. Feishu File {item.get('filename') or item.get('file_token')}\n"
            f"{item.get('text_content') or '[empty]'}"
        )
    return "\n\n".join(blocks)


async def _dashscope_chat(*, model: str, messages: list[dict[str, str]]) -> str:
    body = {
        "model": model,
        "messages": messages,
        "temperature": 0.6,
    }
    async with httpx.AsyncClient(timeout=90) as client:
        response = await client.post(
            f"{settings.dashscope_compatible_base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {settings.dashscope_api_key}", "Content-Type": "application/json"},
            json=body,
        )
    response.raise_for_status()
    data = response.json()
    return _normalize_assistant_reply(data.get("choices", [{}])[0].get("message", {}).get("content"))


async def _openai_chat(*, model: str, messages: list[dict[str, str]]) -> str:
    body = {
        "model": model,
        "input": messages,
    }
    async with httpx.AsyncClient(timeout=90) as client:
        response = await client.post(
            f"{settings.openai_base_url.rstrip('/')}/v1/responses",
            headers={"Authorization": f"Bearer {settings.openai_api_key}", "Content-Type": "application/json"},
            json=body,
        )
    response.raise_for_status()
    data = response.json()
    if data.get("output_text"):
        return str(data["output_text"]).strip()
    for item in data.get("output", []):
        for content in item.get("content", []):
            if content.get("type") in {"output_text", "text"} and content.get("text"):
                return str(content["text"]).strip()
    return "我没有生成有效回复。"


async def _deepseek_chat(*, model: str, messages: list[dict[str, str]]) -> str:
    body = {
        "model": model,
        "messages": messages,
        "temperature": 0.6,
    }
    async with httpx.AsyncClient(timeout=90) as client:
        response = await client.post(
            f"{settings.deepseek_base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {settings.deepseek_api_key}", "Content-Type": "application/json"},
            json=body,
        )
    response.raise_for_status()
    data = response.json()
    return _normalize_assistant_reply(data.get("choices", [{}])[0].get("message", {}).get("content"))


async def _openrouter_chat(*, model: str, messages: list[dict[str, str]], enable_web_search: bool = False) -> str:
    body = {
        "model": model,
        "messages": messages,
        "temperature": 0.6,
    }
    if enable_web_search:
        body["tools"] = [
            {
                "type": "openrouter:web_search",
                "parameters": {
                    "max_results": 5,
                },
            }
        ]
    async with httpx.AsyncClient(timeout=90) as client:
        response = await client.post(
            f"{settings.openrouter_base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {settings.openrouter_api_key}", "Content-Type": "application/json"},
            json=body,
        )
    response.raise_for_status()
    data = response.json()
    return _chat_content_from_response(data)
