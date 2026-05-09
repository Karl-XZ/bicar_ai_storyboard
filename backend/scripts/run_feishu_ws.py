from __future__ import annotations

import asyncio
import json
import logging
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import lark_oapi as lark  # noqa: E402
from lark_oapi.event.callback.model.p2_card_action_trigger import (  # noqa: E402
    CallBackToast,
    P2CardActionTriggerResponse,
)

from app.api.routes.webhooks import _message_text  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.services.bot_commands import handle_bot_text, handle_card_action  # noqa: E402
from app.services.feishu_storyboard import FeishuStoryboardService  # noqa: E402
from app.services.projects import ProjectService  # noqa: E402


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("feishu-ws")
message_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="feishu-msg")
bitable_executor = ThreadPoolExecutor(max_workers=12, thread_name_prefix="feishu-bitable")
_pending_record_runs: dict[tuple[str, str, str], bool] = {}
_pending_record_lock = Lock()


def main() -> int:
    if not settings.feishu_app_id or not settings.feishu_app_secret:
        logger.error("FEISHU_APP_ID and FEISHU_APP_SECRET are required")
        return 2

    handler = (
        lark.EventDispatcherHandler.builder(settings.feishu_encrypt_key, settings.feishu_verification_token, lark.LogLevel.INFO)
        .register_p2_im_message_receive_v1(on_message_receive)
        .register_p2_card_action_trigger(on_card_action)
        .register_p2_drive_file_bitable_record_changed_v1(on_bitable_record_changed)
        .build()
    )
    client = lark.ws.Client(
        app_id=settings.feishu_app_id,
        app_secret=settings.feishu_app_secret,
        event_handler=handler,
        log_level=lark.LogLevel.INFO,
    )
    logger.info("starting Feishu long-connection client")
    client.start()
    return 0


def on_message_receive(data) -> None:
    sender = data.event.sender if data.event else None
    sender_id = sender.sender_id if sender else None
    message = data.event.message
    message_payload = {
        "content": message.content,
        "chat_id": message.chat_id,
        "mentions": [
            {"key": mention.key, "name": mention.name}
            for mention in (message.mentions or [])
        ],
    }
    text = _message_text(message_payload)
    chat_id = message.chat_id or settings.feishu_default_chat_id
    chat_type = message.chat_type
    sender_open_id = sender_id.open_id if sender_id else None
    logger.info("received message event chat_id=%s chat_type=%s sender_open_id=%s text=%s", chat_id, chat_type, sender_open_id, text)
    message_executor.submit(_run_bot_text, text, chat_id, chat_type, sender_open_id)


def on_card_action(data) -> P2CardActionTriggerResponse:
    value: dict[str, Any] = (data.event.action.value if data.event and data.event.action else {}) or {}
    chat_id = data.event.context.open_chat_id if data.event and data.event.context else settings.feishu_default_chat_id
    logger.info("received card action chat_id=%s value=%s", chat_id, json.dumps(value, ensure_ascii=False))
    message_executor.submit(_run_card_action, value, chat_id)
    response = P2CardActionTriggerResponse()
    response.toast = CallBackToast({"type": "info", "content": "已收到，正在处理"})
    return response


def on_bitable_record_changed(data) -> None:
    event = data.event
    app_token = event.file_token if event else None
    table_id = event.table_id if event else None
    actions = [
        {"record_id": action.record_id, "action": action.action}
        for action in (event.action_list or [])
    ] if event else []
    logger.info(
        "received bitable record changed app_token=%s table_id=%s actions=%s",
        app_token,
        table_id,
        json.dumps(actions, ensure_ascii=False),
    )
    _dispatch_bitable_record_changed(app_token, table_id, actions)


def _run_bot_text(text: str, chat_id: str | None, chat_type: str | None, sender_open_id: str | None) -> None:
    db = SessionLocal()
    try:
        result = asyncio.run(handle_bot_text(db, text=text, chat_id=chat_id, chat_type=chat_type, sender_open_id=sender_open_id))
        logger.info("message handled result=%s", result)
    except Exception:
        logger.exception("message handling failed")
    finally:
        db.close()


def _run_card_action(value: dict[str, Any], chat_id: str | None) -> None:
    db = SessionLocal()
    try:
        result = asyncio.run(handle_card_action(db, value=value, chat_id=chat_id))
        logger.info("card action handled result=%s", result)
    except Exception:
        logger.exception("card action handling failed")
    finally:
        db.close()


def _dispatch_bitable_record_changed(app_token: str | None, table_id: str | None, actions: list[dict[str, str]]) -> None:
    if not app_token or not table_id:
        logger.warning("bitable event missing app_token or table_id")
        return

    changed_record_ids = [
        action.get("record_id")
        for action in actions
        if action.get("record_id") and action.get("action") != "record_deleted"
    ]
    if changed_record_ids:
        for record_id in changed_record_ids:
            _submit_record_processing(app_token, table_id, str(record_id))
        return
    bitable_executor.submit(_run_bitable_table_sync, app_token, table_id)


def _submit_record_processing(app_token: str, table_id: str, record_id: str) -> None:
    key = (app_token, table_id, record_id)
    with _pending_record_lock:
        if key in _pending_record_runs:
            _pending_record_runs[key] = True
            logger.info("coalesced duplicate bitable event app_token=%s table_id=%s record_id=%s", app_token, table_id, record_id)
            return
        _pending_record_runs[key] = False
    bitable_executor.submit(_run_single_record_changed, app_token, table_id, record_id)


def _run_bitable_table_sync(app_token: str, table_id: str) -> None:
    db = SessionLocal()
    try:
        project = ProjectService(db).find_by_feishu_table(app_token, table_id)
        if not project:
            logger.warning("bitable project not found app_token=%s table_id=%s", app_token, table_id)
            return
        asyncio.run(FeishuStoryboardService(db).sync_from_feishu(project))
        logger.info("bitable table synced project_id=%s", project.id)
    except Exception:
        logger.exception("bitable table sync failed")
    finally:
        db.close()


def _run_single_record_changed(app_token: str, table_id: str, record_id: str) -> None:
    key = (app_token, table_id, record_id)
    while True:
        db = SessionLocal()
        try:
            project = ProjectService(db).find_by_feishu_table(app_token, table_id)
            if not project:
                logger.warning("bitable project not found app_token=%s table_id=%s", app_token, table_id)
                return

            service = FeishuStoryboardService(db)
            asyncio.run(service.sync_from_feishu(project))
            response = asyncio.run(service.feishu.search_records(app_token, table_id, {}))
            records = response.get("data", {}).get("items", [])
            target = next((record for record in records if record.get("record_id") == record_id), None)
            if target:
                asyncio.run(service.process_record_status(project=project, record=target))
                logger.info("bitable record processed project_id=%s record_id=%s", project.id, record_id)
            else:
                logger.info("bitable record missing after sync project_id=%s record_id=%s", project.id, record_id)
        except Exception:
            logger.exception("bitable record handling failed record_id=%s", record_id)
        finally:
            db.close()

        with _pending_record_lock:
            rerun = _pending_record_runs.get(key, False)
            if rerun:
                _pending_record_runs[key] = False
                logger.info("rerunning coalesced bitable event app_token=%s table_id=%s record_id=%s", app_token, table_id, record_id)
                continue
            _pending_record_runs.pop(key, None)
            break


if __name__ == "__main__":
    raise SystemExit(main())
