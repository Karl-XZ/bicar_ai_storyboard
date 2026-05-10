import asyncio

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.adapters.feishu import FeishuApiError
from app.core.config import settings
from app.domain.schemas import CreateProjectRequest
from app.models import Base
from app.services import bot_commands
from app.services.chat_memory import ChatMemoryService, resolve_chat_session
from app.services.chat_preferences import ChatPreferenceService
from app.services.projects import ProjectService


def make_db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_resolve_chat_session_distinguishes_group_and_private():
    group = resolve_chat_session(chat_id="oc_group", chat_type="group", sender_open_id="ou_alice")
    private = resolve_chat_session(chat_id="oc_p2p", chat_type="p2p", sender_open_id="ou_alice")

    assert group.session_type == "group"
    assert group.session_key == "group:oc_group"
    assert private.session_type == "private"
    assert private.session_key == "private:ou_alice"


def test_chat_memory_keeps_last_20_rounds_per_session():
    db = make_db()
    memory = ChatMemoryService(db, chat_id="oc_group", chat_type="group", sender_open_id="ou_alice")

    for index in range(25):
        memory.append_turn(user_text=f"user-{index}", assistant_text=f"assistant-{index}")
    db.commit()

    messages = memory.recent_messages()
    assert len(messages) == 40
    assert messages[0].content == "user-5"
    assert messages[-1].content == "assistant-24"


def test_latest_for_chat_does_not_fall_back_to_other_chat():
    db = make_db()
    project = ProjectService(db).create_project(CreateProjectRequest(name="隔离项目"))
    project.feishu_app_token = "app_001"
    project.workflow_config = {**(project.workflow_config or {}), "chat_id": "oc_bound"}
    db.commit()

    assert ProjectService(db).latest_for_chat("oc_missing") is None
    assert ProjectService(db).latest_for_chat("oc_bound").id == project.id


def test_chatbot_reply_uses_history_and_persists_turn(monkeypatch):
    db = make_db()
    project = ProjectService(db).create_project(CreateProjectRequest(name="群聊项目"))
    project.feishu_app_token = "app_001"
    project.workflow_config = {**(project.workflow_config or {}), "chat_id": "oc_group"}
    db.commit()

    history = ChatMemoryService(db, chat_id="oc_group", chat_type="group", sender_open_id="ou_alice")
    history.append_turn(user_text="第一问", assistant_text="第一答")
    db.commit()

    monkeypatch.setattr(settings, "dashscope_api_key", "test-key")
    monkeypatch.setattr(settings, "default_text_provider", "dashscope")
    monkeypatch.setattr(settings, "dashscope_text_model", "qwen-plus")

    captured: dict[str, object] = {}

    async def fake_dashscope_chat(*, model: str, messages: list[dict[str, str]]) -> str:
        captured["model"] = model
        captured["messages"] = messages
        return "第二答"

    monkeypatch.setattr(bot_commands, "_dashscope_chat", fake_dashscope_chat)

    reply = asyncio.run(
        bot_commands._chatbot_reply(
            db,
            text="第二问",
            chat_id="oc_group",
            chat_type="group",
            sender_open_id="ou_bob",
        )
    )

    assert reply == "第二答"
    assert captured["model"] == "qwen-plus"
    messages = captured["messages"]
    assert isinstance(messages, list)
    assert "哔车AI助手" in messages[0]["content"]
    assert "当前绑定项目：群聊项目" in messages[0]["content"]
    assert "最近一次模型 smoke test 时间：2026-05-09" in messages[0]["content"]
    assert "小云雀 已正式接入当前项目" in messages[0]["content"]
    assert "/Deep Research" in messages[0]["content"]
    assert "/分镜助手" in messages[0]["content"]
    assert "deepseek-v4-pro、deepseek-v4-flash" in messages[0]["content"]
    assert messages[1:] == [
        {"role": "user", "content": "第一问"},
        {"role": "assistant", "content": "第一答"},
        {"role": "user", "content": "第二问"},
    ]

    stored = ChatMemoryService(db, chat_id="oc_group", chat_type="group", sender_open_id="ou_carol").recent_messages()
    assert [item.content for item in stored][-2:] == ["第二问", "第二答"]


def test_handle_bot_text_sends_normal_chat_as_markdown_card(monkeypatch):
    db = make_db()
    sent: dict[str, object] = {}

    class FakeFeishuClient:
        async def send_card(self, receive_id: str, card: dict, receive_id_type: str = "chat_id") -> dict:
            sent["receive_id"] = receive_id
            sent["card"] = card
            sent["receive_id_type"] = receive_id_type
            return {"ok": True}

        async def send_text(self, receive_id: str, text: str, receive_id_type: str = "chat_id") -> dict:
            raise AssertionError("normal chatbot replies should use markdown card instead of text")

    async def fake_chatbot_reply(*args, **kwargs) -> str:
        return "**回复重点**\n- 第一条\n- 第二条"

    monkeypatch.setattr(bot_commands, "FeishuClient", FakeFeishuClient)
    monkeypatch.setattr(bot_commands, "_chatbot_reply", fake_chatbot_reply)

    result = asyncio.run(
        bot_commands.handle_bot_text(
            db,
            text="给我一个总结",
            chat_id="oc_group",
            chat_type="group",
            sender_open_id="ou_alice",
        )
    )

    assert result == {"message": "chatbot 已回复", "data": {"chat_id": "oc_group"}}
    assert sent["receive_id"] == "oc_group"
    assert sent["receive_id_type"] == "chat_id"
    assert sent["card"]["header"]["title"]["content"] == "哔车AI助手"
    assert sent["card"]["elements"][0]["tag"] == "markdown"
    assert "**回复重点**" in sent["card"]["elements"][0]["content"]
    assert sent["card"]["elements"][1]["actions"][0]["text"]["content"] == "/New Session"


def test_switch_chatbot_model_persists_without_project(monkeypatch):
    db = make_db()
    sent: dict[str, object] = {}

    class FakeFeishuClient:
        async def send_text(self, receive_id: str, text: str, receive_id_type: str = "chat_id") -> dict:
            sent["receive_id"] = receive_id
            sent["text"] = text
            sent["receive_id_type"] = receive_id_type
            return {"ok": True}

        async def send_card(self, receive_id: str, card: dict, receive_id_type: str = "chat_id") -> dict:
            raise AssertionError("model switch should respond with text")

    monkeypatch.setattr(bot_commands, "FeishuClient", FakeFeishuClient)

    result = asyncio.run(
        bot_commands.handle_bot_text(
            db,
            text="/切换chatbot模型 google/gemini-3.1-pro-preview",
            chat_id="oc_group",
            chat_type="group",
            sender_open_id="ou_alice",
        )
    )

    assert result == {"message": "chatbot 模型已切换", "data": {"model": "google/gemini-3.1-pro-preview"}}
    assert sent["receive_id"] == "oc_group"
    assert sent["text"] == "chatbot 文本模型已切换为：google/gemini-3.1-pro-preview"
    assert (
        ChatPreferenceService(db).get_chatbot_text_model(
            chat_id="oc_group",
            chat_type="group",
            sender_open_id="ou_alice",
        )
        == "google/gemini-3.1-pro-preview"
    )


def test_new_session_command_clears_only_current_session(monkeypatch):
    db = make_db()
    group_memory = ChatMemoryService(db, chat_id="oc_group", chat_type="group", sender_open_id="ou_alice")
    group_memory.append_turn(user_text="群聊第一问", assistant_text="群聊第一答")
    private_memory = ChatMemoryService(db, chat_id="oc_p2p", chat_type="p2p", sender_open_id="ou_bob")
    private_memory.append_turn(user_text="私聊第一问", assistant_text="私聊第一答")
    db.commit()

    sent: dict[str, object] = {}

    class FakeFeishuClient:
        async def send_text(self, receive_id: str, text: str, receive_id_type: str = "chat_id") -> dict:
            sent["receive_id"] = receive_id
            sent["text"] = text
            sent["receive_id_type"] = receive_id_type
            return {"ok": True}

        async def send_card(self, receive_id: str, card: dict, receive_id_type: str = "chat_id") -> dict:
            raise AssertionError("new session should respond with text")

    monkeypatch.setattr(bot_commands, "FeishuClient", FakeFeishuClient)

    result = asyncio.run(
        bot_commands.handle_bot_text(
            db,
            text="/New session",
            chat_id="oc_group",
            chat_type="group",
            sender_open_id="ou_alice",
        )
    )

    assert result == {"message": "聊天记录已重置", "data": {"chat_id": "oc_group", "cleared_messages": 2}}
    assert sent["receive_id"] == "oc_group"
    assert sent["text"] == "当前会话的聊天记录已重置。"
    assert ChatMemoryService(db, chat_id="oc_group", chat_type="group", sender_open_id="ou_alice").recent_messages() == []
    assert [item.content for item in ChatMemoryService(db, chat_id="oc_p2p", chat_type="p2p", sender_open_id="ou_bob").recent_messages()] == [
        "私聊第一问",
        "私聊第一答",
    ]


def test_chatbot_reply_enables_openrouter_web_search_for_gemini_models(monkeypatch):
    db = make_db()
    ChatPreferenceService(db).set_chatbot_text_model(
        chat_id="oc_group",
        chat_type="group",
        sender_open_id="ou_alice",
        model="google/gemini-3.1-pro-preview",
    )
    db.commit()

    captured: dict[str, object] = {}

    async def fake_openrouter_chat(*, model: str, messages: list[dict[str, str]], enable_web_search: bool = False) -> str:
        captured["model"] = model
        captured["messages"] = messages
        captured["enable_web_search"] = enable_web_search
        return "已联网搜索后的回答"

    monkeypatch.setattr(settings, "openrouter_api_key", "test-key")
    monkeypatch.setattr(bot_commands, "_openrouter_chat", fake_openrouter_chat)

    reply = asyncio.run(
        bot_commands._chatbot_reply(
            db,
            text="帮我查一下 e.go 最近的公开动态",
            chat_id="oc_group",
            chat_type="group",
            sender_open_id="ou_alice",
        )
    )

    assert reply == "已联网搜索后的回答"
    assert captured["model"] == "google/gemini-3.1-pro-preview"
    assert captured["enable_web_search"] is True
    messages = captured["messages"]
    assert isinstance(messages, list)
    assert "当前聊天模型通过 OpenRouter 接入了联网搜索" in messages[0]["content"]


def test_assistant_mode_switch_persists(monkeypatch):
    db = make_db()
    sent: dict[str, object] = {}

    class FakeFeishuClient:
        async def send_text(self, receive_id: str, text: str, receive_id_type: str = "chat_id") -> dict:
            sent["receive_id"] = receive_id
            sent["text"] = text
            return {"ok": True}

        async def send_card(self, receive_id: str, card: dict, receive_id_type: str = "chat_id") -> dict:
            sent["card"] = card
            return {"ok": True}

    monkeypatch.setattr(bot_commands, "FeishuClient", FakeFeishuClient)

    result = asyncio.run(
        bot_commands.handle_bot_text(
            db,
            text="/分镜助手",
            chat_id="oc_group",
            chat_type="group",
            sender_open_id="ou_alice",
        )
    )

    assert result == {"message": "聊天模式已切换", "data": {"mode": "storyboard"}}
    assert sent["text"] == "当前会话已切换为：分镜助手"
    assert (
        ChatPreferenceService(db).get_assistant_mode(
            chat_id="oc_group",
            chat_type="group",
            sender_open_id="ou_alice",
        )
        == "storyboard"
    )


def test_openrouter_chat_includes_web_search_tool_when_enabled(monkeypatch):
    captured: dict[str, object] = {}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, headers=None, json=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "联网回复"}}]},
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr(settings, "openrouter_api_key", "test-key")
    monkeypatch.setattr(settings, "openrouter_base_url", "https://openrouter.ai/api/v1")
    monkeypatch.setattr(bot_commands.httpx, "AsyncClient", FakeAsyncClient)

    reply = asyncio.run(
        bot_commands._openrouter_chat(
            model="google/gemini-3.1-pro-preview",
            messages=[{"role": "user", "content": "查一下最新动态"}],
            enable_web_search=True,
        )
    )

    assert reply == "联网回复"
    assert captured["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert captured["json"]["tools"] == [{"type": "openrouter:web_search", "parameters": {"max_results": 5}}]


def test_chatbot_reply_uses_deep_research_mode(monkeypatch):
    db = make_db()
    project = ProjectService(db).create_project(CreateProjectRequest(name="不应注入的项目"))
    project.feishu_app_token = "app_001"
    project.workflow_config = {**(project.workflow_config or {}), "chat_id": "oc_group"}
    ChatPreferenceService(db).set_assistant_mode(
        chat_id="oc_group",
        chat_type="group",
        sender_open_id="ou_alice",
        mode="deep_research",
    )
    db.commit()

    captured: dict[str, object] = {}

    async def fake_deep_research_reply(*, project, query: str, messages: list[dict[str, str]], active_model: str) -> str:
        captured["query"] = query
        captured["messages"] = messages
        captured["active_model"] = active_model
        return "研究结果"

    monkeypatch.setattr(bot_commands, "_deep_research_reply", fake_deep_research_reply)

    reply = asyncio.run(
        bot_commands._chatbot_reply(
            db,
            text="请研究 e.go 的发展历程",
            chat_id="oc_group",
            chat_type="group",
            sender_open_id="ou_alice",
        )
    )

    assert reply == "研究结果"
    assert captured["query"] == "请研究 e.go 的发展历程"
    assert captured["active_model"] == "deepseek-v4-pro"
    assert "Deep Research" in captured["messages"][0]["content"]
    assert "当前绑定项目" not in captured["messages"][0]["content"]
    assert "不应注入的项目" not in captured["messages"][0]["content"]
    assert (
        ChatPreferenceService(db).get_assistant_mode(
            chat_id="oc_group",
            chat_type="group",
            sender_open_id="ou_alice",
        )
        == "chat"
    )


def test_chatbot_reply_uses_default_deepseek_model_without_preference(monkeypatch):
    db = make_db()
    monkeypatch.setattr(settings, "default_text_provider", "deepseek")
    monkeypatch.setattr(settings, "deepseek_text_model", "deepseek-v4-pro")
    monkeypatch.setattr(settings, "deepseek_api_key", "test-key")

    captured: dict[str, object] = {}

    async def fake_deepseek_chat(*, model: str, messages: list[dict[str, str]]) -> str:
        captured["model"] = model
        captured["messages"] = messages
        return "默认 DeepSeek 回复"

    monkeypatch.setattr(bot_commands, "_deepseek_chat", fake_deepseek_chat)

    reply = asyncio.run(
        bot_commands._chatbot_reply(
            db,
            text="介绍一下小米汽车",
            chat_id="oc_group",
            chat_type="group",
            sender_open_id="ou_alice",
        )
    )

    assert reply == "默认 DeepSeek 回复"
    assert captured["model"] == "deepseek-v4-pro"


def test_chatbot_reply_switches_to_selected_text_provider(monkeypatch):
    db = make_db()
    preferences = ChatPreferenceService(db)
    preferences.set_chatbot_text_model(
        chat_id="oc_group",
        chat_type="group",
        sender_open_id="ou_alice",
        model="qwen-plus",
    )
    db.commit()

    captured: dict[str, object] = {}

    async def fake_dashscope_chat(*, model: str, messages: list[dict[str, str]]) -> str:
        captured["provider"] = "dashscope"
        captured["model"] = model
        return "qwen reply"

    monkeypatch.setattr(settings, "dashscope_api_key", "dashscope-key")
    monkeypatch.setattr(bot_commands, "_dashscope_chat", fake_dashscope_chat)

    reply = asyncio.run(
        bot_commands._chatbot_reply(
            db,
            text="你好",
            chat_id="oc_group",
            chat_type="group",
            sender_open_id="ou_alice",
        )
    )

    assert reply == "qwen reply"
    assert captured == {"provider": "dashscope", "model": "qwen-plus"}

    preferences.set_chatbot_text_model(
        chat_id="oc_group",
        chat_type="group",
        sender_open_id="ou_alice",
        model="google/gemini-3.1-pro-preview",
    )
    db.commit()
    captured.clear()

    async def fake_openrouter_chat(*, model: str, messages: list[dict[str, str]], enable_web_search: bool = False) -> str:
        captured["provider"] = "openrouter"
        captured["model"] = model
        return "gemini reply"

    monkeypatch.setattr(settings, "openrouter_api_key", "openrouter-key")
    monkeypatch.setattr(bot_commands, "_openrouter_chat", fake_openrouter_chat)

    reply = asyncio.run(
        bot_commands._chatbot_reply(
            db,
            text="你好",
            chat_id="oc_group",
            chat_type="group",
            sender_open_id="ou_alice",
        )
    )

    assert reply == "gemini reply"
    assert captured == {"provider": "openrouter", "model": "google/gemini-3.1-pro-preview"}

    preferences.set_chatbot_text_model(
        chat_id="oc_group",
        chat_type="group",
        sender_open_id="ou_alice",
        model="gpt-5.4",
    )
    db.commit()
    captured.clear()

    async def fake_openai_chat(*, model: str, messages: list[dict[str, str]]) -> str:
        captured["provider"] = "openai"
        captured["model"] = model
        return "gpt reply"

    monkeypatch.setattr(settings, "openai_api_key", "openai-key")
    monkeypatch.setattr(bot_commands, "_openai_chat", fake_openai_chat)

    reply = asyncio.run(
        bot_commands._chatbot_reply(
            db,
            text="你好",
            chat_id="oc_group",
            chat_type="group",
            sender_open_id="ou_alice",
        )
    )

    assert reply == "gpt reply"
    assert captured == {"provider": "openai", "model": "gpt-5.4"}


def test_handle_card_action_supports_quick_assistant_buttons(monkeypatch):
    db = make_db()
    sent: dict[str, object] = {}

    class FakeFeishuClient:
        async def send_text(self, receive_id: str, text: str, receive_id_type: str = "chat_id") -> dict:
            sent.setdefault("texts", []).append((receive_id, text, receive_id_type))
            return {"ok": True}

    monkeypatch.setattr(bot_commands, "FeishuClient", FakeFeishuClient)
    ChatMemoryService(db, chat_id="oc_p2p", chat_type="p2p", sender_open_id="ou_alice").append_turn(
        user_text="旧问题",
        assistant_text="旧回答",
    )
    db.commit()

    clear_result = asyncio.run(
        bot_commands.handle_card_action(
            db,
            value={"action": "assistant.clear_session", "chat_type": "p2p", "sender_open_id": "ou_alice"},
            chat_id="oc_p2p",
        )
    )
    mode_result = asyncio.run(
        bot_commands.handle_card_action(
            db,
            value={"action": "assistant.set_mode", "mode": "storyboard", "chat_type": "p2p", "sender_open_id": "ou_alice"},
            chat_id="oc_p2p",
        )
    )

    assert clear_result["message"] == "聊天记录已重置"
    assert mode_result["message"] == "聊天模式已切换"
    assert ChatMemoryService(db, chat_id="oc_p2p", chat_type="p2p", sender_open_id="ou_alice").recent_messages() == []
    assert (
        ChatPreferenceService(db).get_assistant_mode(
            chat_id="oc_p2p",
            chat_type="p2p",
            sender_open_id="ou_alice",
        )
        == "storyboard"
    )


def test_upload_direct_output_falls_back_when_project_folder_is_missing(monkeypatch):
    class FakeFeishuClient:
        def __init__(self):
            self.calls = []

        async def upload_file(self, folder_token: str, name: str, content: bytes) -> dict:
            self.calls.append(folder_token)
            if folder_token == "deleted_folder":
                raise FeishuApiError("Feishu HTTP error: 400, msg=parent node not exist.")
            return {"data": {"file_token": "file_123"}}

    class FakeWorkspace:
        def __init__(self, feishu=None):
            self.feishu = feishu

        async def ensure_default_workspace_folder(self):
            return {"folder_token": "workspace_root"}

    monkeypatch.setattr(bot_commands, "FeishuWorkspaceService", FakeWorkspace)
    fake = FakeFeishuClient()

    result = asyncio.run(
        bot_commands._upload_direct_output(
            fake,
            folder_token="deleted_folder",
            filename="result.png",
            content=b"image-bytes",
        )
    )

    assert result["data"]["file_token"] == "file_123"
    assert fake.calls == ["deleted_folder", "workspace_root"]


def test_deep_research_reply_prefers_google_primary(monkeypatch):
    class FakeWorkspace:
        async def save_markdown_document(self, *, title: str, markdown: str, folder_token: str | None = None):
            class Result:
                url = "https://feishu.test/docx/research_doc"

            return Result()

    async def fake_google_deep_research_report(*, query: str, references: list[dict], project):
        return f"# 研究报告\n\n{query}"

    monkeypatch.setattr(settings, "google_api_key", "test-google")
    monkeypatch.setattr(settings, "google_deep_research_model", "deep-research-preview-04-2026")
    monkeypatch.setattr(settings, "openrouter_api_key", "")
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(bot_commands, "FeishuWorkspaceService", FakeWorkspace)
    monkeypatch.setattr(bot_commands, "_google_deep_research_report", fake_google_deep_research_report)

    reply = asyncio.run(
        bot_commands._deep_research_reply(
            project=None,
            query="研究 e.go 公司",
            messages=[{"role": "user", "content": "研究 e.go 公司"}],
            active_model="deepseek-v4-pro",
        )
    )

    assert "Gemini Deep Research" in reply
    assert "Fallback 搜索总结" not in reply
    assert "https://feishu.test/docx/research_doc" in reply


def test_deep_research_reply_marks_fallback_reason(monkeypatch):
    class FakeWorkspace:
        async def save_markdown_document(self, *, title: str, markdown: str, folder_token: str | None = None):
            class Result:
                url = "https://feishu.test/docx/research_doc"

            return Result()

    async def fail_google_deep_research_report(*, query: str, references: list[dict], project):
        raise RuntimeError("gemini unavailable")

    async def fail_openrouter_deep_research_report(*, query: str, references: list[dict], project):
        raise RuntimeError("openrouter unavailable")

    async def fail_openai_deep_research_report(*, query: str, references: list[dict], project):
        raise RuntimeError("openai unavailable")

    async def fake_fallback_research_report(*, query: str, references: list[dict], active_model: str, messages: list[dict[str, str]]):
        return "# 回退报告"

    monkeypatch.setattr(settings, "google_api_key", "test-google")
    monkeypatch.setattr(settings, "openrouter_api_key", "test-openrouter")
    monkeypatch.setattr(settings, "openai_api_key", "test-openai")
    monkeypatch.setattr(bot_commands, "FeishuWorkspaceService", FakeWorkspace)
    monkeypatch.setattr(bot_commands, "_google_deep_research_report", fail_google_deep_research_report)
    monkeypatch.setattr(bot_commands, "_openrouter_deep_research_report", fail_openrouter_deep_research_report)
    monkeypatch.setattr(bot_commands, "_openai_deep_research_report", fail_openai_deep_research_report)
    monkeypatch.setattr(bot_commands, "_fallback_research_report", fake_fallback_research_report)

    reply = asyncio.run(
        bot_commands._deep_research_reply(
            project=None,
            query="研究 e.go 公司",
            messages=[{"role": "user", "content": "研究 e.go 公司"}],
            active_model="deepseek-v4-pro",
        )
    )

    assert "Fallback 搜索总结（deepseek-v4-pro）" in reply
    assert "回退原因" in reply
    assert "gemini unavailable" in reply
    assert "openrouter unavailable" in reply


def test_format_google_interaction_response_reads_latest_text_step():
    payload = {
        "status": "completed",
        "steps": [
            {"content": [{"type": "text", "text": "中间计划"}]},
            {"content": [{"type": "text", "text": "# 最终研究报告"}]},
        ],
    }

    assert bot_commands._format_google_interaction_response(payload) == "# 最终研究报告"


def test_format_google_interaction_response_prefers_outputs_over_steps():
    payload = {
        "status": "completed",
        "outputs": [{"text": "# 完整研究报告\n\n这里是正文"}],
        "steps": [
            {"content": [{"type": "text", "text": "**Sources:**\n1. https://example.com"}]},
        ],
    }

    assert bot_commands._format_google_interaction_response(payload) == "# 完整研究报告\n\n这里是正文"


def test_deep_research_prompt_does_not_include_project_context():
    db = make_db()
    project = ProjectService(db).create_project(CreateProjectRequest(name="项目上下文不应出现"))

    prompt = bot_commands._deep_research_prompt(
        query="研究 e.go 公司",
        references=[],
        project=project,
    )

    assert "项目上下文不应出现" not in prompt
    assert "当前飞书项目" not in prompt


def test_chatbot_reply_injects_search_context_for_deepseek(monkeypatch):
    db = make_db()
    ChatPreferenceService(db).set_chatbot_text_model(
        chat_id="oc_group",
        chat_type="group",
        sender_open_id="ou_alice",
        model="deepseek-v4-pro",
    )
    db.commit()

    captured: dict[str, object] = {}

    async def fake_inject(messages: list[dict[str, str]], *, query: str) -> list[dict[str, str]]:
        captured["query"] = query
        return messages + [{"role": "system", "content": "搜索结果：example.com"}]

    async def fake_deepseek_chat(*, model: str, messages: list[dict[str, str]]) -> str:
        captured["model"] = model
        captured["messages"] = messages
        return "基于搜索结果的 DeepSeek 回答"

    monkeypatch.setattr(settings, "deepseek_api_key", "test-key")
    monkeypatch.setattr(bot_commands, "_inject_web_search_context", fake_inject)
    monkeypatch.setattr(bot_commands, "_deepseek_chat", fake_deepseek_chat)

    reply = asyncio.run(
        bot_commands._chatbot_reply(
            db,
            text="帮我查一下 e.go 公司最新公开资料",
            chat_id="oc_group",
            chat_type="group",
            sender_open_id="ou_alice",
        )
    )

    assert reply == "基于搜索结果的 DeepSeek 回答"
    assert captured["query"] == "帮我查一下 e.go 公司最新公开资料"
    assert captured["model"] == "deepseek-v4-pro"
    assert captured["messages"][-1]["content"] == "搜索结果：example.com"
