from __future__ import annotations

import asyncio
import json
import mimetypes
import re
import tempfile
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from sqlalchemy.orm import Session

from app.adapters.feishu import FeishuApiError, FeishuClient
from app.adapters.feishu_cards import chatbot_reply_card, help_card
from app.core.config import settings
from app.core.model_aliases import IMAGE_MODEL_GPT2, IMAGE_MODEL_NANOBANANA, VIDEO_MODEL_XYQ, normalize_image_model, normalize_video_model
from app.models.project import Project
from app.services.chat_memory import ChatMemoryService
from app.services.chat_preferences import ChatPreferenceService
from app.services.feishu_storyboard import FeishuStoryboardService
from app.services.feishu_workspace import FeishuWorkspaceService
from app.services.projects import ProjectService
from app.services.workflow import WorkflowService
from app.providers.router import ProviderRouter

LAST_MODEL_SMOKE_TEST_DATE = "2026-05-08"
ASSISTANT_MODE_CHAT = "chat"
ASSISTANT_MODE_STORYBOARD = "storyboard"
ASSISTANT_MODE_DEEP_RESEARCH = "deep_research"
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
    "reference_images": {"参考图", "图片", "文件", "文件地址", "飞书地址", "reference", "reference_images"},
    "first_frame": {"首帧", "first_frame"},
    "last_frame": {"尾帧", "last_frame"},
    "keyframes": {"关键帧", "keyframes"},
}


@dataclass(frozen=True)
class DirectGenerationCommand:
    kind: str
    model: str
    prompt: str
    negative_prompt: str = ""
    size: str | None = None
    duration_seconds: int | None = None
    reference_images: tuple[str, ...] = ()
    first_frame: tuple[str, ...] = ()
    last_frame: tuple[str, ...] = ()
    keyframes: tuple[str, ...] = ()


@dataclass(frozen=True)
class AssistantModeCommand:
    mode: str
    prompt: str = ""


async def handle_bot_text(
    db: Session,
    *,
    text: str,
    chat_id: str | None = None,
    chat_type: str | None = None,
    sender_open_id: str | None = None,
) -> dict[str, Any] | None:
    target_chat = chat_id or settings.feishu_default_chat_id
    feishu = FeishuClient()
    if not _is_slash_command(text):
        current_mode = ChatPreferenceService(db).get_assistant_mode(
            chat_id=target_chat,
            chat_type=chat_type,
            sender_open_id=sender_open_id,
        )
        reply = await _chatbot_reply(
            db,
            text=text,
            chat_id=target_chat,
            chat_type=chat_type,
            sender_open_id=sender_open_id,
        )
        if target_chat and reply:
            await feishu.send_card(
                target_chat,
                chatbot_reply_card(
                    content=reply,
                    title=_assistant_card_title(current_mode),
                    chat_type=chat_type,
                    sender_open_id=sender_open_id,
                ),
            )
        return {"message": "chatbot 已回复", "data": {"chat_id": target_chat}}

    command_text = _command_text(text)
    if _is_help_command(command_text):
        if target_chat:
            await feishu.send_card(target_chat, help_card())
        return {"message": "帮助已发送", "data": {"chat_id": target_chat}}

    if _is_new_session_command(command_text):
        cleared = ChatMemoryService(
            db,
            chat_id=target_chat,
            chat_type=chat_type,
            sender_open_id=sender_open_id,
        ).clear()
        db.commit()
        if target_chat:
            await feishu.send_text(target_chat, "当前会话的聊天记录已重置。")
        return {"message": "聊天记录已重置", "data": {"chat_id": target_chat, "cleared_messages": cleared}}

    mode_command = _parse_assistant_mode_command(command_text)
    if mode_command:
        ChatPreferenceService(db).set_assistant_mode(
            chat_id=target_chat,
            chat_type=chat_type,
            sender_open_id=sender_open_id,
            mode=mode_command.mode,
        )
        db.commit()
        if not mode_command.prompt:
            mode_name = _assistant_mode_label(mode_command.mode)
            if target_chat:
                await feishu.send_text(target_chat, f"当前会话已切换为：{mode_name}")
            return {"message": "聊天模式已切换", "data": {"mode": mode_command.mode}}
        reply = await _chatbot_reply(
            db,
            text=mode_command.prompt,
            chat_id=target_chat,
            chat_type=chat_type,
            sender_open_id=sender_open_id,
        )
        if target_chat and reply:
            await feishu.send_card(
                target_chat,
                chatbot_reply_card(
                    content=reply,
                    title=_assistant_card_title(mode_command.mode),
                    chat_type=chat_type,
                    sender_open_id=sender_open_id,
                ),
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

    switch_model = _parse_chatbot_model_command(command_text)
    if switch_model:
        ChatPreferenceService(db).set_chatbot_text_model(
            chat_id=target_chat,
            chat_type=chat_type,
            sender_open_id=sender_open_id,
            model=switch_model,
        )
        project = ProjectService(db).latest_for_chat(target_chat)
        if project:
            project.workflow_config = {**(project.workflow_config or {}), "chatbot_text_model": switch_model}
        db.commit()
        if target_chat:
            await feishu.send_text(target_chat, f"chatbot 文本模型已切换为：{switch_model}")
        return {"message": "chatbot 模型已切换", "data": {"model": switch_model}}

    project_name = _parse_create_project_command(command_text)
    if project_name:
        project_name_text, folder_url = _parse_create_project_command_parts(command_text)
        provisioned = await FeishuStoryboardService(db).create_project_from_bot(
            project_name=project_name_text,
            chat_id=target_chat,
            parent_folder_url=folder_url,
        )
        return {
            "message": "项目已创建",
            "data": {"project_id": str(provisioned.project.id), "table_url": provisioned.table_url},
        }

    command = _parse_project_command(command_text)
    if command:
        project = ProjectService(db).latest_for_chat(target_chat)
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
    action = value.get("action")
    project_id = value.get("project_id")
    batch_no = value.get("batch_no") or "batch_001"
    chat_type = value.get("chat_type")
    sender_open_id = value.get("sender_open_id")
    service = FeishuStoryboardService(db)
    if not action:
        return {"message": "卡片动作已接收", "data": {"action": action}}

    if action == "assistant.clear_session":
        cleared = ChatMemoryService(
            db,
            chat_id=chat_id,
            chat_type=chat_type,
            sender_open_id=sender_open_id,
        ).clear()
        db.commit()
        if chat_id:
            await FeishuClient().send_text(chat_id, "当前会话的聊天记录已重置。")
        return {"message": "聊天记录已重置", "data": {"chat_id": chat_id, "cleared_messages": cleared}}

    if action == "assistant.set_mode":
        mode = str(value.get("mode") or ASSISTANT_MODE_CHAT)
        ChatPreferenceService(db).set_assistant_mode(
            chat_id=chat_id,
            chat_type=chat_type,
            sender_open_id=sender_open_id,
            mode=mode,
        )
        db.commit()
        if chat_id:
            await FeishuClient().send_text(chat_id, f"当前会话已切换为：{_assistant_mode_label(mode)}")
        return {"message": "聊天模式已切换", "data": {"chat_id": chat_id, "mode": mode}}

    if not project_id:
        return {"message": "卡片动作已接收", "data": {"action": action}}

    project = ProjectService(db).get_project(project_id)
    if not project:
        return {"message": "项目不存在", "data": {"project_id": project_id}}

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
        if chat_id:
            await service.send_progress(project, chat_id=chat_id)
        return {
            "message": "首尾帧同步已开启" if enabled else "首尾帧同步已关闭",
            "data": {"project_id": project_id, "synced": synced, "enabled": enabled},
        }

    if action in {"project.enable_keyframes", "project.set_keyframes"}:
        enabled = _bool_value(value.get("enabled"), default=True)
        await service.set_keyframe_generation(project, enabled)
        if chat_id:
            await service.send_progress(project, chat_id=chat_id)
        return {
            "message": "关键帧生成已开启" if enabled else "关键帧生成已关闭",
            "data": {"project_id": project_id, "enabled": enabled},
        }

    if action == "batch.optimize_prompt":
        shots = await service.optimize_current_batch(project=project, batch_no=batch_no)
        return {"message": "当前批次 Prompt 已优化", "data": {"project_id": project_id, "batch_no": batch_no, "shots": len(shots)}}

    if action == "project.progress":
        stats = await service.send_progress(project, chat_id=chat_id)
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
    normalized = re.sub(r"\s+", " ", text.strip()).lower()
    return normalized in {"new session", "newsession", "重置聊天", "重置会话", "清空聊天记录", "清空会话"}


def _parse_assistant_mode_command(text: str) -> AssistantModeCommand | None:
    normalized = text.strip()
    patterns = [
        (r"^(?:deep\s*research|deepresearch)(?:\s*[:：]\s*|\s+)?(.*)$", ASSISTANT_MODE_DEEP_RESEARCH),
        (r"^(?:分镜助手|ai分镜助手|视频助手)(?:\s*[:：]\s*|\s+)?(.*)$", ASSISTANT_MODE_STORYBOARD),
        (r"^(?:普通助手|聊天助手|普通对话|chat)(?:\s*[:：]\s*|\s+)?(.*)$", ASSISTANT_MODE_CHAT),
    ]
    for pattern, mode in patterns:
        match = re.match(pattern, normalized, flags=re.IGNORECASE)
        if match:
            return AssistantModeCommand(mode=mode, prompt=(match.group(1) or "").strip())
    return None


def _assistant_mode_label(mode: str) -> str:
    return {
        ASSISTANT_MODE_CHAT: "普通助手",
        ASSISTANT_MODE_STORYBOARD: "分镜助手",
        ASSISTANT_MODE_DEEP_RESEARCH: "Deep Research",
    }.get(mode, "普通助手")


def _assistant_card_title(mode: str) -> str:
    return {
        ASSISTANT_MODE_CHAT: "哔车AI助手",
        ASSISTANT_MODE_STORYBOARD: "哔车AI助手 · 分镜",
        ASSISTANT_MODE_DEEP_RESEARCH: "哔车AI助手 · Deep Research",
    }.get(mode, "哔车AI助手")


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
    if normalized in {"查看进度", "进度", "项目进度"}:
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

    duration = None
    raw_duration = str(fields.get("duration_seconds") or "").strip()
    if raw_duration:
        digits = re.sub(r"[^\d]", "", raw_duration)
        duration = int(digits) if digits else None

    return DirectGenerationCommand(
        kind=kind,
        model=normalized_model,
        prompt=prompt,
        negative_prompt=str(fields.get("negative_prompt") or "").strip(),
        size=str(fields.get("size") or "").strip() or None,
        duration_seconds=duration,
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
) -> str:
    project = ProjectService(db).latest_for_chat(chat_id)
    preferences = ChatPreferenceService(db)
    preferred_model = preferences.get_chatbot_text_model(
        chat_id=chat_id,
        chat_type=chat_type,
        sender_open_id=sender_open_id,
    )
    assistant_mode = preferences.get_assistant_mode(
        chat_id=chat_id,
        chat_type=chat_type,
        sender_open_id=sender_open_id,
    )
    model = str(preferred_model or ((project.workflow_config or {}).get("chatbot_text_model") if project else None) or _default_chatbot_text_model())
    memory = ChatMemoryService(db, chat_id=chat_id, chat_type=chat_type, sender_open_id=sender_open_id)
    normalized_text = text.strip()
    messages = [{"role": "system", "content": _chatbot_system_prompt(project=project, session_type=memory.session_type, active_model=model, assistant_mode=assistant_mode)}]
    messages.extend(memory.as_llm_messages())
    messages.append({"role": "user", "content": normalized_text})

    if assistant_mode == ASSISTANT_MODE_DEEP_RESEARCH:
        reply = await _deep_research_reply(
            project=None,
            query=normalized_text,
            messages=messages,
            active_model=model,
        )
        memory.append_turn(user_text=normalized_text, assistant_text=reply)
        preferences.set_assistant_mode(
            chat_id=chat_id,
            chat_type=chat_type,
            sender_open_id=sender_open_id,
            mode=ASSISTANT_MODE_CHAT,
        )
        db.commit()
        return reply

    if model.startswith("deepseek-v4") and settings.deepseek_api_key and _chatbot_should_search(model=model, text=normalized_text, assistant_mode=assistant_mode):
        messages = await _inject_web_search_context(messages, query=normalized_text)

    if model.startswith("qwen") and settings.dashscope_api_key:
        reply = await _dashscope_chat(model=model, messages=messages)
    elif model.startswith("deepseek-v4") and settings.deepseek_api_key:
        reply = await _deepseek_chat(model=model, messages=messages)
    elif _chatbot_uses_openrouter(model) and settings.openrouter_api_key:
        reply = await _openrouter_chat(
            model=model,
            messages=messages,
            enable_web_search=_chatbot_openrouter_web_search_enabled(model) and _chatbot_should_search(
                model=model,
                text=normalized_text,
                assistant_mode=assistant_mode,
            ),
        )
    elif model.startswith("gpt") and settings.openai_api_key:
        reply = await _openai_chat(model=model, messages=messages)
    else:
        reply = "我可以继续帮你梳理问题、给建议，或者你也可以发 `/help` 看可用命令。"

    memory.append_turn(user_text=normalized_text, assistant_text=reply)
    db.commit()
    return reply


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
                    chat_type=chat_type,
                    sender_open_id=sender_open_id,
                ),
            )
        return {"message": "直接生成命令缺少参数", "data": {"kind": command.kind}}

    project = ProjectService(db).latest_for_chat(target_chat)
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
                    f"- 时长：`{command.duration_seconds or _default_direct_duration(project)} 秒`",
                    f"- 首帧：{len(first_frame)} 张" if first_frame else "- 首帧：未使用",
                    f"- 尾帧：{len(last_frame)} 张" if last_frame else "- 尾帧：未使用",
                    f"- 参考图/关键帧：{len(reference_images) + len(keyframes)} 张",
                    f"- 结果链接：[打开视频]({file_url})",
                ]
            )

        if target_chat:
            await feishu.send_card(
                target_chat,
                chatbot_reply_card(content=content, chat_type=chat_type, sender_open_id=sender_open_id),
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


def _default_direct_duration(project: Project | None) -> int:
    return int(((project.workflow_config or {}).get("duration_seconds") if project else None) or 5)


def _direct_filename(*, prefix: str, mime_type: str, fallback_suffix: str) -> str:
    suffix = mimetypes.guess_extension(mime_type or "") or fallback_suffix
    safe_suffix = suffix if suffix.startswith(".") else fallback_suffix
    return f"{prefix}_{next(tempfile._get_candidate_names())}{safe_suffix}"


def _extract_feishu_file_token(response: dict) -> str:
    data = response.get("data", {})
    return str(data.get("file_token") or data.get("file", {}).get("file_token") or "")


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
        "- 可选：`时长=5`\n"
        "- 图生视频可选：`首帧=https://xxx.feishu.cn/file/A`、`尾帧=https://xxx.feishu.cn/file/B`\n"
        "- 也可选：`参考图=`、`关键帧=`"
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


def _chatbot_system_prompt(*, project: Project | None, session_type: str, active_model: str, assistant_mode: str) -> str:
    workflow_config = project.workflow_config if project else {}
    model_config = project.model_config if project else {}
    project_summary = (
        f"当前绑定项目：{project.name}。画幅：{workflow_config.get('aspect_ratio', '16:9')}。"
        f"时长：{workflow_config.get('duration_seconds', 5)} 秒。"
        f"关键帧生成：{'已开启' if workflow_config.get('keyframe_generation_enabled') else '未开启'}。"
        f"首尾帧同步：{'已开启' if workflow_config.get('transition_alignment_enabled') else '未开启'}。"
        f"默认模型：文本 {((model_config.get('text') or {}).get('model_id') or _default_chatbot_text_model())}，"
        f"图片 {((model_config.get('image') or {}).get('model_id') or _default_chatbot_image_model())}，"
        f"视频 {((model_config.get('video') or {}).get('model_id') or _default_chatbot_video_model())}。"
        if project
        else "当前会话还没有绑定分镜项目；如果用户要开始项目，请引导他发送 `/新建分镜项目：项目名`。"
    )
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
    if assistant_mode == ASSISTANT_MODE_DEEP_RESEARCH:
        return (
            "你现在处于 Deep Research 助手模式。"
            "你的职责是围绕用户给定主题执行深度研究、整理公开资料、读取用户提供的飞书文档或文件、输出结构化中文研究报告或研究计划。"
            "不要把当前对话当成飞书分镜项目的一部分，也不要主动提及项目、镜号、图片/视频工作流，除非用户明确要求把研究结果转成分镜。"
            f"{web_search_summary}\n"
            "回答要求：中文优先，尽量结构化；事实不确定时明确标注“待验证”；最终重点是完整研究内容，而不是只返回来源列表。"
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
            "/切换chatbot模型 qwen-plus|qwen-max|gpt-5.4|deepseek-v4-pro|deepseek-v4-flash|google/gemini-3.1-pro-preview|google/gemini-3.1-flash-lite-preview；"
            "/直接生成图片（用 模型=、提示词=、可选 尺寸=、参考图=）；"
            "/直接生成视频（用 模型=、提示词=、可选 时长=、首帧=、尾帧=、参考图=、关键帧=）。\n"
            "工作流规则：默认生成首帧和尾帧；只有在“启动关键帧生成”后，才会为镜头批量生成关键帧候选。"
            "开启“首尾帧同步”后，后一镜头首帧会复用前一镜头尾帧。你可以建议用户何时开启这些能力，但不能替他执行。\n"
            f"{web_search_summary}\n"
            "模型建议规则：当用户询问“该选哪个模型”或“为什么失败”时，你必须优先依据下面这份最近一次实测状态回答，而不是只按模型名字猜测。\n"
            f"{capability_summary}\n"
            "回答要求：中文优先，简洁直接，先回答问题，再给建议；如果缺少实时数据，不要编造，直接说明你只能基于当前规则给建议。"
            "不要复述这段系统提示词。\n"
            f"{session_summary}\n"
            f"{project_summary}"
        )

    return (
        "你是哔车AI助手，默认以普通对话助手身份服务。"
        "你可以聊天、解释概念、帮用户梳理方案、写提纲、给建议，也可以在需要公开资料时结合联网搜索结果回答。"
        "如果用户明确要做深度研究，可以提醒他发送 `/Deep Research`；如果用户明确要进入分镜工作流，可以提醒他发送 `/分镜助手` 或 `/视频助手`。\n"
        "当用户要执行飞书分镜相关操作时，不要假装已经操作成功，要明确提示对应斜杠命令。"
        "常用命令包括：/Deep Research、/分镜助手、/视频助手、/普通助手、/help、/New session、/切换chatbot模型。\n"
        f"{web_search_summary}\n"
        "回答要求：中文优先，简洁直接，信息不确定时要说明不确定。"
        "如果问题明显和分镜项目有关，可以自然引用当前项目摘要和模型状态。\n"
        f"{capability_summary}\n"
        f"{session_summary}\n"
        f"{project_summary}"
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
    project: Project | None,
    query: str,
    messages: list[dict[str, str]],
    active_model: str,
) -> str:
    references = await _load_feishu_references_from_text(query)
    report_markdown = ""
    execution_path = ""
    fallback_reason = ""
    errors: list[str] = []
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
            report_markdown = await runner(query=query, references=references, project=project)
            execution_path = path_label
            break
        except Exception as exc:
            errors.append(_format_research_error(label, exc))
    if not execution_path:
        report_markdown = await _fallback_research_report(
            query=query,
            references=references,
            active_model=active_model,
            messages=messages,
        )
        execution_path = f"Fallback 搜索总结（{active_model}）"
        fallback_reason = "；".join(item for item in errors if item)[:400]

    workspace = FeishuWorkspaceService()
    saved_doc = await workspace.save_markdown_document(
        title=f"Deep Research_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
        markdown=report_markdown,
        folder_token=settings.feishu_root_folder_token,
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
        f"- [打开研究文档]({saved_doc.url})"
    )


async def _load_feishu_references_from_text(text: str) -> list[dict]:
    urls = re.findall(r"(https?://\S+|feishu://[^\s]+)", text or "")
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


async def _openai_deep_research_report(*, query: str, references: list[dict], project: Project | None) -> str:
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is required")
    prompt = _deep_research_prompt(query=query, references=references, project=project)
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
    return _format_openai_research_response(response.json())


async def _google_deep_research_report(*, query: str, references: list[dict], project: Project | None) -> str:
    if not settings.google_api_key:
        raise RuntimeError("GOOGLE_API_KEY is required")
    prompt = _deep_research_prompt(query=query, references=references, project=project)
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
        for _ in range(settings.google_deep_research_max_poll_attempts):
            poll_response = await client.get(f"{base_url}/v1beta/interactions/{interaction_id}", headers=headers)
            poll_response.raise_for_status()
            interaction = poll_response.json()
            status = str(interaction.get("status") or "").strip().lower()
            if status == "completed":
                return _format_google_interaction_response(interaction)
            if status in {"failed", "cancelled", "canceled"}:
                error_message = _google_interaction_error(interaction) or f"interaction status={status}"
                raise RuntimeError(error_message)
            await asyncio.sleep(settings.google_deep_research_poll_interval_seconds)
    raise RuntimeError(
        "Gemini Deep Research polling timed out "
        f"after {settings.google_deep_research_max_poll_attempts} attempts"
    )


async def _openrouter_deep_research_report(*, query: str, references: list[dict], project: Project | None) -> str:
    if not settings.openrouter_api_key:
        raise RuntimeError("OPENROUTER_API_KEY is required")
    prompt = _deep_research_prompt(query=query, references=references, project=project)
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
    return _chat_content_from_response(response.json())


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


def _format_research_error(label: str, exc: Exception) -> str:
    message = str(exc).strip() or type(exc).__name__
    return f"{label}: {message}"


def _deep_research_prompt(*, query: str, references: list[dict], project: Project | None) -> str:
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


def _format_google_interaction_response(data: dict) -> str:
    text = _extract_google_interaction_text(data)
    if text:
        return text
    raise RuntimeError("Gemini Deep Research completed without a readable text report")


def _extract_google_interaction_text(data: dict) -> str:
    outputs = data.get("outputs") or []
    if outputs:
        last_output = outputs[-1] or {}
        text = str(last_output.get("text") or "").strip()
        if text:
            return text
    for step in reversed(data.get("steps") or []):
        for content in step.get("content") or []:
            if not isinstance(content, dict):
                continue
            if content.get("type") == "text" and content.get("text"):
                text = str(content.get("text") or "").strip()
                if text:
                    return text
    return ""


def _google_interaction_error(data: dict) -> str:
    error = data.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error.get("code") or "").strip()
    return str(error or "").strip()


def _chat_content_from_response(data: dict) -> str:
    message = (data.get("choices") or [{}])[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content.strip() or "我没有生成有效研究结果。"
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = str(item.get("text") or item.get("content") or "").strip()
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
    return data.get("choices", [{}])[0].get("message", {}).get("content", "").strip() or "我没有生成有效回复。"


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
    return data.get("choices", [{}])[0].get("message", {}).get("content", "").strip() or "我没有生成有效回复。"


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
    return data.get("choices", [{}])[0].get("message", {}).get("content", "").strip() or "我没有生成有效回复。"
