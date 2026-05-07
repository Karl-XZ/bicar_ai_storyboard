from app.adapters.feishu_cards import help_card
from app.api.routes.webhooks import _is_help_command, _message_text
from app.services.bot_commands import _parse_create_project_command, _parse_project_command


def test_help_command_aliases():
    for text in ["帮助", "help", "/help", "菜单", "命令", "指令", "使用说明", "说明"]:
        assert _is_help_command(text)


def test_help_card_contains_core_commands():
    card = help_card()
    content = card["elements"][0]["content"]
    assert "新建分镜项目：项目名" in content
    assert "优化当前批次 Prompt" in content
    assert "生成全部图片" in content
    assert "生成全部视频" in content
    assert "生成状态" in content
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
