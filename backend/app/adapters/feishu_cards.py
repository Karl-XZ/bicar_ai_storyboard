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


def help_card() -> dict:
    content = "\n".join(
        [
            "**常用命令**",
            "",
            "- 直接聊天：不加 `/` 时，机器人会按普通 AI 助手回复",
            "- `/help` / `/帮助` / `/菜单`：查看这张说明卡片",
            "- `/新建分镜项目：项目名`：创建分镜表和项目文件夹",
            "- `/新建项目：项目名` / `/新建：项目名` / `/new：项目名`：同样创建项目",
            "- `/新建 AI 分镜项目`：创建一个未命名项目，后续再改名",
            "- `/优化当前批次 Prompt`：优化最近项目的 batch_001",
            "- `/生成全部图片`：为最近项目所有分镜生成首帧和尾帧；关键帧需先启动关键帧生成",
            "- `/生成全部视频`：为最近项目生成视频；有首尾帧/参考图就使用，没有图片就按文字生成",
            "- `/生成全部图片和视频`：连续执行图片和视频生成",
            "- `/启动首尾帧同步`：把所有行的 `首帧同步设置` 改为 `是`",
            "- `/启动关键帧生成`：后续生成图片时同时生成关键帧候选图",
            "- `/切换chatbot模型 qwen-plus`：切换普通对话模型，可选 qwen-plus / qwen-max / gpt-5.4 / deepseek-v4-pro / deepseek-v4-flash / google/gemini-3.1-pro-preview / google/gemini-3.1-flash-lite-preview",
            "- `/同步表格`：补齐默认值并同步分镜行",
            "- `/查看进度`：查看最近项目进度",
            "",
            "**项目创建后常用按钮**",
            "",
            "- 打开分镜表",
            "- 优化当前批次 Prompt",
            "- 生成全部图片",
            "- 生成全部视频",
            "- 生成全部图片和视频",
            "- 启动首尾帧同步",
            "- 启动关键帧生成",
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
            "text": {"tag": "plain_text", "content": "启动首尾帧同步"},
            "type": "default",
            "value": {"action": "project.enable_transition_alignment", "project_id": project_id},
        },
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "启动关键帧生成"},
            "type": "default",
            "value": {"action": "project.enable_keyframes", "project_id": project_id},
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
            f"- 关键帧生成：{'已启动' if stats.get('keyframe_generation_enabled') else '未启动'}",
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
