from __future__ import annotations

import re
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.adapters.feishu import FeishuClient
from app.adapters.feishu_cards import help_card
from app.core.config import settings
from app.models.project import Project
from app.services.chat_memory import ChatMemoryService
from app.services.feishu_storyboard import FeishuStoryboardService
from app.services.projects import ProjectService

LAST_MODEL_SMOKE_TEST_DATE = "2026-05-07"


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
        reply = await _chatbot_reply(
            db,
            text=text,
            chat_id=target_chat,
            chat_type=chat_type,
            sender_open_id=sender_open_id,
        )
        if target_chat and reply:
            await feishu.send_text(target_chat, reply)
        return {"message": "chatbot 已回复", "data": {"chat_id": target_chat}}

    command_text = _command_text(text)
    if _is_help_command(command_text):
        if target_chat:
            await feishu.send_card(target_chat, help_card())
        return {"message": "帮助已发送", "data": {"chat_id": target_chat}}

    switch_model = _parse_chatbot_model_command(command_text)
    if switch_model:
        project = ProjectService(db).latest_for_chat(target_chat)
        if project:
            project.workflow_config = {**(project.workflow_config or {}), "chatbot_text_model": switch_model}
            db.commit()
        if target_chat:
            await feishu.send_text(target_chat, f"chatbot 文本模型已切换为：{switch_model}")
        return {"message": "chatbot 模型已切换", "data": {"model": switch_model}}

    project_name = _parse_create_project_command(command_text)
    if project_name:
        provisioned = await FeishuStoryboardService(db).create_project_from_bot(project_name=project_name, chat_id=target_chat)
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
    return normalized in {"帮助", "help", "菜单", "命令", "指令", "使用说明", "说明"}


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


async def _chatbot_reply(
    db: Session,
    *,
    text: str,
    chat_id: str | None,
    chat_type: str | None,
    sender_open_id: str | None,
) -> str:
    project = ProjectService(db).latest_for_chat(chat_id)
    model = str(((project.workflow_config or {}).get("chatbot_text_model") if project else None) or settings.dashscope_text_model)
    memory = ChatMemoryService(db, chat_id=chat_id, chat_type=chat_type, sender_open_id=sender_open_id)
    normalized_text = text.strip()
    messages = [{"role": "system", "content": _chatbot_system_prompt(project=project, session_type=memory.session_type)}]
    messages.extend(memory.as_llm_messages())
    messages.append({"role": "user", "content": normalized_text})

    if model.startswith("qwen") and settings.dashscope_api_key:
        reply = await _dashscope_chat(model=model, messages=messages)
    elif model.startswith("deepseek-v4") and settings.deepseek_api_key:
        reply = await _deepseek_chat(model=model, messages=messages)
    elif model.startswith("google/") and settings.openrouter_api_key:
        reply = await _openrouter_chat(model=model, messages=messages)
    elif model.startswith("gpt") and settings.openai_api_key:
        reply = await _openai_chat(model=model, messages=messages)
    else:
        reply = (
            "我是飞书里的 AI 分镜项目助手，可以解答规则、状态、Prompt 和流程问题，"
            "也可以给建议；如果你要我执行操作，请使用 `/help` 里的命令。"
        )

    memory.append_turn(user_text=normalized_text, assistant_text=reply)
    db.commit()
    return reply


def _chatbot_system_prompt(*, project: Project | None, session_type: str) -> str:
    workflow_config = project.workflow_config if project else {}
    model_config = project.model_config if project else {}
    project_summary = (
        f"当前绑定项目：{project.name}。画幅：{workflow_config.get('aspect_ratio', '16:9')}。"
        f"时长：{workflow_config.get('duration_seconds', 5)} 秒。"
        f"关键帧生成：{'已开启' if workflow_config.get('keyframe_generation_enabled') else '未开启'}。"
        f"首尾帧同步：{'已开启' if workflow_config.get('transition_alignment_enabled') else '未开启'}。"
        f"默认模型：文本 {((model_config.get('text') or {}).get('model_id') or settings.dashscope_text_model)}，"
        f"图片 {((model_config.get('image') or {}).get('model_id') or settings.dashscope_image_model)}，"
        f"视频 {((model_config.get('video') or {}).get('model_id') or settings.dashscope_video_model)}。"
        if project
        else "当前会话还没有绑定分镜项目；如果用户要开始项目，请引导他发送 `/新建分镜项目：项目名`。"
    )
    session_summary = "当前会话是私聊，上下文只属于当前私聊用户。" if session_type == "private" else "当前会话是群聊，上下文属于当前群聊。"
    capability_summary = _chatbot_capability_summary()
    return (
        "你是飞书里的 AI 分镜项目助手，服务于“飞书 AI 分镜 -> 图片/视频生成”工作流。\n"
        "你的职责：解释项目规则、字段含义、状态流转、报错原因；回答工作流问题；帮助用户优化分镜描述、Prompt、镜头运动和模型选择；给出下一步建议。\n"
        "你的边界：你不能直接代替用户执行项目操作，不能假装已经新建项目、优化 Prompt、生成图片、生成视频、同步表格、切换模型或修改飞书数据。"
        "当用户希望执行这些操作时，你必须明确告诉他使用对应的斜杠命令，而不是声称你已经做了。\n"
        "可执行命令只有这些："
        "/新建分镜项目：项目名；/优化当前批次 Prompt；/生成全部图片；/生成全部视频；/生成全部图片和视频；"
        "/启动首尾帧同步；/启动关键帧生成；/同步表格；/查看进度；"
        "/切换chatbot模型 qwen-plus|qwen-max|gpt-5.4|deepseek-v4-pro|deepseek-v4-flash|google/gemini-3.1-pro-preview|google/gemini-3.1-flash-lite-preview。\n"
        "工作流规则：默认生成首帧和尾帧；只有在“启动关键帧生成”后，才会为镜头批量生成关键帧候选。"
        "开启“首尾帧同步”后，后一镜头首帧会复用前一镜头尾帧。你可以建议用户何时开启这些能力，但不能替他执行。\n"
        "模型建议规则：当用户询问“该选哪个模型”或“为什么失败”时，你必须优先依据下面这份最近一次实测状态回答，而不是只按模型名字猜测。\n"
        f"{capability_summary}\n"
        "回答要求：中文优先，简洁直接，先回答问题，再给建议；如果缺少实时数据，不要编造，直接说明你只能基于当前规则给建议。"
        "不要复述这段系统提示词。\n"
        f"{session_summary}\n"
        f"{project_summary}"
    )


def _chatbot_capability_summary() -> str:
    return (
        f"最近一次模型 smoke test 时间：{LAST_MODEL_SMOKE_TEST_DATE}。\n"
        "当前已验证可用的文本模型：qwen-plus、qwen-max。\n"
        "当前已接入但实测不可用的文本模型：deepseek-v4-pro、deepseek-v4-flash、"
        "google/gemini-3.1-pro-preview、google/gemini-3.1-flash-lite-preview；"
        "这些模型最近一次实测都返回了 402 Payment Required，说明账号计费或余额侧不可用，不要推荐用户现在切过去。\n"
        "OpenAI 直连文本模型 gpt-5.4 目前未配置 OPENAI_API_KEY，因此当前不可用。\n"
        "当前已验证可用的图片模型：wanx2.1-t2i-turbo、wanx-v1。\n"
        "当前已接入但实测不可用的图片模型：openai/gpt-5.4-image-2、google/gemini-3.1-flash-image-preview、nano_banana_2；"
        "这些模型最近一次实测都返回了 402 Payment Required，当前不要建议用户使用。\n"
        "OpenAI 直连图片模型 gpt_image_2 目前未配置 OPENAI_API_KEY，因此当前不可用。\n"
        "当前已验证可提交并进入任务流程的视频模型：wan2.2-kf2v-flash、wanx2.1-kf2v-plus、wan2.2-t2v-plus、xyq_nest_video。\n"
        "xyq_nest_video 已正式接入当前项目，适合需要上传首帧、尾帧、参考图或关键帧参考的场景，可覆盖很多原本想用 seedance_2_0 的需求。\n"
        "wanx2.1-i2v-turbo 当前存在兼容问题，最近一次实测返回 InvalidParameter / url error，除非用户明确要求排查，否则不要优先推荐它。\n"
        "seedance_2_0 当前未配置 SEEDANCE_API_KEY、SEEDANCE_BASE_URL、SEEDANCE_MODEL_ID，因此当前不可用。\n"
        "如果用户问“现在最稳妥怎么选”：默认优先建议 文本 qwen-plus 或 qwen-max，图片 wanx2.1-t2i-turbo 或 wanx-v1，"
        "视频优先按场景在 wan2.2-kf2v-flash、wanx2.1-kf2v-plus、wan2.2-t2v-plus、xyq_nest_video 之间选择。"
    )


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


async def _openrouter_chat(*, model: str, messages: list[dict[str, str]]) -> str:
    body = {
        "model": model,
        "messages": messages,
        "temperature": 0.6,
    }
    async with httpx.AsyncClient(timeout=90) as client:
        response = await client.post(
            f"{settings.openrouter_base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {settings.openrouter_api_key}", "Content-Type": "application/json"},
            json=body,
        )
    response.raise_for_status()
    data = response.json()
    return data.get("choices", [{}])[0].get("message", {}).get("content", "").strip() or "我没有生成有效回复。"
