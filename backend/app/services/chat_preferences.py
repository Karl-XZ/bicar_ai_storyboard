from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.chat_session_preference import ChatSessionPreference
from app.services.chat_memory import resolve_chat_session


class ChatPreferenceService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get_chatbot_text_model(
        self,
        *,
        chat_id: str | None,
        chat_type: str | None,
        sender_open_id: str | None,
    ) -> str | None:
        session = resolve_chat_session(chat_id=chat_id, chat_type=chat_type, sender_open_id=sender_open_id)
        preference = self.db.scalar(
            select(ChatSessionPreference).where(ChatSessionPreference.session_key == session.session_key)
        )
        if not preference:
            return None
        value = str(preference.chatbot_text_model or "").strip()
        return value or None

    def get_assistant_mode(
        self,
        *,
        chat_id: str | None,
        chat_type: str | None,
        sender_open_id: str | None,
    ) -> str:
        session = resolve_chat_session(chat_id=chat_id, chat_type=chat_type, sender_open_id=sender_open_id)
        preference = self.db.scalar(
            select(ChatSessionPreference).where(ChatSessionPreference.session_key == session.session_key)
        )
        return preference.assistant_mode if preference and preference.assistant_mode else "chat"

    def set_chatbot_text_model(
        self,
        *,
        chat_id: str | None,
        chat_type: str | None,
        sender_open_id: str | None,
        model: str,
    ) -> ChatSessionPreference:
        session = resolve_chat_session(chat_id=chat_id, chat_type=chat_type, sender_open_id=sender_open_id)
        preference = self.db.scalar(
            select(ChatSessionPreference).where(ChatSessionPreference.session_key == session.session_key)
        )
        if not preference:
            preference = ChatSessionPreference(
                session_key=session.session_key,
                session_type=session.session_type,
                chat_id=session.chat_id,
                sender_open_id=session.sender_open_id,
                assistant_mode="chat",
                chatbot_text_model=model,
            )
            self.db.add(preference)
        else:
            preference.session_type = session.session_type
            preference.chat_id = session.chat_id
            preference.sender_open_id = session.sender_open_id
            preference.chatbot_text_model = model
        self.db.flush()
        return preference

    def set_assistant_mode(
        self,
        *,
        chat_id: str | None,
        chat_type: str | None,
        sender_open_id: str | None,
        mode: str,
    ) -> ChatSessionPreference:
        session = resolve_chat_session(chat_id=chat_id, chat_type=chat_type, sender_open_id=sender_open_id)
        preference = self.db.scalar(
            select(ChatSessionPreference).where(ChatSessionPreference.session_key == session.session_key)
        )
        if not preference:
            preference = ChatSessionPreference(
                session_key=session.session_key,
                session_type=session.session_type,
                chat_id=session.chat_id,
                sender_open_id=session.sender_open_id,
                assistant_mode=mode,
                chatbot_text_model="",
            )
            self.db.add(preference)
        else:
            preference.session_type = session.session_type
            preference.chat_id = session.chat_id
            preference.sender_open_id = session.sender_open_id
            preference.assistant_mode = mode
        self.db.flush()
        return preference
