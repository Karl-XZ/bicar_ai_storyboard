from app.adapters.feishu_cards import chatbot_reply_card, help_card, render_feishu_markdown
from app.api.routes.webhooks import _is_help_command, _message_text
from app.services.bot_commands import (
    _extract_message_urls,
    _parse_agent_runtime_command,
    _is_new_session_command,
    _parse_assistant_mode_command,
    _parse_chatbot_model_command,
    _parse_create_project_command,
    _parse_create_project_command_parts,
    _parse_direct_generation_command,
    _parse_project_command,
)


def test_help_command_aliases():
    for text in ["帮助", "help", "菜单", "命令", "指令", "使用说明", "说明"]:
        assert _is_help_command(text)


def test_help_card_contains_core_commands():
    card = help_card()
    content = card["elements"][0]["content"]
    assert "/Agent" in content
    assert "/新建分镜项目：项目名" in content
    assert "/优化当前批次 Prompt" in content
    assert "/生成全部图片" in content
    assert "/生成全部视频" in content
    assert "/切换chatbot模型 qwen-plus" in content
    assert "/New session" in content
    assert "/普通助手" in content
    assert "/分镜助手" in content
    assert "/视频助手" in content
    assert "/Deep Research" in content
    assert "/拆分镜需求" in content
    assert "/直接生成图片" in content
    assert "/直接生成视频" in content
    assert "联网搜索" in content
    assert "生成状态" in content
    assert "重新生成状态" in content
    assert "满意度" in content


def test_create_project_command_aliases():
    for text in [
        "新建分镜项目：咖啡广告",
        "新建项目：咖啡广告",
        "新建：咖啡广告",
        "new：咖啡广告",
        "new: 咖啡广告",
    ]:
        assert _parse_create_project_command(text) == "咖啡广告"


def test_create_project_command_accepts_explicit_folder_url():
    command = _parse_create_project_command_parts(
        "新建分镜项目：测试010 https://ocnwptzvwvt6.feishu.cn/drive/folder/TcAUfNw3nlk8eTdrPWxc0kK3nJe"
    )
    assert command == (
        "测试010",
        "https://ocnwptzvwvt6.feishu.cn/drive/folder/TcAUfNw3nlk8eTdrPWxc0kK3nJe",
    )


def test_message_text_strips_feishu_mentions():
    message = {
        "content": '{"text":"@_user_1 help"}',
        "mentions": [{"key": "_user_1", "name": "AI 分镜机器人"}],
    }
    assert _message_text(message) == "help"


def test_message_text_appends_file_reference_tokens():
    message = {
        "content": '{"file_key":"file_tok_123","file_name":"brief.docx"}',
        "mentions": [],
    }
    assert _message_text(message) == "brief.docx\nfeishu://file_tok_123"


def test_message_text_extracts_image_key_and_title():
    message = {
        "content": '{"image_key":"img_tok_123","title":"现场照片"}',
        "mentions": [],
    }
    assert _message_text(message) == "现场照片\nfeishu://img_tok_123"


def test_message_text_extracts_feishu_post_rich_text_body_and_links():
    message = {
        "content": (
            '{"post":{"zh_cn":{"title":"参考资料要用这种结构",'
            '"content":[['
            '{"tag":"text","text":"原文链接："},'
            '{"tag":"a","text":"本田-川端康成 大纲","href":"https://ocnwptzvwvt6.feishu.cn/docx/Mia0dT1kuovU8wxVT2ncgGJInnd"}'
            '],['
            '{"tag":"text","text":"参考资料索引"}'
            '],['
            '{"tag":"a","text":"Honda Global Heritage","href":"https://global.honda/en/heritage/episodes/1958manttrace.html"}'
            ']]}}}'
        ),
        "mentions": [],
    }
    assert _message_text(message) == (
        "参考资料要用这种结构\n"
        "原文链接：本田-川端康成 大纲 (https://ocnwptzvwvt6.feishu.cn/docx/Mia0dT1kuovU8wxVT2ncgGJInnd)\n"
        "参考资料索引\n"
        "Honda Global Heritage (https://global.honda/en/heritage/episodes/1958manttrace.html)\n"
        "https://ocnwptzvwvt6.feishu.cn/docx/Mia0dT1kuovU8wxVT2ncgGJInnd\n"
        "https://global.honda/en/heritage/episodes/1958manttrace.html"
    )


def test_message_text_falls_back_for_special_message_without_text():
    message = {
        "content": '{"type":"post","elements":[{"tag":"unknown"}]}',
        "mentions": [],
    }
    assert "没有解析到正文" in _message_text(message)


def test_text_project_commands_parse_to_card_actions():
    assert _parse_project_command("优化当前批次 Prompt") == {"action": "batch.optimize_prompt", "batch_no": "batch_001"}
    assert _parse_project_command("生成当前批次帧 batch_002") == {
        "action": "project.generate_all_images",
        "batch_no": "batch_002",
    }
    assert _parse_project_command("生成全部视频") == {"action": "project.generate_all_videos", "batch_no": "batch_001"}
    assert _parse_project_command("启动首尾帧同步") == {
        "action": "project.set_transition_alignment",
        "batch_no": "batch_001",
    }
    assert _parse_project_command("同步表格") == {"action": "project.sync", "batch_no": "batch_001"}


def test_chatbot_model_command_allows_new_providers():
    assert _parse_chatbot_model_command("切换chatbot模型 deepseek-v4-pro") == "deepseek-v4-pro"
    assert _parse_chatbot_model_command("chatbot模型 google/gemini-3.1-pro-preview") == "google/gemini-3.1-pro-preview"


def test_new_session_command_aliases():
    for text in ["New session", "new session", "newsession", "重置聊天", "清空聊天记录"]:
        assert _is_new_session_command(text)


def test_new_session_command_accepts_trailing_punctuation():
    for text in ["New session。", "new session!", "重置聊天？", "清空会话；"]:
        assert _is_new_session_command(text)


def test_assistant_mode_command_aliases():
    assert _parse_assistant_mode_command("Agent").mode == "agent"
    assert _parse_assistant_mode_command("智能体").mode == "agent"
    assert _parse_assistant_mode_command("Codex").mode == "agent"
    assert _parse_assistant_mode_command("Deep Research").mode == "deep_research"
    assert _parse_assistant_mode_command("拆分镜需求").mode == "storyboard_breakdown"
    assert _parse_assistant_mode_command("分镜拆解").mode == "storyboard_breakdown"
    assert _parse_assistant_mode_command("重新拆解").mode == "storyboard_breakdown"
    assert _parse_assistant_mode_command("分镜助手").mode == "storyboard"
    assert _parse_assistant_mode_command("视频助手").mode == "storyboard"
    assert _parse_assistant_mode_command("普通助手").mode == "chat"


def test_assistant_mode_command_supports_agent_runtime_switch():
    command = _parse_assistant_mode_command("Agent deepseek 请帮我梳理")
    assert command is not None
    assert command.mode == "agent"
    assert command.agent_runtime == "deepseek"
    assert command.prompt == "请帮我梳理"


def test_agent_runtime_command_aliases():
    assert _parse_agent_runtime_command("切换Agent模型 deepseek") == "deepseek"
    assert _parse_agent_runtime_command("智能体模型 codex") == "codex"


def test_assistant_mode_command_accepts_multiline_prompt():
    command = _parse_assistant_mode_command("拆分镜需求 请拆成分镜\nhttps://feishu.cn/file/abc")
    assert command is not None
    assert command.mode == "storyboard_breakdown"
    assert "https://feishu.cn/file/abc" in command.prompt


def test_extract_message_urls_supports_markdown_links_without_trailing_parenthesis():
    urls = _extract_message_urls("[新建 DOCX 文档](https://ocnwptzvwvt6.feishu.cn/docx/PVAidWsAToWPS2xEJfecfeYqnts)")

    assert urls == ["https://ocnwptzvwvt6.feishu.cn/docx/PVAidWsAToWPS2xEJfecfeYqnts"]


def test_direct_image_command_parses_model_prompt_and_reference():
    command = _parse_direct_generation_command(
        "直接生成图片 模型=nanobanana 提示词=把这张图改成卡通海报风 参考图=https://foo.feishu.cn/file/abc123"
    )
    assert command is not None
    assert command.kind == "image"
    assert command.model == "nanobanana"
    assert command.prompt == "把这张图改成卡通海报风"
    assert command.reference_images == ("https://foo.feishu.cn/file/abc123",)


def test_direct_video_command_parses_frames_and_duration():
    command = _parse_direct_generation_command(
        "直接生成视频 模型=小云雀 提示词=镜头缓慢推近 首帧=https://foo.feishu.cn/file/first 尾帧=https://foo.feishu.cn/file/last 时长=5"
    )
    assert command is not None
    assert command.kind == "video"
    assert command.model == "小云雀"
    assert command.duration_seconds == 5
    assert command.first_frame == ("https://foo.feishu.cn/file/first",)
    assert command.last_frame == ("https://foo.feishu.cn/file/last",)


def test_chatbot_reply_card_uses_markdown_block():
    card = chatbot_reply_card(content="**重点**\n- 第一条\n```python\nprint(1)\n```", chat_type="group", sender_open_id="ou_alice")
    assert card["header"]["title"]["content"] == "哔车AI助手"
    assert card["elements"][0]["tag"] == "markdown"
    assert "**重点**" in card["elements"][0]["content"]
    assert card["elements"][1]["tag"] == "action"
    labels = [item["text"]["content"] for item in card["elements"][1]["actions"]]
    assert labels == ["/Agent", "/普通助手", "/New Session", "/视频助手", "/Deep Research", "/分镜拆解"]


def test_chatbot_reply_card_adds_save_to_feishu_button_for_local_artifact():
    card = chatbot_reply_card(
        content="文件在这里：\nwilhelm-ii-early-mercedes-factory-photo.png",
        title="哔车AI助手 · Agent",
        chat_id="oc_group",
        chat_type="group",
        sender_open_id="ou_alice",
    )

    actions = card["elements"][1]["actions"]
    labels = [item["text"]["content"] for item in actions]
    assert labels[0] == "保存到飞书"
    assert actions[0]["value"]["action"] == "assistant.upload_recent_artifact"


def test_render_feishu_markdown_converts_gfm_table_to_feishu_table_tag():
    rendered = render_feishu_markdown(
        "| 模型 | 状态 |\n| --- | --- |\n| qwen-plus | 可用 |\n| gpt-5.4 | 不可用 |"
    )
    assert rendered.startswith("<table ")
    assert "columns={[{" in rendered
    assert '"title": "模型"' in rendered
    assert '"col_0": "qwen-plus"' in rendered


def test_render_feishu_markdown_downgrades_h3_to_bold():
    rendered = render_feishu_markdown("### 第三级标题\n正文")
    assert rendered.splitlines()[0] == "**第三级标题**"
