from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class ChatSessionPreference(TimestampMixin, Base):
    __tablename__ = "chat_session_preferences"

    session_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    session_type: Mapped[str] = mapped_column(String(32), nullable=False)
    chat_id: Mapped[str | None] = mapped_column(String(255), index=True)
    sender_open_id: Mapped[str | None] = mapped_column(String(255), index=True)
    assistant_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="chat")
    chatbot_text_model: Mapped[str] = mapped_column(String(255), nullable=False)
    agent_runtime: Mapped[str] = mapped_column(String(32), nullable=False, default="codex")
    agent_session_nonce: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
