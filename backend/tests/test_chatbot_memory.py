import asyncio
from pathlib import Path

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


def test_active_project_binding_overrides_latest_project_for_chat():
    db = make_db()
    service = ProjectService(db)
    older = service.create_project(CreateProjectRequest(name="旧项目"))
    older.feishu_app_token = "app_old"
    older.feishu_table_id = "tbl_old"
    older.workflow_config = {**(older.workflow_config or {}), "chat_id": "oc_group"}

    newer = service.create_project(CreateProjectRequest(name="新项目"))
    newer.feishu_app_token = "app_new"
    newer.feishu_table_id = "tbl_new"
    newer.workflow_config = {**(newer.workflow_config or {}), "chat_id": "oc_group"}
    db.commit()

    prefs = ChatPreferenceService(db)
    prefs.set_active_project_id(chat_id="oc_group", chat_type="group", sender_open_id="ou_alice", project_id=str(older.id))
    db.commit()

    current = bot_commands._current_chat_project(
        db,
        chat_id="oc_group",
        chat_type="group",
        sender_open_id="ou_alice",
    )

    assert current is not None
    assert current.id == older.id


def test_message_with_bitable_link_rebinds_active_project():
    db = make_db()
    service = ProjectService(db)
    older = service.create_project(CreateProjectRequest(name="旧项目"))
    older.feishu_app_token = "app_old"
    older.feishu_table_id = "tbl_old"
    older.workflow_config = {**(older.workflow_config or {}), "chat_id": "oc_group"}

    newer = service.create_project(CreateProjectRequest(name="新项目"))
    newer.feishu_app_token = "app_new"
    newer.feishu_table_id = "tbl_new"
    newer.workflow_config = {**(newer.workflow_config or {}), "chat_id": "oc_group"}
    db.commit()

    prefs = ChatPreferenceService(db)
    prefs.set_active_project_id(chat_id="oc_group", chat_type="group", sender_open_id="ou_alice", project_id=str(newer.id))
    db.commit()

    rebound = bot_commands._maybe_bind_project_from_message(
        db,
        text="6450 分镜表在这里：https://ocnwptzvwvt6.feishu.cn/base/app_old?table=tbl_old",
        chat_id="oc_group",
        chat_type="group",
        sender_open_id="ou_alice",
        preferences=prefs,
    )

    assert rebound is not None
    assert rebound.id == older.id
    assert (
        prefs.get_active_project_id(
            chat_id="oc_group",
            chat_type="group",
            sender_open_id="ou_alice",
        )
        == str(older.id)
    )


def test_switch_current_project_command_rebinds_current_session(monkeypatch):
    db = make_db()
    service = ProjectService(db)
    older = service.create_project(CreateProjectRequest(name="旧项目"))
    older.feishu_app_token = "app_old"
    older.feishu_table_id = "tbl_old"
    older.feishu_folder_token = "fld_old"
    older.workflow_config = {**(older.workflow_config or {}), "chat_id": "oc_group"}

    newer = service.create_project(CreateProjectRequest(name="新项目"))
    newer.feishu_app_token = "app_new"
    newer.feishu_table_id = "tbl_new"
    newer.feishu_folder_token = "fld_new"
    newer.workflow_config = {**(newer.workflow_config or {}), "chat_id": "oc_group"}
    db.commit()

    prefs = ChatPreferenceService(db)
    prefs.set_active_project_id(chat_id="oc_group", chat_type="group", sender_open_id="ou_alice", project_id=str(newer.id))
    db.commit()

    sent: dict[str, str] = {}

    class FakeFeishuClient:
        async def send_text(self, receive_id: str, text: str, receive_id_type: str = "chat_id") -> dict:
            sent["receive_id"] = receive_id
            sent["text"] = text
            return {"ok": True}

    monkeypatch.setattr(bot_commands, "FeishuClient", FakeFeishuClient)

    result = asyncio.run(
        bot_commands.handle_bot_text(
            db,
            text="/切换当前项目 https://ocnwptzvwvt6.feishu.cn/base/app_old?table=tbl_old",
            chat_id="oc_group",
            chat_type="group",
            sender_open_id="ou_alice",
        )
    )

    assert result["message"] == "当前项目已切换"
    assert "当前会话已切换到项目：`旧项目`" in sent["text"]
    assert "https://feishu.cn/base/app_old?table=tbl_old" in sent["text"]
    assert (
        prefs.get_active_project_id(
            chat_id="oc_group",
            chat_type="group",
            sender_open_id="ou_alice",
        )
        == str(older.id)
    )


def test_chatbot_reply_uses_history_and_persists_turn(monkeypatch):
    db = make_db()
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
    assert "最近一次模型 smoke test 时间：2026-05-09" in messages[0]["content"]
    assert "小云雀 已正式接入当前项目" in messages[0]["content"]
    assert "/Deep Research" in messages[0]["content"]
    assert "/分镜助手" in messages[0]["content"]
    assert "deepseek-v4-pro、deepseek-v4-flash" in messages[0]["content"]
    assert "当前绑定项目" not in messages[0]["content"]
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
    assert sent["card"]["elements"][1]["actions"][0]["text"]["content"] == "/Agent"


def test_handle_bot_text_never_falls_back_to_default_chat(monkeypatch):
    db = make_db()
    sent_calls: list[tuple[str, str]] = []

    monkeypatch.setattr(settings, "feishu_default_chat_id", "oc_default_group")

    class FakeFeishuClient:
        async def send_card(self, receive_id: str, card: dict, receive_id_type: str = "chat_id") -> dict:
            sent_calls.append(("card", receive_id))
            return {"ok": True}

        async def send_text(self, receive_id: str, text: str, receive_id_type: str = "chat_id") -> dict:
            sent_calls.append(("text", receive_id))
            return {"ok": True}

    async def fake_chatbot_reply(*args, **kwargs) -> str:
        return "不应该回退到默认群。"

    monkeypatch.setattr(bot_commands, "FeishuClient", FakeFeishuClient)
    monkeypatch.setattr(bot_commands, "_chatbot_reply", fake_chatbot_reply)

    result = asyncio.run(
        bot_commands.handle_bot_text(
            db,
            text="测试不要回退默认群",
            chat_id=None,
            chat_type="p2p",
            sender_open_id="ou_alice",
        )
    )

    assert result == {"message": "chatbot 已回复", "data": {"chat_id": None}}
    assert sent_calls == []


def test_handle_bot_text_streams_long_reply_in_multiple_cards(monkeypatch):
    db = make_db()
    cards: list[dict] = []

    class FakeFeishuClient:
        async def send_card(self, receive_id: str, card: dict, receive_id_type: str = "chat_id") -> dict:
            cards.append(card)
            return {"ok": True}

        async def send_text(self, receive_id: str, text: str, receive_id_type: str = "chat_id") -> dict:
            raise AssertionError("segmented send should not fall back to text when all chunks succeed")

    async def fake_chatbot_reply(*args, **kwargs) -> str:
        return ("\n\n".join([f"第{i}段：" + ("内容" * 260) for i in range(1, 4)])).strip()

    monkeypatch.setattr(bot_commands, "FeishuClient", FakeFeishuClient)
    monkeypatch.setattr(bot_commands, "_chatbot_reply", fake_chatbot_reply)

    asyncio.run(
        bot_commands.handle_bot_text(
            db,
            text="给我一段很长的回复",
            chat_id="oc_group",
            chat_type="group",
            sender_open_id="ou_alice",
        )
    )

    assert len(cards) >= 2
    assert cards[0]["header"]["title"]["content"].startswith("哔车AI助手（1/")
    assert cards[-1]["elements"][-1]["tag"] == "action"


def test_send_reply_segmented_falls_back_to_full_reply(monkeypatch):
    sent_cards: list[dict] = []
    sent_texts: list[str] = []

    class FakeFeishuClient:
        def __init__(self) -> None:
            self.calls = 0

        async def send_card(self, receive_id: str, card: dict, receive_id_type: str = "chat_id") -> dict:
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("chunk send failed")
            sent_cards.append(card)
            return {"ok": True}

        async def send_text(self, receive_id: str, text: str, receive_id_type: str = "chat_id") -> dict:
            sent_texts.append(text)
            return {"ok": True}

    count = asyncio.run(
        bot_commands._send_reply_segmented(
            feishu=FakeFeishuClient(),
            target_chat="oc_group",
            content=("\n\n".join([f"第{i}段：" + ("内容" * 260) for i in range(1, 4)])).strip(),
            title="哔车AI助手",
            chat_id="oc_group",
            chat_type="group",
            sender_open_id="ou_alice",
            request_context=None,
        )
    )

    assert count >= 2
    assert any("分段发送中断" in text for text in sent_texts)
    assert sent_cards[-1]["header"]["title"]["content"] == "哔车AI助手"


def test_reply_with_timeout_hint_notifies_after_five_minutes(monkeypatch):
    sent: list[str] = []

    class FakeFeishuClient:
        async def send_text(self, receive_id: str, text: str, receive_id_type: str = "chat_id") -> dict:
            sent.append(text)
            return {"ok": True}

    async def fake_chatbot_reply(*args, **kwargs) -> str:
        await asyncio.sleep(0)
        return "最终回复"

    async def fake_wait(tasks, timeout=None):
        return set(), set(tasks)

    monkeypatch.setattr(bot_commands, "_chatbot_reply", fake_chatbot_reply)
    monkeypatch.setattr(bot_commands.asyncio, "wait", fake_wait)

    reply = asyncio.run(
        bot_commands._reply_with_timeout_hint(
            make_db(),
            text="你好",
            chat_id="oc_group",
            chat_type="group",
            sender_open_id="ou_alice",
            progress_notifier=None,
            hint_enabled=True,
            feishu=FakeFeishuClient(),
            request_context=None,
        )
    )

    assert reply == "最终回复"
    assert any("超过 5 分钟仍未回复" in message for message in sent)


def test_handle_bot_text_sends_failure_reason_when_normal_chat_errors(monkeypatch):
    db = make_db()
    sent: list[str] = []

    class FakeFeishuClient:
        async def send_card(self, receive_id: str, card: dict, receive_id_type: str = "chat_id") -> dict:
            raise AssertionError("failing normal chat should not send card")

        async def send_text(self, receive_id: str, text: str, receive_id_type: str = "chat_id") -> dict:
            sent.append(text)
            return {"ok": True}

    async def fail_reply_with_timeout_hint(*args, **kwargs):
        raise RuntimeError("provider timeout")

    monkeypatch.setattr(bot_commands, "FeishuClient", FakeFeishuClient)
    monkeypatch.setattr(bot_commands, "_reply_with_timeout_hint", fail_reply_with_timeout_hint)

    result = asyncio.run(
        bot_commands.handle_bot_text(
            db,
            text="普通聊天测试",
            chat_id="oc_group",
            chat_type="group",
            sender_open_id="ou_alice",
        )
    )

    assert result["message"] == "chatbot 回复失败"
    assert "provider timeout" in result["data"]["error"]
    assert any("本次请求处理失败，请重试" in message for message in sent)
    assert any("provider timeout" in message for message in sent)


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


def test_switch_agent_runtime_persists_without_project(monkeypatch):
    db = make_db()
    sent: dict[str, object] = {}

    class FakeFeishuClient:
        async def send_text(self, receive_id: str, text: str, receive_id_type: str = "chat_id") -> dict:
            sent["receive_id"] = receive_id
            sent["text"] = text
            return {"ok": True}

        async def send_card(self, receive_id: str, card: dict, receive_id_type: str = "chat_id") -> dict:
            raise AssertionError("runtime switch should respond with text")

    monkeypatch.setattr(bot_commands, "FeishuClient", FakeFeishuClient)

    result = asyncio.run(
        bot_commands.handle_bot_text(
            db,
            text="/切换Agent模型 deepseek",
            chat_id="oc_group",
            chat_type="group",
            sender_open_id="ou_alice",
        )
    )

    assert result == {"message": "agent runtime 已切换", "data": {"runtime": "deepseek"}}
    assert sent["text"] == "Agent 运行后端已切换为：DeepSeek。当前会话下次进入 `/Agent` 或继续 Agent 对话时生效。"
    assert (
        ChatPreferenceService(db).get_agent_runtime(
            chat_id="oc_group",
            chat_type="group",
            sender_open_id="ou_alice",
        )
        == "deepseek"
    )


def test_agent_reply_uses_deepseek_runtime_when_selected(monkeypatch):
    db = make_db()
    prefs = ChatPreferenceService(db)
    prefs.set_assistant_mode(chat_id="oc_group", chat_type="group", sender_open_id="ou_alice", mode="agent")
    prefs.set_agent_runtime(chat_id="oc_group", chat_type="group", sender_open_id="ou_alice", runtime="deepseek")
    db.commit()

    captured: dict[str, object] = {}
    reactions: list[tuple[str, str]] = []

    class FakeFeishuClient:
        async def add_message_reaction(self, message_id: str, emoji_type: str) -> dict:
            reactions.append(("add", message_id))
            return {"data": {"reaction_id": "r_001"}}

        async def remove_message_reaction(self, message_id: str, reaction_id: str) -> dict:
            reactions.append(("remove", message_id))
            return {"data": {}}

    async def fake_run_openclaw_agent(*, session_id: str, message: str, timeout_seconds: int = 600, session_key: str | None = None, model: str | None = None) -> dict[str, object]:
        captured["session_id"] = session_id
        captured["message"] = message
        captured["session_key"] = session_key
        captured["model"] = model
        return {"result": {"finalAssistantVisibleText": "deepseek agent reply"}}

    monkeypatch.setattr(bot_commands, "FeishuClient", FakeFeishuClient)
    monkeypatch.setattr(bot_commands, "_run_openclaw_agent", fake_run_openclaw_agent)

    reply = asyncio.run(
        bot_commands._chatbot_reply(
            db,
            text="请总结",
            chat_id="oc_group",
            chat_type="group",
            sender_open_id="ou_alice",
            source_message_id="om_msg_001",
        )
    )

    assert reply == "deepseek agent reply"
    assert "请总结" in captured["message"]
    assert "文档/文案修订规则（强约束）" in captured["message"]
    assert captured["model"] == "deepseek/deepseek-v4-pro"
    assert reactions == [("add", "om_msg_001"), ("remove", "om_msg_001")]


def test_agent_mode_command_runtime_switch_bumps_agent_nonce(monkeypatch):
    db = make_db()
    prefs = ChatPreferenceService(db)
    prefs.set_assistant_mode(chat_id="oc_group", chat_type="group", sender_open_id="ou_alice", mode="chat")
    before_nonce = prefs.get_agent_session_nonce(chat_id="oc_group", chat_type="group", sender_open_id="ou_alice")

    sent: dict[str, str] = {}

    class FakeFeishuClient:
        async def send_text(self, receive_id: str, text: str, receive_id_type: str = "chat_id") -> dict:
            sent["text"] = text
            return {"ok": True}

        async def send_card(self, receive_id: str, card: dict, receive_id_type: str = "chat_id") -> dict:
            raise AssertionError("mode switch should respond with text")

    monkeypatch.setattr(bot_commands, "FeishuClient", FakeFeishuClient)

    result = asyncio.run(
        bot_commands.handle_bot_text(
            db,
            text="/Agent deepseek",
            chat_id="oc_group",
            chat_type="group",
            sender_open_id="ou_alice",
        )
    )

    after_nonce = prefs.get_agent_session_nonce(chat_id="oc_group", chat_type="group", sender_open_id="ou_alice")
    assert result == {"message": "聊天模式已切换", "data": {"mode": "agent"}}
    assert after_nonce == before_nonce + 1
    assert "Agent（DeepSeek）" in sent["text"]


def test_handle_card_action_dedupes_same_mode_switch(monkeypatch):
    db = make_db()
    sent: list[str] = []

    class FakeFeishuClient:
        async def send_text(self, receive_id: str, text: str, receive_id_type: str = "chat_id") -> dict:
            sent.append(text)
            return {"ok": True}

    monkeypatch.setattr(bot_commands, "FeishuClient", FakeFeishuClient)

    first = asyncio.run(
        bot_commands.handle_card_action(
            db,
            value={"action": "assistant.set_mode", "mode": "chat", "chat_id": "oc_group", "chat_type": "group", "sender_open_id": "ou_alice"},
            chat_id="oc_group",
        )
    )
    second = asyncio.run(
        bot_commands.handle_card_action(
            db,
            value={"action": "assistant.set_mode", "mode": "chat", "chat_id": "oc_group", "chat_type": "group", "sender_open_id": "ou_alice"},
            chat_id="oc_group",
        )
    )

    assert first["message"] == "聊天模式已切换"
    assert second["message"] == "重复模式切换已忽略"
    assert len(sent) == 1


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


def test_chat_memory_is_isolated_between_groups_and_private_sessions():
    db = make_db()
    group_a = ChatMemoryService(db, chat_id="oc_group_a", chat_type="group", sender_open_id="ou_alice")
    group_b = ChatMemoryService(db, chat_id="oc_group_b", chat_type="group", sender_open_id="ou_alice")
    private_alice = ChatMemoryService(db, chat_id="oc_p2p_a", chat_type="p2p", sender_open_id="ou_alice")
    private_bob = ChatMemoryService(db, chat_id="oc_p2p_b", chat_type="p2p", sender_open_id="ou_bob")

    group_a.append_turn(user_text="乐高项目", assistant_text="群A回复")
    group_b.append_turn(user_text="e.go项目", assistant_text="群B回复")
    private_alice.append_turn(user_text="Alice私聊", assistant_text="Alice回复")
    private_bob.append_turn(user_text="Bob私聊", assistant_text="Bob回复")
    db.commit()

    assert [item.content for item in group_a.recent_messages()] == ["乐高项目", "群A回复"]
    assert [item.content for item in group_b.recent_messages()] == ["e.go项目", "群B回复"]
    assert [item.content for item in private_alice.recent_messages()] == ["Alice私聊", "Alice回复"]
    assert [item.content for item in private_bob.recent_messages()] == ["Bob私聊", "Bob回复"]


def test_newer_chat_request_suppresses_older_reply(monkeypatch):
    db = make_db()
    sent_cards: list[str] = []
    release_first = asyncio.Event()
    call_count = {"value": 0}

    class FakeFeishuClient:
        async def send_card(self, receive_id: str, card: dict, receive_id_type: str = "chat_id") -> dict:
            sent_cards.append(card["elements"][0]["content"])
            return {"ok": True}

        async def send_text(self, receive_id: str, text: str, receive_id_type: str = "chat_id") -> dict:
            return {"ok": True}

    async def fake_generate_chat_response(*, model: str, messages: list[dict[str, str]], text: str, assistant_mode: str) -> str:
        call_count["value"] += 1
        if call_count["value"] == 1:
            await release_first.wait()
            return "旧回复"
        return "新回复"

    monkeypatch.setattr(bot_commands, "FeishuClient", FakeFeishuClient)
    monkeypatch.setattr(bot_commands, "_generate_chat_response", fake_generate_chat_response)

    async def run_scenario():
        first = asyncio.create_task(
            bot_commands.handle_bot_text(
                db,
                text="第一条消息",
                chat_id="oc_group",
                chat_type="group",
                sender_open_id="ou_alice",
            )
        )
        await asyncio.sleep(0)
        second = asyncio.create_task(
            bot_commands.handle_bot_text(
                db,
                text="第二条消息",
                chat_id="oc_group",
                chat_type="group",
                sender_open_id="ou_alice",
            )
        )
        await asyncio.sleep(0)
        release_first.set()
        return await asyncio.gather(first, second)

    results = asyncio.run(run_scenario())

    assert results[0]["message"] == "chatbot 旧回复已抑制"
    assert results[1]["message"] == "chatbot 已回复"
    assert sent_cards == ["新回复"]


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
    assert sent["text"] == "当前会话已切换为：分镜助手。请继续发送分镜需求、项目命令或素材说明。"
    assert (
        ChatPreferenceService(db).get_assistant_mode(
            chat_id="oc_group",
            chat_type="group",
            sender_open_id="ou_alice",
        )
        == "storyboard"
    )


def test_agent_mode_switch_persists(monkeypatch):
    db = make_db()
    sent: dict[str, object] = {}

    class FakeFeishuClient:
        async def send_text(self, receive_id: str, text: str, receive_id_type: str = "chat_id") -> dict:
            sent["receive_id"] = receive_id
            sent["text"] = text
            return {"ok": True}

    monkeypatch.setattr(bot_commands, "FeishuClient", FakeFeishuClient)

    result = asyncio.run(
        bot_commands.handle_bot_text(
            db,
            text="/Agent",
            chat_id="oc_group",
            chat_type="group",
            sender_open_id="ou_alice",
        )
    )

    assert result == {"message": "聊天模式已切换", "data": {"mode": "agent"}}
    assert "当前会话已切换为：Agent" in sent["text"]
    assert (
        ChatPreferenceService(db).get_assistant_mode(
            chat_id="oc_group",
            chat_type="group",
            sender_open_id="ou_alice",
        )
        == "agent"
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

    async def fake_deep_research_reply(*, query: str, messages: list[dict[str, str]], active_model: str, progress_notifier=None, request_context=None) -> str:
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


def test_chatbot_reply_uses_agent_mode_and_appends_local_memory(monkeypatch):
    db = make_db()
    preferences = ChatPreferenceService(db)
    preferences.set_assistant_mode(
        chat_id="oc_group",
        chat_type="group",
        sender_open_id="ou_alice",
        mode="agent",
    )
    ChatMemoryService(db, chat_id="oc_group", chat_type="group", sender_open_id="ou_alice").append_turn(
        user_text="旧问题",
        assistant_text="旧回答",
    )
    db.commit()

    captured: dict[str, object] = {}
    reactions: list[tuple[str, str]] = []

    class FakeFeishuClient:
        async def add_message_reaction(self, message_id: str, emoji_type: str) -> dict:
            reactions.append(("add", message_id))
            return {"data": {"reaction_id": "r_001"}}

        async def remove_message_reaction(self, message_id: str, reaction_id: str) -> dict:
            reactions.append(("remove", message_id))
            return {"data": {}}

    async def fake_run_openclaw_agent(*, session_id: str, message: str, timeout_seconds: int = 600, session_key: str | None = None, model: str | None = None) -> dict[str, object]:
        captured["session_id"] = session_id
        captured["message"] = message
        captured["session_key"] = session_key
        captured["model"] = model
        return {"result": {"finalAssistantVisibleText": "agent reply"}}

    monkeypatch.setattr(bot_commands, "_openclaw_agent_session_file", lambda _: Path("/tmp/nonexistent-openclaw-session.jsonl"))
    monkeypatch.setattr(bot_commands, "FeishuClient", FakeFeishuClient)
    monkeypatch.setattr(bot_commands, "_run_openclaw_agent", fake_run_openclaw_agent)

    reply = asyncio.run(
        bot_commands._chatbot_reply(
            db,
            text="请帮我查日志",
            chat_id="oc_group",
            chat_type="group",
            sender_open_id="ou_alice",
            source_message_id="om_msg_001",
        )
    )

    assert reply == "agent reply"
    assert "当前新请求：请帮我查日志" in captured["message"]
    assert "用户：旧问题" in captured["message"]
    assert "助手：旧回答" in captured["message"]
    assert captured["model"] is None
    assert reactions == [("add", "om_msg_001"), ("remove", "om_msg_001")]
    messages = ChatMemoryService(db, chat_id="oc_group", chat_type="group", sender_open_id="ou_alice").recent_messages()
    assert [item.content for item in messages] == ["旧问题", "旧回答", "请帮我查日志", "agent reply"]


def test_deepseek_agent_second_turn_sees_previous_material(monkeypatch):
    db = make_db()
    prefs = ChatPreferenceService(db)
    prefs.set_assistant_mode(chat_id="oc_group", chat_type="group", sender_open_id="ou_alice", mode="agent")
    prefs.set_agent_runtime(chat_id="oc_group", chat_type="group", sender_open_id="ou_alice", runtime="deepseek")
    db.commit()

    captured_calls: list[str] = []

    async def fake_run_openclaw_agent(*, session_id: str, message: str, timeout_seconds: int = 600, session_key: str | None = None, model: str | None = None) -> dict[str, object]:
        captured_calls.append(message)
        if len(captured_calls) == 1:
            return {"result": {"finalAssistantVisibleText": "我读到了资料结构。"}}
        return {"result": {"finalAssistantVisibleText": "我读到了标题、原文链接和资料链接。"}}

    monkeypatch.setattr(bot_commands, "_run_openclaw_agent", fake_run_openclaw_agent)

    first_reply = asyncio.run(
        bot_commands._chatbot_reply(
            db,
            text="参考资料要用这种结构（原文链接：本田-川端康成 大纲）\n参考资料索引\nhttps://global.honda/en/heritage/episodes/1958manttrace.html",
            chat_id="oc_group",
            chat_type="group",
            sender_open_id="ou_alice",
        )
    )
    second_reply = asyncio.run(
        bot_commands._chatbot_reply(
            db,
            text="请根据上面那条资料，先确认你是否读到了标题、原文链接和资料链接。",
            chat_id="oc_group",
            chat_type="group",
            sender_open_id="ou_alice",
        )
    )

    assert first_reply == "我读到了资料结构。"
    assert second_reply == "我读到了标题、原文链接和资料链接。"
    assert len(captured_calls) == 2
    memory_contents = [item.content for item in ChatMemoryService(db, chat_id="oc_group", chat_type="group", sender_open_id="ou_alice").recent_messages()]
    assert "参考资料要用这种结构（原文链接：本田-川端康成 大纲）\n参考资料索引\nhttps://global.honda/en/heritage/episodes/1958manttrace.html" in memory_contents
    assert "我读到了资料结构。" in memory_contents


def test_openclaw_agent_existing_session_does_not_duplicate_memory(monkeypatch, tmp_path):
    db = make_db()
    prefs = ChatPreferenceService(db)
    prefs.set_assistant_mode(chat_id="oc_group", chat_type="group", sender_open_id="ou_alice", mode="agent")
    ChatMemoryService(db, chat_id="oc_group", chat_type="group", sender_open_id="ou_alice").append_turn(
        user_text="旧问题",
        assistant_text="旧回答",
    )
    db.commit()

    session_id = bot_commands._openclaw_agent_session_id(
        chat_id="oc_group",
        chat_type="group",
        sender_open_id="ou_alice",
        nonce=prefs.get_agent_session_nonce(chat_id="oc_group", chat_type="group", sender_open_id="ou_alice"),
    )
    existing = tmp_path / f"{session_id}.jsonl"
    existing.write_text("{}\n")
    monkeypatch.setattr(bot_commands, "_openclaw_agent_session_file", lambda _: existing)

    captured: dict[str, object] = {}

    class FakeFeishuClient:
        async def add_message_reaction(self, message_id: str, emoji_type: str) -> dict:
            return {"data": {"reaction_id": "r_001"}}

        async def remove_message_reaction(self, message_id: str, reaction_id: str) -> dict:
            return {"data": {}}

    async def fake_run_openclaw_agent(*, session_id: str, message: str, timeout_seconds: int = 600, session_key: str | None = None, model: str | None = None) -> dict[str, object]:
        captured["message"] = message
        return {"result": {"finalAssistantVisibleText": "ok"}}

    monkeypatch.setattr(bot_commands, "FeishuClient", FakeFeishuClient)
    monkeypatch.setattr(bot_commands, "_run_openclaw_agent", fake_run_openclaw_agent)

    reply = asyncio.run(
        bot_commands._chatbot_reply(
            db,
            text="继续",
            chat_id="oc_group",
            chat_type="group",
            sender_open_id="ou_alice",
            source_message_id="om_msg_002",
        )
    )

    assert reply == "ok"
    assert captured["message"].endswith("继续")
    assert "文档/文案修订规则（强约束）" in captured["message"]


def test_agent_upload_request_uses_recent_local_artifact_and_returns_feishu_link(monkeypatch, tmp_path):
    db = make_db()
    prefs = ChatPreferenceService(db)
    prefs.set_assistant_mode(chat_id="oc_group", chat_type="group", sender_open_id="ou_alice", mode="agent")
    local_file = tmp_path / "wilhelm-ii-early-mercedes-factory-photo.png"
    local_file.write_bytes(b"fake-image")
    ChatMemoryService(db, chat_id="oc_group", chat_type="group", sender_open_id="ou_alice").append_turn(
        user_text="生成一张图",
        assistant_text=f"文件在这里：\n{local_file}",
    )
    db.commit()

    captured: dict[str, object] = {}

    class FakeWorkspace:
        def __init__(self, feishu=None):
            self.feishu = feishu

        def folder_token_from_url(self, url: str | None) -> str | None:
            return None

        async def upload_file_with_fallback(self, *, target_folder: str | None, name: str, content: bytes):
            captured["target_folder"] = target_folder
            captured["name"] = name
            captured["content"] = content
            return {"data": {"file_token": "file_abc"}}, "workspace_root"

    async def fail_openclaw(*args, **kwargs):
        raise AssertionError("upload-to-feishu shortcut should bypass OpenClaw runtime")

    monkeypatch.setattr(bot_commands, "FeishuWorkspaceService", FakeWorkspace)
    monkeypatch.setattr(bot_commands, "_run_openclaw_agent", fail_openclaw)

    reply = asyncio.run(
        bot_commands._chatbot_reply(
            db,
            text="存到飞书里面来",
            chat_id="oc_group",
            chat_type="group",
            sender_open_id="ou_alice",
        )
    )

    assert "已存到飞书" in reply
    assert "[打开文件](" in reply
    assert "/file/file_abc)" in reply
    assert captured["name"] == "wilhelm-ii-early-mercedes-factory-photo.png"
    assert captured["content"] == b"fake-image"


def test_openclaw_agent_session_id_is_isolated_between_group_and_private():
    group_session = bot_commands._openclaw_agent_session_id(
        chat_id="oc_group",
        chat_type="group",
        sender_open_id="ou_alice",
        nonce=0,
    )
    private_session = bot_commands._openclaw_agent_session_id(
        chat_id="oc_p2p",
        chat_type="p2p",
        sender_open_id="ou_alice",
        nonce=0,
    )

    assert group_session != private_session
    assert group_session.startswith("feishu-agent-")
    assert private_session.startswith("feishu-agent-")


def test_extract_openclaw_agent_reply_prefers_final_visible_text():
    payload = {
        "status": "ok",
        "result": {
            "finalAssistantVisibleText": "这是 OpenClaw 的最终可见回复",
            "payloads": [
                {"text": "备用 payload 文本"},
            ],
        },
    }

    assert bot_commands._extract_openclaw_agent_reply(payload) == "这是 OpenClaw 的最终可见回复"


def test_extract_openclaw_agent_reply_falls_back_to_payload_text():
    payload = {
        "status": "ok",
        "result": {
            "payloads": [
                {"text": "第一段 payload 文本"},
            ],
        },
    }

    assert bot_commands._extract_openclaw_agent_reply(payload) == "第一段 payload 文本"


def test_extract_openclaw_agent_reply_returns_chinese_fallback_when_success_has_no_text():
    payload = {
        "status": "success",
        "result": {
            "toolMetas": [{"toolName": "exec", "meta": "did something"}],
        },
        "messages": [
            {"role": "assistant", "content": [{"type": "thinking", "thinking": "done"}]},
        ],
    }

    reply = bot_commands._extract_openclaw_agent_reply(payload)

    assert "系统兜底收尾" in reply
    assert "联系开发者" in reply


def test_extract_openclaw_agent_reply_reports_timeout_fallback():
    payload = {
        "status": "success",
        "result": {
            "finalStatus": "error",
            "timedOut": True,
            "timedOutDuringToolExecution": True,
            "promptError": "request timed out | request timed out",
            "assistantTexts": [],
        },
        "messages": [
            {"role": "assistant", "content": []},
        ],
    }

    reply = bot_commands._extract_openclaw_agent_reply(payload)

    assert "系统兜底收尾" in reply
    assert "超时" in reply
    assert "request timed out" in reply
    assert "联系开发者" in reply


def test_extract_openclaw_agent_reply_reports_rate_limit_fallback():
    payload = {
        "result": {
            "finalStatus": "error",
            "error": "OpenRouter HTTP 429: rate limit exceeded",
            "assistantTexts": [],
        }
    }

    reply = bot_commands._extract_openclaw_agent_reply(payload)

    assert "系统兜底收尾" in reply
    assert "限流" in reply
    assert "429" in reply


def test_resolve_openclaw_command_prefers_bundled_runtime(monkeypatch, tmp_path):
    bundled_node = tmp_path / "node"
    bundled_entry = tmp_path / "index.js"
    bundled_node.write_text("")
    bundled_entry.write_text("")
    monkeypatch.setattr(bot_commands, "_OPENCLAW_BUNDLED_NODE", bundled_node)
    monkeypatch.setattr(bot_commands, "_OPENCLAW_BUNDLED_ENTRYPOINT", bundled_entry)
    monkeypatch.setattr(bot_commands.shutil, "which", lambda _: "/usr/local/bin/openclaw")

    command = bot_commands._resolve_openclaw_command()

    assert command == [str(bundled_node), str(bundled_entry)]


def test_new_session_only_bumps_current_agent_nonce(monkeypatch):
    db = make_db()

    class FakeFeishuClient:
        async def send_text(self, receive_id: str, text: str, receive_id_type: str = "chat_id") -> dict:
            return {"ok": True}

    monkeypatch.setattr(bot_commands, "FeishuClient", FakeFeishuClient)
    preferences = ChatPreferenceService(db)
    before_current = preferences.get_agent_session_nonce(
        chat_id="oc_group",
        chat_type="group",
        sender_open_id="ou_alice",
    )
    before_other = preferences.get_agent_session_nonce(
        chat_id="oc_other",
        chat_type="group",
        sender_open_id="ou_bob",
    )

    asyncio.run(
        bot_commands.handle_bot_text(
            db,
            text="/New session",
            chat_id="oc_group",
            chat_type="group",
            sender_open_id="ou_alice",
        )
    )

    after_current = preferences.get_agent_session_nonce(
        chat_id="oc_group",
        chat_type="group",
        sender_open_id="ou_alice",
    )
    after_other = preferences.get_agent_session_nonce(
        chat_id="oc_other",
        chat_type="group",
        sender_open_id="ou_bob",
    )

    assert after_current == before_current + 1
    assert after_other == before_other


def test_stop_only_bumps_current_agent_nonce(monkeypatch):
    db = make_db()

    class FakeFeishuClient:
        async def send_text(self, receive_id: str, text: str, receive_id_type: str = "chat_id") -> dict:
            return {"ok": True}

    monkeypatch.setattr(bot_commands, "FeishuClient", FakeFeishuClient)
    preferences = ChatPreferenceService(db)
    before_current = preferences.get_agent_session_nonce(
        chat_id="oc_group",
        chat_type="group",
        sender_open_id="ou_alice",
    )
    before_other = preferences.get_agent_session_nonce(
        chat_id="oc_other",
        chat_type="group",
        sender_open_id="ou_bob",
    )

    asyncio.run(
        bot_commands.handle_bot_text(
            db,
            text="/Stop",
            chat_id="oc_group",
            chat_type="group",
            sender_open_id="ou_alice",
        )
    )

    after_current = preferences.get_agent_session_nonce(
        chat_id="oc_group",
        chat_type="group",
        sender_open_id="ou_alice",
    )
    after_other = preferences.get_agent_session_nonce(
        chat_id="oc_other",
        chat_type="group",
        sender_open_id="ou_bob",
    )

    assert after_current == before_current + 1
    assert after_other == before_other


def test_stop_terminates_active_openclaw_process():
    class FakeProcess:
        def __init__(self) -> None:
            self.returncode = None
            self.terminated = False

        def terminate(self) -> None:
            self.terminated = True

    process = FakeProcess()
    bot_commands._ACTIVE_OPENCLAW_PROCESSES["group:oc_group"] = process

    terminated = bot_commands._terminate_openclaw_process_for_session("group:oc_group")

    assert terminated is True
    assert process.terminated is True
    assert "group:oc_group" not in bot_commands._ACTIVE_OPENCLAW_PROCESSES


def test_chatbot_reply_uses_storyboard_breakdown_mode(monkeypatch):
    db = make_db()
    ChatPreferenceService(db).set_assistant_mode(
        chat_id="oc_group",
        chat_type="group",
        sender_open_id="ou_alice",
        mode="storyboard_breakdown",
    )
    db.commit()

    captured: dict[str, object] = {}

    async def fake_storyboard_breakdown_reply(*, query: str, messages: list[dict[str, str]], active_model: str, progress_notifier=None, request_context=None) -> str:
        captured["query"] = query
        captured["messages"] = messages
        captured["active_model"] = active_model
        return "分镜拆解结果"

    monkeypatch.setattr(bot_commands, "_storyboard_breakdown_reply", fake_storyboard_breakdown_reply)

    reply = asyncio.run(
        bot_commands._chatbot_reply(
            db,
            text="请把这份文档拆成分镜",
            chat_id="oc_group",
            chat_type="group",
            sender_open_id="ou_alice",
        )
    )

    assert reply == "分镜拆解结果"
    assert captured["query"] == "请把这份文档拆成分镜"
    assert captured["active_model"] == "deepseek-v4-pro"
    assert "分镜拆解" in captured["messages"][0]["content"]
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


def test_handle_card_action_uses_chat_id_from_card_value(monkeypatch):
    db = make_db()
    sent: list[tuple[str, str, str]] = []

    class FakeFeishuClient:
        async def send_text(self, receive_id: str, text: str, receive_id_type: str = "chat_id") -> dict:
            sent.append((receive_id, text, receive_id_type))
            return {"ok": True}

    monkeypatch.setattr(bot_commands, "FeishuClient", FakeFeishuClient)

    result = asyncio.run(
        bot_commands.handle_card_action(
            db,
            value={
                "action": "assistant.set_mode",
                "mode": "deep_research",
                "chat_id": "oc_group_from_card",
                "chat_type": "group",
                "sender_open_id": "ou_alice",
            },
        )
    )

    assert result == {
        "message": "聊天模式已切换",
        "data": {"chat_id": "oc_group_from_card", "mode": "deep_research"},
    }
    assert sent == [
        (
            "oc_group_from_card",
            "当前会话已切换为：Deep Research。请继续发送研究主题、文档链接或文件。",
            "chat_id",
        )
    ]


def test_handle_card_action_upload_recent_artifact(monkeypatch, tmp_path):
    db = make_db()
    local_file = tmp_path / "artifact.png"
    local_file.write_bytes(b"artifact-bytes")
    ChatMemoryService(db, chat_id="oc_group", chat_type="group", sender_open_id="ou_alice").append_turn(
        user_text="帮我生成",
        assistant_text=f"文件在这里：\n{local_file}",
    )
    db.commit()

    sent: dict[str, object] = {}

    class FakeFeishuClient:
        async def send_card(self, receive_id: str, card: dict, receive_id_type: str = "chat_id") -> dict:
            sent["receive_id"] = receive_id
            sent["card"] = card
            return {"ok": True}

        async def send_text(self, receive_id: str, text: str, receive_id_type: str = "chat_id") -> dict:
            raise AssertionError("upload action should respond with card")

    class FakeWorkspace:
        def __init__(self, feishu=None):
            self.feishu = feishu

        async def upload_file_with_fallback(self, *, target_folder: str | None, name: str, content: bytes):
            return {"data": {"file_token": "file_uploaded"}}, "workspace_root"

        def folder_token_from_url(self, url: str | None) -> str | None:
            return None

    monkeypatch.setattr(bot_commands, "FeishuClient", FakeFeishuClient)
    monkeypatch.setattr(bot_commands, "FeishuWorkspaceService", FakeWorkspace)

    result = asyncio.run(
        bot_commands.handle_card_action(
            db,
            value={
                "action": "assistant.upload_recent_artifact",
                "chat_id": "oc_group",
                "chat_type": "group",
                "sender_open_id": "ou_alice",
            },
            chat_id="oc_group",
        )
    )

    assert result == {"message": "Agent 产物上传已处理", "data": {"chat_id": "oc_group"}}
    assert sent["receive_id"] == "oc_group"
    content = sent["card"]["elements"][0]["content"]
    assert "已存到飞书" in content
    assert "file_uploaded" in content


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
    captured: dict[str, object] = {}

    class FakeWorkspace:
        async def save_markdown_document(self, *, title: str, markdown: str, folder_token: str | None = None):
            captured["folder_token"] = folder_token
            class Result:
                pass

            Result.url = "https://feishu.test/docx/research_doc"
            Result.folder_token = folder_token or "dest_folder_123"
            return Result()

        async def save_text_file(self, *, filename: str, text: str, folder_token: str | None = None):
            captured["raw_filename"] = filename
            captured["raw_text"] = text
            class Result:
                url = "https://feishu.test/file/research_raw"

            return Result()

        def folder_token_from_url(self, url: str | None) -> str | None:
            return "dest_folder_123" if url else None

    async def fake_google_deep_research_report(*, query: str, references: list[dict], progress_notifier=None):
        return bot_commands.DeepResearchResult(
            markdown=f"# 研究报告\n\n{query}",
            raw_text=f"# 研究报告\n\n{query}",
            raw_payload={"outputs": [{"text": f"# 研究报告\n\n{query}"}]},
        )

    monkeypatch.setattr(settings, "google_api_key", "test-google")
    monkeypatch.setattr(settings, "google_deep_research_model", "deep-research-preview-04-2026")
    monkeypatch.setattr(settings, "openrouter_api_key", "")
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(bot_commands, "FeishuWorkspaceService", FakeWorkspace)
    monkeypatch.setattr(bot_commands, "_google_deep_research_report", fake_google_deep_research_report)

    reply = asyncio.run(
        bot_commands._deep_research_reply(
            query="研究 e.go 公司，并保存到 https://feishu.test/drive/folder/dest_folder_123",
            messages=[{"role": "user", "content": "研究 e.go 公司"}],
            active_model="deepseek-v4-pro",
        )
    )

    assert "Gemini Deep Research" in reply
    assert "Fallback 搜索总结" not in reply
    assert "https://feishu.test/docx/research_doc" in reply
    assert "https://feishu.test/file/research_raw" in reply
    assert captured["folder_token"] == "dest_folder_123"
    assert captured["raw_filename"].endswith("_raw.txt")
    assert "=== raw_payload_json ===" in captured["raw_text"]


def test_storyboard_breakdown_reply_saves_doc_and_raw_text(monkeypatch):
    captured: dict[str, object] = {}

    class FakeWorkspace:
        async def ensure_storyboard_workspace_folder(self):
            return {"folder_token": "storyboards"}

        async def save_markdown_document(self, *, title: str, markdown: str, folder_token: str | None = None):
            captured["folder_token"] = folder_token
            captured["markdown"] = markdown
            class Result:
                pass

            Result.url = "https://feishu.test/docx/storyboard_doc"
            Result.folder_token = folder_token or "storyboards"
            return Result()

        async def save_text_file(self, *, filename: str, text: str, folder_token: str | None = None):
            captured["raw_filename"] = filename
            class Result:
                url = "https://feishu.test/file/storyboard_raw"

            return Result()

        def folder_token_from_url(self, url: str | None) -> str | None:
            return None

    async def fake_generate_chat_response(*, model: str, messages: list[dict[str, str]], text: str, assistant_mode: str) -> str:
        captured["model"] = model
        captured["assistant_mode"] = assistant_mode
        return "# 分镜需求\n\n## 镜头 1\n- 内容：开场"

    monkeypatch.setattr(bot_commands, "FeishuWorkspaceService", FakeWorkspace)
    monkeypatch.setattr(bot_commands, "_generate_chat_response", fake_generate_chat_response)

    reply = asyncio.run(
        bot_commands._storyboard_breakdown_reply(
            query="请拆成分镜",
            messages=[{"role": "system", "content": "stub"}, {"role": "user", "content": "原问题"}],
            active_model="deepseek-v4-pro",
        )
    )

    assert "分镜拆解已完成" in reply
    assert "https://feishu.test/docx/storyboard_doc" in reply
    assert "https://feishu.test/file/storyboard_raw" in reply
    assert "# 分镜需求" not in reply
    assert captured["folder_token"] == "storyboards"
    assert captured["assistant_mode"] == "storyboard_breakdown"
    assert captured["raw_filename"].endswith("_raw.txt")


def test_reference_context_block_prefers_doc_text_content():
    block = bot_commands._reference_context_block(
        [
            {
                "type": "feishu_doc",
                "url": "https://feishu.test/docx/doc_abc",
                "text_content": "这是正文第一段\n这是正文第二段",
                "content_json": {"content": [{"type": "text", "text": "不该优先展示的原始 JSON"}]},
            }
        ]
    )

    assert "这是正文第一段" in block
    assert "不该优先展示的原始 JSON" not in block


def test_deep_research_reply_marks_fallback_reason(monkeypatch):
    class FakeWorkspace:
        async def save_markdown_document(self, *, title: str, markdown: str, folder_token: str | None = None):
            class Result:
                pass

            Result.url = "https://feishu.test/docx/research_doc"
            Result.folder_token = folder_token or "deep_research"
            return Result()

        async def save_text_file(self, *, filename: str, text: str, folder_token: str | None = None):
            class Result:
                url = "https://feishu.test/file/research_raw"

            return Result()

    async def fail_google_deep_research_report(*, query: str, references: list[dict], progress_notifier=None):
        raise RuntimeError("gemini unavailable")

    async def fail_openrouter_deep_research_report(*, query: str, references: list[dict], progress_notifier=None):
        raise RuntimeError("openrouter unavailable")

    async def fail_openai_deep_research_report(*, query: str, references: list[dict], progress_notifier=None):
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

    result = bot_commands._format_google_interaction_response(payload)
    assert result.markdown == "# 最终研究报告"
    assert result.raw_text == "# 最终研究报告"


def test_format_google_interaction_response_prefers_outputs_over_steps():
    payload = {
        "status": "completed",
        "outputs": [{"text": "# 完整研究报告\n\n这里是正文"}],
        "steps": [
            {"content": [{"type": "text", "text": "**Sources:**\n1. https://example.com"}]},
        ],
    }

    result = bot_commands._format_google_interaction_response(payload)
    assert result.markdown == "# 完整研究报告\n\n这里是正文"


def test_extract_google_interaction_text_avoids_source_only_block():
    payload = {
        "status": "completed",
        "outputs": [
            {"text": "**Sources:**\n1. https://example.com\n2. https://example.org"},
            {"text": "# 结论摘要\n\n这是完整正文。\n\n## 时间线\n- 2020：开始量产"},
        ],
        "steps": [
            {"content": [{"type": "text", "text": "https://example.com"}]},
        ],
    }

    assert bot_commands._extract_google_interaction_text(payload) == "# 结论摘要\n\n这是完整正文。\n\n## 时间线\n- 2020：开始量产"


def test_extract_google_interaction_text_merges_split_outputs_in_order():
    payload = {
        "status": "completed",
        "outputs": [
            {"text": "# 标题\n\n## 1. 开头\n这里是第一部分。"},
            {"text": ""},
            {"text": "### 4.2 中段\n这里是第二部分。"},
            {"text": "**Sources:**\n1. https://example.com"},
        ],
    }

    assert bot_commands._extract_google_interaction_text(payload) == (
        "# 标题\n\n## 1. 开头\n这里是第一部分。\n\n### 4.2 中段\n这里是第二部分。"
    )


def test_deep_research_prompt_does_not_include_project_context():
    db = make_db()
    project = ProjectService(db).create_project(CreateProjectRequest(name="项目上下文不应出现"))

    prompt = bot_commands._deep_research_prompt(
        query="研究 e.go 公司",
        references=[],
    )

    assert "项目上下文不应出现" not in prompt
    assert "当前飞书项目" not in prompt


def test_handle_bot_text_sends_deep_research_started_hint(monkeypatch):
    db = make_db()
    sent: list[tuple[str, str]] = []

    class FakeFeishuClient:
        async def send_text(self, receive_id: str, text: str, receive_id_type: str = "chat_id") -> dict:
            sent.append((receive_id, text))
            return {"ok": True}

        async def send_card(self, receive_id: str, card: dict, receive_id_type: str = "chat_id") -> dict:
            sent.append((receive_id, card["elements"][0]["content"]))
            return {"ok": True}

    async def fake_google_deep_research_report(*, query: str, references: list[dict], progress_notifier=None):
        if progress_notifier:
            await progress_notifier("Deep Research 仍在进行中：Gemini 当前状态为 `in_progress`，已等待约 5 分钟。若长时间无结果，系统会自动回退到备用路径。")
        return bot_commands.DeepResearchResult(markdown="# 报告正文", raw_text="# 报告正文", raw_payload={"outputs": [{"text": "# 报告正文"}]})

    monkeypatch.setattr(bot_commands, "FeishuClient", FakeFeishuClient)
    monkeypatch.setattr(settings, "google_api_key", "test-google")
    monkeypatch.setattr(settings, "openrouter_api_key", "")
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(bot_commands, "_google_deep_research_report", fake_google_deep_research_report)

    class FakeWorkspace:
        async def save_markdown_document(self, *, title: str, markdown: str, folder_token: str | None = None):
            class Result:
                pass

            Result.url = "https://feishu.test/docx/research_doc"
            Result.folder_token = folder_token or "deep_research"
            return Result()

        async def save_text_file(self, *, filename: str, text: str, folder_token: str | None = None):
            class Result:
                url = "https://feishu.test/file/research_raw"

            return Result()

        def folder_token_from_url(self, url: str | None) -> str | None:
            return None

    monkeypatch.setattr(bot_commands, "FeishuWorkspaceService", FakeWorkspace)

    result = asyncio.run(
        bot_commands.handle_bot_text(
            db,
            text="/Deep Research 研究 e.go 公司",
            chat_id="oc_p2p",
            chat_type="p2p",
            sender_open_id="ou_alice",
        )
    )

    assert result["message"] == "聊天模式已切换并回复"
    assert any("已开始 Deep Research" in message for _, message in sent)
    assert any("仍在进行中" in message for _, message in sent)
    assert any("40 分钟后自动回退" in message for _, message in sent)


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


def test_handle_bot_text_intercepts_query_progress_even_in_agent_mode(monkeypatch):
    db = make_db()
    project = ProjectService(db).create_project(CreateProjectRequest(name="进度项目"))
    project.feishu_app_token = "app_123"
    project.feishu_table_id = "tbl_123"
    project.feishu_folder_token = "fld_123"
    project.workflow_config = {**(project.workflow_config or {}), "chat_id": "oc_group"}
    db.commit()

    ChatPreferenceService(db).set_assistant_mode(
        chat_id="oc_group",
        chat_type="group",
        sender_open_id="ou_alice",
        mode=bot_commands.ASSISTANT_MODE_AGENT,
    )
    db.commit()

    sent_cards: list[dict] = []

    class FakeFeishuClient:
        async def send_text(self, receive_id: str, text: str, receive_id_type: str = "chat_id") -> dict:
            return {"ok": True}

        async def send_card(self, receive_id: str, card: dict, receive_id_type: str = "chat_id") -> dict:
            sent_cards.append(card)
            return {"ok": True}

        async def add_message_reaction(self, message_id: str, emoji_type: str) -> dict:
            return {"data": {"reaction_id": "reaction_1"}}

        async def remove_message_reaction(self, message_id: str, reaction_id: str) -> dict:
            return {"ok": True}

    monkeypatch.setattr(bot_commands, "FeishuClient", FakeFeishuClient)
    async def fake_run_openclaw_agent(*, session_id, message, timeout_seconds=600, session_key=None, model=None):
        assert "当前聊天绑定的项目上下文" in message
        assert "这是默认候选项目，不是唯一真相" in message
        assert "表格链接：" in message
        assert "不确定时先明确追问" in message
        return {"result": {"finalAssistantVisibleText": "Agent 已看到项目链接"}}
    monkeypatch.setattr(bot_commands, "_run_openclaw_agent", fake_run_openclaw_agent)

    result = asyncio.run(
        bot_commands.handle_bot_text(
            db,
            text="查询进度",
            chat_id="oc_group",
            chat_type="group",
            sender_open_id="ou_alice",
        )
    )

    assert result["message"] == "chatbot 已回复"
    assert sent_cards
    assert sent_cards[-1]["elements"][0]["content"] == "Agent 已看到项目链接"


def test_openclaw_agent_message_includes_project_links(monkeypatch):
    db = make_db()
    project = ProjectService(db).create_project(CreateProjectRequest(name="进度项目"))
    project.feishu_app_token = "app_123"
    project.feishu_table_id = "tbl_123"
    project.feishu_folder_token = "fld_123"
    project.workflow_config = {**(project.workflow_config or {}), "chat_id": "oc_group"}
    db.commit()

    ChatPreferenceService(db).set_assistant_mode(
        chat_id="oc_group",
        chat_type="group",
        sender_open_id="ou_alice",
        mode=bot_commands.ASSISTANT_MODE_AGENT,
    )
    db.commit()

    captured: dict[str, str] = {}

    sent_cards: list[dict] = []

    class FakeFeishuClient:
        async def send_text(self, receive_id: str, text: str, receive_id_type: str = "chat_id") -> dict:
            return {"ok": True}

        async def send_card(self, receive_id: str, card: dict, receive_id_type: str = "chat_id") -> dict:
            sent_cards.append(card)
            return {"ok": True}

        async def add_message_reaction(self, message_id: str, emoji_type: str) -> dict:
            return {"data": {"reaction_id": "reaction_1"}}

        async def remove_message_reaction(self, message_id: str, reaction_id: str) -> dict:
            return {"ok": True}

    async def fake_run_openclaw_agent(*, session_id, message, timeout_seconds=600, session_key=None, model=None):
        captured["openclaw_message"] = message
        return {"result": {"finalAssistantVisibleText": "Agent 已看到项目链接"}}

    monkeypatch.setattr(bot_commands, "FeishuClient", FakeFeishuClient)
    monkeypatch.setattr(bot_commands, "_run_openclaw_agent", fake_run_openclaw_agent)

    result = asyncio.run(
        bot_commands.handle_bot_text(
            db,
            text="查询进度",
            chat_id="oc_group",
            chat_type="group",
            sender_open_id="ou_alice",
        )
    )

    assert result["message"] == "chatbot 已回复"
    openclaw_message = captured["openclaw_message"]
    assert "当前聊天绑定的项目上下文" in openclaw_message
    assert "这是默认候选项目，不是唯一真相" in openclaw_message
    assert "表格链接：" in openclaw_message
    assert "不确定时先明确追问" in openclaw_message
    assert "https://open.feishu.cn" not in openclaw_message
    assert "https://" in openclaw_message
    assert sent_cards[-1]["elements"][0]["content"] == "Agent 已看到项目链接"


def test_openclaw_agent_message_includes_document_revision_policy():
    db = make_db()
    memory = ChatMemoryService(db, chat_id="oc_group", chat_type="group", sender_open_id="ou_alice")
    memory.append_turn(
        user_text="帮我改这份文案",
        assistant_text="收到，先看现有版本。",
    )
    db.commit()

    message = bot_commands._compose_openclaw_agent_message(
        session_id="session_without_file",
        memory=memory,
        text="请把第 2 段改得更顺一点",
    )

    assert "文档/文案修订规则（强约束）" in message
    assert "保留原文并加删除线" in message
    assert "用黄色高亮标出新增内容" in message
    assert "如果目标文档已经存在明显修订痕迹" in message
    assert "当前新请求：请把第 2 段改得更顺一点" in message


def test_openclaw_agent_message_includes_video_download_workflow_policy():
    db = make_db()
    memory = ChatMemoryService(db, chat_id="oc_group", chat_type="group", sender_open_id="ou_alice")

    message = bot_commands._compose_openclaw_agent_message(
        session_id="session_without_file",
        memory=memory,
        text="把这个文档划线评论里的 YouTube 视频都下载下来",
    )

    assert "视频下载工作流（Agent 强约束）" in message
    assert "不要自己直接运行 `yt-dlp`" in message
    assert "VideoDownloadService" in message
    assert "AI生成/视频下载" in message
    assert "非文档视频下载" in message
    assert "目标文件夹" in message
    assert "来源文档：<文档原名> <文档链接>" in message
    assert "不得只写 `文档批注引用`" in message
    assert "/open-apis/drive/v1/files/{doc_token}/comments" in message
    assert "docs_link.url" in message
    assert "preview_comment_id" in message


def test_deepseek_agent_prompt_includes_revision_mode_rules():
    prompt = bot_commands._agent_deepseek_system_prompt(session_type="private")

    assert "默认采用修订模式" in prompt
    assert "保留原文并加删除线" in prompt
    assert "黄色高亮" in prompt
    assert "不要继续直接改写；先在聊天里输出一个简短修改计划" in prompt
