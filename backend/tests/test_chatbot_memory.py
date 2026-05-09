import asyncio

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

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
    assert "通用中文 AI 助手" in messages[0]["content"]
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
    assert sent["card"]["header"]["title"]["content"] == "AI 助手"
    assert sent["card"]["elements"][0]["tag"] == "markdown"
    assert "**回复重点**" in sent["card"]["elements"][0]["content"]


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
    assert captured["active_model"] == "qwen-plus"
    assert "Deep Research" in captured["messages"][0]["content"]


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
