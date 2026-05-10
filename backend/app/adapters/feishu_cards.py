from __future__ import annotations

import json
import re


def project_overview_card(*, project_name: str, table_url: str | None, stats: dict) -> dict:
    return {
        "config": {"wide_screen_mode": True},
        "elements": [
            {"tag": "markdown", "content": f"**{project_name}**\nAI 分镜项目已就绪。"},
            {
                "tag": "div",
                "text": {
                    "tag": "plain_text",
                    "content": f"待生成 {stats.get('pending', 0)} / 待审核 {stats.get('review', 0)} / 已通过 {stats.get('approved', 0)}",
                },
            },
            {
                "tag": "action",
                "actions": [
                    {"tag": "button", "text": {"tag": "plain_text", "content": "打开分镜表"}, "url": table_url or ""},
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "同步表格"},
                        "type": "default",
                        "value": {"action": "project.sync"},
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "生成全部图片"},
                        "type": "primary",
                        "value": {"action": "project.generate_all_images"},
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "查看进度"},
                        "type": "default",
                        "value": {"action": "project.progress"},
                    },
                ],
            },
        ],
        "header": {"title": {"tag": "plain_text", "content": "哔车 AI 分镜"}, "template": "orange"},
    }


def chatbot_reply_card(*, content: str, title: str = "哔车AI助手", chat_type: str | None = None, sender_open_id: str | None = None) -> dict:
    markdown = render_feishu_markdown(content)
    quick_action_value = {
        "chat_type": chat_type,
        "sender_open_id": sender_open_id,
    }
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": title}, "template": "wathet"},
        "elements": [
            {"tag": "markdown", "content": markdown},
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "/New Session"},
                        "type": "default",
                        "value": {**quick_action_value, "action": "assistant.clear_session"},
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "/视频助手"},
                        "type": "default",
                        "value": {**quick_action_value, "action": "assistant.set_mode", "mode": "storyboard"},
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "/Deep Research"},
                        "type": "primary",
                        "value": {**quick_action_value, "action": "assistant.set_mode", "mode": "deep_research"},
                    },
                ],
            },
        ],
    }


def render_feishu_markdown(content: str) -> str:
    text = (content or "").strip() or "我暂时没有生成有效回复。"
    text = _downgrade_unsupported_headings(text)
    return _convert_markdown_tables(text)


def _downgrade_unsupported_headings(text: str) -> str:
    lines = []
    for line in text.splitlines():
        match = re.match(r"^(#{3,6})\s+(.+?)\s*$", line)
        if match:
            lines.append(f"**{match.group(2).strip()}**")
        else:
            lines.append(line)
    return "\n".join(lines)


def _convert_markdown_tables(text: str) -> str:
    lines = text.splitlines()
    converted: list[str] = []
    index = 0
    table_count = 0
    in_code_block = False

    while index < len(lines):
        line = lines[index]
        if line.strip().startswith("```"):
            in_code_block = not in_code_block
            converted.append(line)
            index += 1
            continue

        if (
            not in_code_block
            and index + 1 < len(lines)
            and _looks_like_table_row(line)
            and _looks_like_table_separator(lines[index + 1])
        ):
            start = index
            index += 2
            table_lines = [line]
            while index < len(lines) and _looks_like_table_row(lines[index]):
                table_lines.append(lines[index])
                index += 1
            table_count += 1
            converted.append(_table_block_to_feishu(table_lines, table_count=table_count))
            if index < len(lines) and lines[index].strip() == "":
                converted.append("")
                index += 1
            continue

        converted.append(line)
        index += 1

    return "\n".join(converted).strip()


def _looks_like_table_row(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("|") and stripped.endswith("|") and stripped.count("|") >= 2


def _looks_like_table_separator(line: str) -> bool:
    stripped = line.strip()
    if not (stripped.startswith("|") and stripped.endswith("|")):
        return False
    cells = _split_table_row(stripped)
    if not cells:
        return False
    return all(re.fullmatch(r":?-{3,}:?", cell.strip()) for cell in cells)


def _split_table_row(line: str) -> list[str]:
    inner = line.strip().strip("|")
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", inner)]


def _table_block_to_feishu(lines: list[str], *, table_count: int) -> str:
    headers = _split_table_row(lines[0])
    rows = [_split_table_row(line) for line in lines[1:]]
    original = "\n".join(lines)

    if not headers or table_count > 5 or len(headers) > 10:
        return _table_fallback_block(headers=headers, rows=rows, original=original)

    normalized_rows: list[dict[str, str]] = []
    for raw_row in rows:
        if len(raw_row) < len(headers):
            raw_row = raw_row + [""] * (len(headers) - len(raw_row))
        elif len(raw_row) > len(headers):
            raw_row = raw_row[: len(headers)]
        if all(re.fullmatch(r":?-{3,}:?", cell.strip()) for cell in raw_row):
            continue
        normalized_rows.append({f"col_{idx}": cell for idx, cell in enumerate(raw_row)})

    columns = [{"title": title or f"列{idx + 1}", "dataIndex": f"col_{idx}"} for idx, title in enumerate(headers)]
    return (
        f"<table columns={{{json.dumps(columns, ensure_ascii=False)}}} "
        f"data={{{json.dumps(normalized_rows, ensure_ascii=False)}}}/>"
    )


def _table_fallback_block(*, headers: list[str], rows: list[list[str]], original: str) -> str:
    if not headers:
        return f"```text\n{original}\n```"
    body = []
    data_rows = [row for row in rows if not all(re.fullmatch(r":?-{3,}:?", cell.strip()) for cell in row)]
    for row_index, row in enumerate(data_rows, start=1):
        values = row + [""] * max(len(headers) - len(row), 0)
        body.append(f"**第 {row_index} 行**")
        for header, value in zip(headers, values):
            body.append(f"- `{header or '列'}`：{value}")
    return "\n".join(body) if body else f"```text\n{original}\n```"


def help_card() -> dict:
    content = "\n".join(
        [
            "**常用命令**",
            "",
            "- 直接聊天：不加 `/` 时，机器人会按普通助手回复",
            "- `/普通助手`：把当前会话切回普通对话助手",
            "- `/分镜助手` / `/视频助手`：把当前会话切到分镜工作流助手",
            "- `/Deep Research`：把当前会话切到深度研究模式，后续会联网检索并把研究结果保存为飞书文档",
            "- `/help` / `/帮助` / `/菜单`：查看这张说明卡片",
            "- `/New session`：重置当前群聊或私聊会话的聊天记录，不影响项目和模型设置",
            "- `/新建分镜项目：项目名`：创建分镜表和项目文件夹",
            "- `/新建分镜项目：项目名 https://xxx.feishu.cn/drive/folder/FILE_TOKEN`：在指定飞书文件夹下创建项目",
            "- `/新建项目：项目名` / `/新建：项目名` / `/new：项目名`：同样创建项目",
            "- `/新建 AI 分镜项目`：创建一个未命名项目，后续再改名",
            "- `/优化当前批次 Prompt`：优化最近项目的 batch_001",
            "- `/生成全部图片`：为最近项目所有分镜生成首帧和尾帧；关键帧需先启动关键帧生成",
            "- `/生成全部视频`：为最近项目生成视频；有首尾帧/参考图就使用，没有图片就按文字生成",
            "- `/生成全部图片和视频`：连续执行图片和视频生成",
            "- `/启动首尾帧同步` / `/关闭首尾帧同步`：统一切换所有行的 `首帧同步设置`，以最后一次操作为准",
            "- `/启动关键帧生成` / `/关闭关键帧生成`：统一切换所有行的 `关键帧生成设置`，以最后一次操作为准",
            "- `/切换chatbot模型 qwen-plus`：切换普通对话模型，可选 qwen-plus / qwen-max / gpt-5.4 / deepseek-v4-pro / deepseek-v4-flash / google/gemini-3.1-pro-preview / google/gemini-3.1-flash-lite-preview",
            "- 如果切到 OpenRouter 的 Gemini 聊天模型，chatbot 现在会按需调用联网搜索，适合问最新公开资料、新闻、公司动态和市场信息",
            "- 如果切到 DeepSeek 聊天模型，系统也会在需要时先做公开网页搜索，再把结果提供给模型回答",
            "- `/直接生成图片`：按命令直接调用图片模型；必填 `模型=`、`提示词=`，可选 `尺寸=`、`参考图=`",
            "- `/直接生成视频`：按命令直接调用视频模型；必填 `模型=`、`提示词=`，可选 `时长=`、`首帧=`、`尾帧=`、`参考图=`、`关键帧=`",
            "- `/同步表格`：补齐默认值并同步分镜行",
            "- `/查看进度`：查看最近项目进度",
            "- Deep Research 模式下如果消息里附带飞书文档/文件链接，系统会读取文档内容或文本文件内容，再结合联网结果写研究报告",
            "",
            "**直接生成示例**",
            "",
            "- `/直接生成图片`",
            "- `模型=nanobanana`",
            "- `提示词=夕阳下的海边咖啡馆，暖光，电影感`",
            "- `/直接生成图片`",
            "- `模型=nanobanana`",
            "- `提示词=把这张图改成卡通海报风`",
            "- `参考图=https://xxx.feishu.cn/file/FILE_TOKEN`",
            "- `/直接生成视频`",
            "- `模型=小云雀`",
            "- `提示词=镜头缓慢推近，蒸汽自然上升`",
            "- `首帧=https://xxx.feishu.cn/file/A`",
            "- `尾帧=https://xxx.feishu.cn/file/B`",
            "",
            "**项目创建后常用按钮**",
            "",
            "- 打开分镜表",
            "- 优化当前批次 Prompt",
            "- 生成全部图片",
            "- 生成全部视频",
            "- 生成全部图片和视频",
            "- 开启/关闭首尾帧同步",
            "- 开启/关闭关键帧生成",
            "- 查看进度",
            "",
            "**表格里最常用的状态**",
            "",
            "- 看图满意，把 `审核状态` 改成 `通过`",
            "- 看图不满意，把 `审核状态` 改成 `驳回`，并填写 `驳回原因`",
            "- 想重生成单条图片，把 `图片生成状态` 改成 `启动`",
            "- 想生成或重生成单条视频，把 `生成状态` 改成 `启动`",
            "- 想按驳回原因重做，选择 `需要重新生成的选项` 后把 `重新生成状态` 改成 `启动`",
            "- 看完视频后，在 `满意度` 里选 `满意` 或 `不满意`",
            "",
            "**最简流程**",
            "",
            "新建项目 → 填分镜表 → 生成全部图片 → 审核图片 → 启动视频生成 → 看视频 → 标满意度",
        ]
    )
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": "AI 分镜机器人帮助"}, "template": "blue"},
        "elements": [{"tag": "markdown", "content": content}],
    }


def project_created_card(
    *,
    project_id: str,
    project_name: str,
    table_url: str | None,
    folder_url: str | None = None,
    transition_alignment_state: str = "未启动",
    keyframe_generation_state: str = "未启动",
) -> dict:
    content = "\n".join(
        [
            f"项目已创建：**{project_name}**",
            "",
            "已创建：",
            "- 分镜表",
            "- 参考图文件夹",
            "- 帧图文件夹",
            "- 视频文件夹",
            "- 满意归档文件夹",
            "- 不满意归档文件夹",
        ]
    )
    actions = [
        {"tag": "button", "text": {"tag": "plain_text", "content": "打开分镜表"}, "url": table_url or ""},
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "优化当前批次 Prompt"},
            "type": "default",
            "value": {"action": "batch.optimize_prompt", "project_id": project_id, "batch_no": "batch_001"},
        },
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "生成全部图片"},
            "type": "primary",
            "value": {"action": "project.generate_all_images", "project_id": project_id},
        },
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "生成全部视频"},
            "type": "primary",
            "value": {"action": "project.generate_all_videos", "project_id": project_id},
        },
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "生成全部图片和视频"},
            "type": "primary",
            "value": {"action": "project.generate_all_media", "project_id": project_id},
        },
        {
            "tag": "button",
            "text": {
                "tag": "plain_text",
                "content": "关闭首尾帧同步" if transition_alignment_state == "已启动" else "开启首尾帧同步",
            },
            "type": "default",
            "value": {
                "action": "project.set_transition_alignment",
                "project_id": project_id,
                "enabled": transition_alignment_state != "已启动",
            },
        },
        {
            "tag": "button",
            "text": {
                "tag": "plain_text",
                "content": "关闭关键帧生成" if keyframe_generation_state == "已启动" else "开启关键帧生成",
            },
            "type": "default",
            "value": {
                "action": "project.set_keyframes",
                "project_id": project_id,
                "enabled": keyframe_generation_state != "已启动",
            },
        },
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "查看进度"},
            "type": "default",
            "value": {"action": "project.progress", "project_id": project_id},
        },
    ]
    if folder_url:
        actions.append({"tag": "button", "text": {"tag": "plain_text", "content": "查看归档文件夹"}, "url": folder_url})
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": "项目已创建"}, "template": "green"},
        "elements": [{"tag": "markdown", "content": content}, *_action_blocks(actions)],
    }


def batch_done_card(*, project_id: str, batch_no: str, rows: list[tuple[str, str]]) -> dict:
    status_lines = "\n".join(f"- 镜头 {shot_no}：{status}" for shot_no, status in rows)
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": f"{batch_no} 帧图生成完成"}, "template": "green"},
        "elements": [
            {"tag": "markdown", "content": f"{status_lines}\n\n请进入分镜表审核首帧、尾帧和关键帧。"},
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "打开待审核视图"},
                        "type": "primary",
                        "value": {"action": "project.progress", "project_id": project_id},
                    }
                ],
            },
        ],
    }


def progress_card(*, project_name: str, stats: dict, table_url: str | None = None, project_id: str | None = None) -> dict:
    content = "\n".join(
        [
            f"**{project_name}**",
            "",
            "当前进度：",
            f"- 镜头总数：{stats.get('total', 0)}",
            f"- Prompt：{stats.get('prompt_optimized', 0)} 已优化 / {stats.get('prompt_pending', 0)} 待优化",
            f"- 图片：首帧 {stats.get('first_frames', 0)} / 尾帧 {stats.get('last_frames', 0)} / 关键帧 {stats.get('keyframe_shots', 0)}",
            f"- 图片生成：{stats.get('image_generating', 0)} 正在生成 / {stats.get('image_done', 0)} 生成完成",
            f"- 视频：{stats.get('videos', 0)} 已生成",
            f"- 视频生成：{stats.get('video_generating', 0)} 正在生成 / {stats.get('video_done', 0)} 生成完成",
            f"- 首尾帧同步：{stats.get('transition_alignment_state') or ('已启动' if stats.get('transition_alignment_enabled') else '未启动')}",
            f"- 关键帧生成：{stats.get('keyframe_generation_state') or ('已启动' if stats.get('keyframe_generation_enabled') else '未启动')}",
            f"- 待生成：{stats.get('pending_frames', 0)}",
            f"- 帧生成中：{stats.get('frames_generating', 0)}",
            f"- 待审核：{stats.get('pending_review', 0)}",
            f"- 视频生成中：{stats.get('video_generating', 0)}",
            f"- 待验收：{stats.get('pending_acceptance', 0)}",
            f"- 已归档：{stats.get('archived', 0)}",
            f"- 错误：{stats.get('errors', 0)}",
        ]
    )
    if stats.get("error_items"):
        content += "\n\n最近错误：\n" + "\n".join(
            f"- 镜头 {item.get('shot_no')}：{item.get('code') or 'ERROR'} {item.get('message') or ''}" for item in stats["error_items"]
        )
    if stats.get("errors"):
        content += "\n\n日志位置：\n" + "\n".join(f"- `{path}`" for path in stats.get("log_paths", []))
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": "项目进度"}, "template": "blue"},
        "elements": [
            {"tag": "markdown", "content": content},
            {
                "tag": "action",
                "actions": [
                    {"tag": "button", "text": {"tag": "plain_text", "content": "打开分镜表"}, "url": table_url or ""},
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "生成全部图片"},
                        "type": "primary",
                        "value": {"action": "project.generate_all_images", "project_id": project_id},
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "生成全部视频"},
                        "type": "primary",
                        "value": {"action": "project.generate_all_videos", "project_id": project_id},
                    },
                    {
                        "tag": "button",
                        "text": {
                            "tag": "plain_text",
                            "content": "关闭首尾帧同步"
                            if stats.get("transition_alignment_state") == "已启动"
                            else "开启首尾帧同步",
                        },
                        "type": "default",
                        "value": {
                            "action": "project.set_transition_alignment",
                            "project_id": project_id,
                            "enabled": stats.get("transition_alignment_state") != "已启动",
                        },
                    },
                    {
                        "tag": "button",
                        "text": {
                            "tag": "plain_text",
                            "content": "关闭关键帧生成"
                            if stats.get("keyframe_generation_state") == "已启动"
                            else "开启关键帧生成",
                        },
                        "type": "default",
                        "value": {
                            "action": "project.set_keyframes",
                            "project_id": project_id,
                            "enabled": stats.get("keyframe_generation_state") != "已启动",
                        },
                    },
                ],
            },
        ],
    }


def failure_card(*, title: str, error_code: str, message: str, retry_value: dict | None = None) -> dict:
    actions = []
    if retry_value:
        actions.append({"tag": "button", "text": {"tag": "plain_text", "content": "重试"}, "value": retry_value})
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": title}, "template": "red"},
        "elements": [
            {"tag": "markdown", "content": f"**错误码**：{error_code}\n\n{message}"},
            {"tag": "action", "actions": actions} if actions else {"tag": "markdown", "content": "请修改配置或 Prompt 后重试。"},
        ],
    }


def _action_blocks(actions: list[dict], size: int = 4) -> list[dict]:
    return [{"tag": "action", "actions": actions[index : index + size]} for index in range(0, len(actions), size)]
