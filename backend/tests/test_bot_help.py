from app.adapters.feishu_cards import chatbot_reply_card, help_card, render_feishu_markdown
from app.api.routes.webhooks import _is_help_command, _message_text
from app.services.bot_commands import _parse_chatbot_model_command, _parse_create_project_command, _parse_project_command


def test_help_command_aliases():
    for text in ["帮助", "help", "菜单", "命令", "指令", "使用说明", "说明"]:
        assert _is_help_command(text)


def test_help_card_contains_core_commands():
    card = help_card()
    content = card["elements"][0]["content"]
    assert "/新建分镜项目：项目名" in content
    assert "/优化当前批次 Prompt" in content
    assert "/生成全部图片" in content
    assert "/生成全部视频" in content
    assert "/切换chatbot模型 qwen-plus" in content
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


def test_message_text_strips_feishu_mentions():
    message = {
        "content": '{"text":"@_user_1 help"}',
        "mentions": [{"key": "_user_1", "name": "AI 分镜机器人"}],
    }
    assert _message_text(message) == "help"


def test_text_project_commands_parse_to_card_actions():
    assert _parse_project_command("优化当前批次 Prompt") == {"action": "batch.optimize_prompt", "batch_no": "batch_001"}
    assert _parse_project_command("生成当前批次帧 batch_002") == {
        "action": "project.generate_all_images",
        "batch_no": "batch_002",
    }
    assert _parse_project_command("生成全部视频") == {"action": "project.generate_all_videos", "batch_no": "batch_001"}
    assert _parse_project_command("启动首尾帧同步") == {
        "action": "project.enable_transition_alignment",
        "batch_no": "batch_001",
    }
    assert _parse_project_command("同步表格") == {"action": "project.sync", "batch_no": "batch_001"}


def test_chatbot_model_command_allows_new_providers():
    assert _parse_chatbot_model_command("切换chatbot模型 deepseek-v4-pro") == "deepseek-v4-pro"
    assert _parse_chatbot_model_command("chatbot模型 google/gemini-3.1-pro-preview") == "google/gemini-3.1-pro-preview"


def test_chatbot_reply_card_uses_markdown_block():
    card = chatbot_reply_card(content="**重点**\n- 第一条\n```python\nprint(1)\n```")
    assert card["header"]["title"]["content"] == "AI 分镜助手"
    assert card["elements"][0]["tag"] == "markdown"
    assert "**重点**" in card["elements"][0]["content"]


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
