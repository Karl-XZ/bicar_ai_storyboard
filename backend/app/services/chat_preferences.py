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

    def get_agent_runtime(
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
        value = str(preference.agent_runtime or "").strip() if preference else ""
        return value or "codex"

    def get_active_project_id(
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
        value = str(preference.active_project_id or "").strip() if preference else ""
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
                agent_runtime="codex",
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
                agent_runtime="codex",
            )
            self.db.add(preference)
        else:
            preference.session_type = session.session_type
            preference.chat_id = session.chat_id
            preference.sender_open_id = session.sender_open_id
            preference.assistant_mode = mode
        self.db.flush()
        return preference

    def set_active_project_id(
        self,
        *,
        chat_id: str | None,
        chat_type: str | None,
        sender_open_id: str | None,
        project_id: str | None,
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
                chatbot_text_model="",
                agent_runtime="codex",
                active_project_id=project_id,
            )
            self.db.add(preference)
        else:
            preference.session_type = session.session_type
            preference.chat_id = session.chat_id
            preference.sender_open_id = session.sender_open_id
            preference.active_project_id = project_id
        self.db.flush()
        return preference

    def set_agent_runtime(
        self,
        *,
        chat_id: str | None,
        chat_type: str | None,
        sender_open_id: str | None,
        runtime: str,
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
                chatbot_text_model="",
                agent_runtime=runtime,
            )
            self.db.add(preference)
        else:
            preference.session_type = session.session_type
            preference.chat_id = session.chat_id
            preference.sender_open_id = session.sender_open_id
            preference.agent_runtime = runtime
        self.db.flush()
        return preference

    def get_agent_session_nonce(
        self,
        *,
        chat_id: str | None,
        chat_type: str | None,
        sender_open_id: str | None,
    ) -> int:
        session = resolve_chat_session(chat_id=chat_id, chat_type=chat_type, sender_open_id=sender_open_id)
        preference = self.db.scalar(
            select(ChatSessionPreference).where(ChatSessionPreference.session_key == session.session_key)
        )
        return int(preference.agent_session_nonce or 0) if preference else 0

    def bump_agent_session_nonce(
        self,
        *,
        chat_id: str | None,
        chat_type: str | None,
        sender_open_id: str | None,
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
                chatbot_text_model="",
                agent_runtime="codex",
                agent_session_nonce=1,
            )
            self.db.add(preference)
        else:
            preference.session_type = session.session_type
            preference.chat_id = session.chat_id
            preference.sender_open_id = session.sender_open_id
            preference.agent_session_nonce = int(preference.agent_session_nonce or 0) + 1
        self.db.flush()
        return preference
