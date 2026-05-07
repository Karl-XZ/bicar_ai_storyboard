from __future__ import annotations

import re
from typing import Any

from sqlalchemy.orm import Session

from app.adapters.feishu import FeishuClient
from app.adapters.feishu_cards import help_card
from app.core.config import settings
from app.services.feishu_storyboard import FeishuStoryboardService
from app.services.projects import ProjectService


async def handle_bot_text(db: Session, *, text: str, chat_id: str | None = None) -> dict[str, Any] | None:
    target_chat = chat_id or settings.feishu_default_chat_id
    feishu = FeishuClient()
    if _is_help_command(text):
        if target_chat:
            await feishu.send_card(target_chat, help_card())
        return {"message": "帮助已发送", "data": {"chat_id": target_chat}}

    project_name = _parse_create_project_command(text)
    if project_name:
        provisioned = await FeishuStoryboardService(db).create_project_from_bot(project_name=project_name, chat_id=target_chat)
        return {
            "message": "项目已创建",
            "data": {"project_id": str(provisioned.project.id), "table_url": provisioned.table_url},
        }

    command = _parse_project_command(text)
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
    service = FeishuStoryboardService(db)
    if not action or not project_id:
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

    if action == "project.enable_transition_alignment":
        synced = await service.enable_transition_alignment(project)
        return {"message": "首尾帧同步已启动", "data": {"project_id": project_id, "synced": synced}}

    if action == "project.enable_keyframes":
        await service.enable_keyframe_generation(project)
        return {"message": "关键帧生成已启动", "data": {"project_id": project_id}}

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


def _parse_create_project_command(text: str) -> str | None:
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
            return name or None
    if normalized == "新建 AI 分镜项目":
        return "未命名 AI 分镜项目"
    return None


def _is_help_command(text: str) -> bool:
    normalized = text.strip().lower()
    return normalized in {"帮助", "help", "/help", "菜单", "命令", "指令", "使用说明", "说明"}


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
        return {"action": "project.enable_transition_alignment", "batch_no": batch_no}
    if normalized in {"启动关键帧生成", "关键帧生成", "生成关键帧", "开启关键帧生成"}:
        return {"action": "project.enable_keyframes", "batch_no": batch_no}
    if normalized in {"同步表格", "同步分镜表", "同步"}:
        return {"action": "project.sync", "batch_no": batch_no}
    if normalized in {"查看进度", "进度", "项目进度"}:
        return {"action": "project.progress", "batch_no": batch_no}
    return None


def _extract_batch_no(text: str) -> str | None:
    match = re.search(r"batch[_-]?\d+", text, flags=re.IGNORECASE)
    if not match:
        return None
    return match.group(0).lower().replace("-", "_")
