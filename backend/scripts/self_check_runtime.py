from __future__ import annotations

import sys
import time
from pathlib import Path

import httpx
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import settings
from app.models.chat_session_preference import ChatSessionPreference
from app.services.chat_preferences import ChatPreferenceService


def _wait_for_http(url: str, expected_status: str, *, attempts: int = 20, delay: float = 0.5) -> None:
    last_error: str | None = None
    for _ in range(attempts):
        try:
            response = httpx.get(url, timeout=3.0)
            response.raise_for_status()
            status = response.json().get("status")
            if status == expected_status:
                return
            last_error = f"unexpected status payload: {response.text}"
        except Exception as exc:  # pragma: no cover - deployment-time diagnostic
            last_error = str(exc)
        time.sleep(delay)
    raise RuntimeError(f"HTTP self-check failed for {url}: {last_error or 'unknown error'}")


def main() -> None:
    engine = create_engine(settings.database_url)
    SessionLocal = sessionmaker(bind=engine)

    with engine.connect() as conn:
        columns = [
            row[0]
            for row in conn.execute(
                text(
                    """
                    select column_name
                    from information_schema.columns
                    where table_name = 'chat_session_preferences'
                    order by ordinal_position
                    """
                )
            ).fetchall()
        ]
    if "active_project_id" not in columns:
        raise RuntimeError("chat_session_preferences.active_project_id is missing in runtime database")

    with SessionLocal() as db:
        prefs = ChatPreferenceService(db)
        prefs.set_assistant_mode(
            chat_id="oc_selfcheck",
            chat_type="group",
            sender_open_id="ou_selfcheck",
            mode="agent",
        )
        prefs.set_agent_runtime(
            chat_id="oc_selfcheck",
            chat_type="group",
            sender_open_id="ou_selfcheck",
            runtime="codex",
        )
        prefs.set_active_project_id(
            chat_id="oc_selfcheck",
            chat_type="group",
            sender_open_id="ou_selfcheck",
            project_id="00000000-0000-0000-0000-000000000000",
        )
        prefs.bump_agent_session_nonce(
            chat_id="oc_selfcheck",
            chat_type="group",
            sender_open_id="ou_selfcheck",
        )
        db.commit()

        stored = db.execute(
            text(
                """
                select active_project_id, assistant_mode, agent_runtime, agent_session_nonce
                from chat_session_preferences
                where session_key = :session_key
                """
            ),
            {"session_key": "group:oc_selfcheck"},
        ).first()
        if stored is None:
            raise RuntimeError("chat_session_preferences row was not created during self-check")

        row = db.query(ChatSessionPreference).filter_by(session_key="group:oc_selfcheck").one()
        if row.active_project_id != "00000000-0000-0000-0000-000000000000":
            raise RuntimeError("active_project_id write/read self-check failed")
        if row.assistant_mode != "agent":
            raise RuntimeError("assistant_mode write/read self-check failed")
        if row.agent_runtime != "codex":
            raise RuntimeError("agent_runtime write/read self-check failed")
        if row.agent_session_nonce < 1:
            raise RuntimeError("agent_session_nonce self-check failed")

        db.delete(row)
        db.commit()

    _wait_for_http("http://127.0.0.1:8000/healthz", "ok")
    _wait_for_http("http://127.0.0.1:8000/readyz", "ready")

    print("Runtime self-check passed.")


if __name__ == "__main__":
    main()
