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
    assert "最近一次模型 smoke test 时间：2026-05-07" in messages[0]["content"]
    assert "xyq_nest_video 已正式接入当前项目" in messages[0]["content"]
    assert "402 Payment Required" in messages[0]["content"]
    assert messages[1:] == [
        {"role": "user", "content": "第一问"},
        {"role": "assistant", "content": "第一答"},
        {"role": "user", "content": "第二问"},
    ]

    stored = ChatMemoryService(db, chat_id="oc_group", chat_type="group", sender_open_id="ou_carol").recent_messages()
    assert [item.content for item in stored][-2:] == ["第二问", "第二答"]
