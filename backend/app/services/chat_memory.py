from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.chat_message import ChatMessage


@dataclass(frozen=True)
class ChatSession:
    session_key: str
    session_type: str
    chat_id: str | None
    sender_open_id: str | None


class ChatMemoryService:
    def __init__(self, db: Session, *, chat_id: str | None, chat_type: str | None, sender_open_id: str | None) -> None:
        self.db = db
        self.session = resolve_chat_session(chat_id=chat_id, chat_type=chat_type, sender_open_id=sender_open_id)

    @property
    def session_key(self) -> str:
        return self.session.session_key

    @property
    def session_type(self) -> str:
        return self.session.session_type

    def recent_messages(self, *, rounds: int | None = None) -> list[ChatMessage]:
        limit = max((rounds or settings.chatbot_memory_rounds) * 2, 0)
        if limit == 0:
            return []
        statement = (
            select(ChatMessage)
            .where(ChatMessage.session_key == self.session.session_key)
            .order_by(ChatMessage.id.desc())
            .limit(limit)
        )
        messages = list(self.db.scalars(statement))
        messages.reverse()
        return messages

    def as_llm_messages(self, *, rounds: int | None = None) -> list[dict[str, str]]:
        return [{"role": item.role, "content": item.content} for item in self.recent_messages(rounds=rounds)]

    def append_turn(self, *, user_text: str, assistant_text: str) -> None:
        normalized_user = user_text.strip()
        normalized_assistant = assistant_text.strip()
        if not normalized_user or not normalized_assistant:
            return
        self.db.add(
            ChatMessage(
                session_key=self.session.session_key,
                session_type=self.session.session_type,
                chat_id=self.session.chat_id,
                sender_open_id=self.session.sender_open_id,
                role="user",
                content=normalized_user,
            )
        )
        self.db.add(
            ChatMessage(
                session_key=self.session.session_key,
                session_type=self.session.session_type,
                chat_id=self.session.chat_id,
                sender_open_id=None,
                role="assistant",
                content=normalized_assistant,
            )
        )
        self.db.flush()
        self.prune()

    def prune(self, *, rounds: int | None = None) -> None:
        max_messages = max((rounds or settings.chatbot_memory_rounds) * 2, 0)
        if max_messages <= 0:
            return
        stale_statement = (
            select(ChatMessage.id)
            .where(ChatMessage.session_key == self.session.session_key)
            .order_by(ChatMessage.id.desc())
            .offset(max_messages)
        )
        stale_ids = list(self.db.scalars(stale_statement))
        if stale_ids:
            self.db.execute(delete(ChatMessage).where(ChatMessage.id.in_(stale_ids)))

    def clear(self) -> int:
        result = self.db.execute(delete(ChatMessage).where(ChatMessage.session_key == self.session.session_key))
        self.db.flush()
        return int(result.rowcount or 0)


def resolve_chat_session(*, chat_id: str | None, chat_type: str | None, sender_open_id: str | None) -> ChatSession:
    normalized_chat_type = (chat_type or "").strip().lower()
    if normalized_chat_type in {"p2p", "private"}:
        identity = sender_open_id or chat_id or settings.feishu_default_chat_id or "default"
        return ChatSession(
            session_key=f"private:{identity}",
            session_type="private",
            chat_id=chat_id,
            sender_open_id=sender_open_id,
        )

    identity = chat_id or settings.feishu_default_chat_id or sender_open_id or "default"
    return ChatSession(
        session_key=f"group:{identity}",
        session_type="group",
        chat_id=chat_id,
        sender_open_id=sender_open_id,
    )
