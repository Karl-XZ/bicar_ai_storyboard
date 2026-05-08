import asyncio

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.domain.schemas import CreateProjectRequest
from app.models import Base
from app.services import bot_commands
from app.services.chat_memory import ChatMemoryService, resolve_chat_session
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
    assert "不能直接代替用户执行项目操作" in messages[0]["content"]
    assert "当前绑定项目：群聊项目" in messages[0]["content"]
    assert "最近一次模型 smoke test 时间：2026-05-08" in messages[0]["content"]
    assert "xyq_nest_video 已正式接入当前项目" in messages[0]["content"]
    assert "402 Payment Required" in messages[0]["content"]
    assert "deepseek-v4-pro、deepseek-v4-flash" in messages[0]["content"]
    assert "429 insufficient_quota / Too Many Requests" in messages[0]["content"]
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
    assert sent["card"]["elements"][0]["tag"] == "markdown"
    assert "**回复重点**" in sent["card"]["elements"][0]["content"]
