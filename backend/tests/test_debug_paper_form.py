from __future__ import annotations

import asyncio
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routes import webhooks
from app.core.config import settings
from app.db.session import get_db
from app.main import app
from app.models import Base
from app.services import bot_commands
from app.services.debug_paper_form import DebugPaperFormService, DebugPaperWorkspace, STATUS_DONE, STATUS_RUNNING
from scripts import run_feishu_ws


class FakeFeishuForDebugPaper:
    def __init__(self) -> None:
        self.created_fields: list[dict] = []
        self.updated_form_fields: list[tuple[str, dict]] = []
        self.updated_records: list[list[dict]] = []
        self.updated_permissions: list[tuple[str, str, dict]] = []
        self.subscriptions: list[tuple[str, str]] = []
        self.records = {
            "rec_001": {
                "record_id": "rec_001",
                "fields": {
                    "副本文件名": "客户A调试纸",
                    "处理状态": "",
                },
            }
        }

    async def list_folder_items(self, folder_token, *, page_size=200, page_token=None):
        return {"data": {"items": [], "has_more": False}}

    async def list_tables(self, app_token):
        return {"data": {"items": []}}

    async def create_table(self, app_token, table_name, fields):
        return {"data": {"table": {"table_id": "tbl_debug"}}}

    async def list_fields(self, app_token, table_id):
        return {"data": {"items": []}}

    async def create_field(self, app_token, table_id, field):
        self.created_fields.append(field)
        return {"data": {"field": field}}

    async def list_views(self, app_token, table_id):
        return {"data": {"items": [], "has_more": False}}

    async def create_view(self, app_token, table_id, *, view_name, view_type):
        assert view_type == "form"
        assert view_name == "创建调试纸副本文档"
        return {"data": {"view": {"view_id": "viw_debug"}}}

    async def update_form_metadata(self, app_token, table_id, form_id, payload):
        assert payload["shared"] is True
        assert payload["name"] == "创建调试纸副本文档"
        assert "只需要填这一项" in payload["description"]
        assert "保存位置：" in payload["description"]
        assert "提交后查看新文档链接：" in payload["description"]
        return {"data": {"form": {"shared_url": "https://feishu.cn/share/form_debug"}}}

    async def list_form_fields(self, app_token, table_id, form_id):
        return {
            "data": {
                "items": [
                    {"field_id": "fld_name", "title": "副本文件名", "required": False, "visible": True, "description": None},
                    {"field_id": "fld_status", "title": "处理状态", "required": False, "visible": True, "description": None},
                    {"field_id": "fld_link", "title": "副本位置", "required": False, "visible": True, "description": None},
                    {"field_id": "fld_time", "title": "处理时间", "required": False, "visible": True, "description": None},
                    {"field_id": "fld_log", "title": "日志", "required": False, "visible": True, "description": None},
                ]
            }
        }

    async def update_form_field(self, app_token, table_id, form_id, field_id, payload):
        self.updated_form_fields.append((field_id, payload))
        return {"data": {"field": payload}}

    async def subscribe_file_events(self, file_token, file_type="bitable"):
        self.subscriptions.append((file_token, file_type))
        return {"data": {}}

    async def update_permission_public(self, token, *, file_type, payload):
        self.updated_permissions.append((token, file_type, payload))
        return {"data": {"permission_public": payload}}

    async def search_records(self, app_token, table_id, payload):
        return {"data": {"items": list(self.records.values())}}

    async def batch_update_records(self, app_token, table_id, records):
        self.updated_records.append(records)
        for record in records:
            self.records[record["record_id"]]["fields"].update(record["fields"])
        return {"data": {"records": records}}


class FakeWorkspaceForDebugPaper:
    def __init__(self) -> None:
        self.copied: list[tuple[str, str, str]] = []

    async def ensure_workspace_subfolder(self, name):
        assert name == "调试纸"
        return {"folder_token": "fld_debug", "folder_url": "https://feishu.cn/drive/folder/fld_debug"}

    async def create_bitable_with_fallback(self, *, folder_token, name):
        assert folder_token == "fld_debug"
        assert name == "调试纸副本申请"
        return {"data": {"token": "app_debug", "url": "https://feishu.cn/base/app_debug"}}, folder_token

    def folder_token_from_url(self, url):
        return "fld_target"

    async def copy_local_docx_to_workspace(self, *, source_path, title, folder_token):
        self.copied.append((source_path, title, folder_token))
        return SimpleNamespace(document_id="file_debug", url="https://feishu.cn/file/file_debug")


def make_db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_debug_paper_form_ensure_workspace_creates_feishu_form(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_local_root", str(tmp_path))
    monkeypatch.setattr(settings, "debug_paper_target_folder_url", "")
    feishu = FakeFeishuForDebugPaper()
    service = DebugPaperFormService(feishu=feishu, workspace=FakeWorkspaceForDebugPaper())

    workspace = asyncio.run(service.ensure_workspace())

    assert workspace == DebugPaperWorkspace(
        folder_token="fld_debug",
        folder_url="https://feishu.cn/drive/folder/fld_debug",
        target_folder_url="https://feishu.cn/drive/folder/fld_debug",
        app_token="app_debug",
        table_id="tbl_debug",
        table_url="https://ocnwptzvwvt6.feishu.cn/base/app_debug?table=tbl_debug",
        form_id="viw_debug",
        form_url="https://feishu.cn/share/form_debug",
    )
    assert {field["field_name"] for field in feishu.created_fields} == {"副本文件名", "处理状态", "副本位置", "处理时间", "日志"}
    assert feishu.subscriptions == [("app_debug", "bitable")]
    assert feishu.updated_permissions == [
        (
            "app_debug",
            "bitable",
            {
                "link_share_entity": "tenant_editable",
                "share_entity": "same_tenant",
                "external_access": False,
                "invite_external": False,
                "comment_entity": "anyone_can_edit",
            },
        )
    ]
    updated_by_field = {field_id: payload for field_id, payload in feishu.updated_form_fields}
    assert updated_by_field["fld_name"]["title"] == "新文档名称"
    assert "不写 .docx" in updated_by_field["fld_name"]["description"]
    assert updated_by_field["fld_name"]["required"] is True
    assert updated_by_field["fld_name"]["visible"] is True
    assert updated_by_field["fld_status"]["visible"] is False
    assert updated_by_field["fld_link"]["title"] == "生成后的文档打开链接（提交后自动填写）"
    assert "不用填写" in updated_by_field["fld_link"]["description"]
    assert updated_by_field["fld_link"]["required"] is False
    assert updated_by_field["fld_link"]["visible"] is True
    assert updated_by_field["fld_time"]["visible"] is False
    assert updated_by_field["fld_log"]["visible"] is False


def test_debug_paper_form_process_record_copies_template_and_updates_record(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_local_root", str(tmp_path))
    monkeypatch.setattr(settings, "debug_paper_target_folder_url", "https://feishu.cn/drive/folder/fld_target")
    monkeypatch.setattr(settings, "debug_paper_template_path", "local_storage/templates/调试纸CN.docx")
    feishu = FakeFeishuForDebugPaper()
    workspace_service = FakeWorkspaceForDebugPaper()
    service = DebugPaperFormService(feishu=feishu, workspace=workspace_service)
    workspace = asyncio.run(service.ensure_workspace())

    asyncio.run(service.process_record_by_table(app_token=workspace.app_token, table_id=workspace.table_id, record_id="rec_001"))

    assert workspace_service.copied == [("local_storage/templates/调试纸CN.docx", "客户A调试纸", "fld_debug")]
    assert feishu.updated_permissions[-1] == (
        "file_debug",
        "file",
        {
            "link_share_entity": "tenant_editable",
            "share_entity": "same_tenant",
            "external_access": False,
            "invite_external": False,
            "comment_entity": "anyone_can_edit",
        },
    )
    statuses = [records[0]["fields"]["处理状态"] for records in feishu.updated_records]
    assert statuses == [STATUS_RUNNING, STATUS_DONE]
    assert feishu.records["rec_001"]["fields"]["副本位置"]["link"] == "https://feishu.cn/file/file_debug"


def test_bitable_trigger_routes_debug_paper_before_video_download(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_local_root", str(tmp_path))
    db = make_db()

    def override_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db] = override_db
    monkeypatch.setattr(
        webhooks,
        "parse_event_body",
        lambda body, encrypt_key: {
            "event": {
                "app_token": "app_debug",
                "table_id": "tbl_debug",
                "record_id": "rec_001",
            }
        },
    )
    calls = []

    class FakeDebugPaperFormService:
        async def handles_table(self, app_token, table_id):
            return app_token == "app_debug" and table_id == "tbl_debug"

        async def process_record_by_table(self, *, app_token, table_id, record_id):
            calls.append((app_token, table_id, record_id))

    class FakeVideoDownloadService:
        async def handles_table(self, app_token, table_id):
            raise AssertionError("调试纸表格事件不应继续落到视频下载工作流")

    monkeypatch.setattr(webhooks, "DebugPaperFormService", FakeDebugPaperFormService)
    monkeypatch.setattr(webhooks, "VideoDownloadService", FakeVideoDownloadService)

    client = TestClient(app)
    response = client.post("/webhooks/feishu/bitable-trigger", json={})

    assert response.status_code == 202
    assert response.json()["data"]["processed"] == 1
    assert calls == [("app_debug", "tbl_debug", "rec_001")]
    app.dependency_overrides.clear()


def test_feishu_events_routes_bitable_record_changed_action_list(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_local_root", str(tmp_path))
    db = make_db()

    def override_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db] = override_db
    monkeypatch.setattr(
        webhooks,
        "parse_event_body",
        lambda body, encrypt_key: {
            "schema": "2.0",
            "header": {"event_type": "drive.file.bitable_record_changed_v1"},
            "event": {
                "file_token": "app_debug",
                "file_type": "bitable",
                "table_id": "tbl_debug",
                "action_list": [
                    {"action": "record_added", "record_id": "rec_001"},
                    {"action": "record_deleted", "record_id": "rec_deleted"},
                ],
            },
        },
    )
    calls = []

    class FakeDebugPaperFormService:
        async def handles_table(self, app_token, table_id):
            return app_token == "app_debug" and table_id == "tbl_debug"

        async def process_record_by_table(self, *, app_token, table_id, record_id):
            calls.append((app_token, table_id, record_id))

    monkeypatch.setattr(webhooks, "DebugPaperFormService", FakeDebugPaperFormService)

    client = TestClient(app)
    response = client.post("/webhooks/feishu/events", json={})

    assert response.status_code == 202
    assert response.json()["data"]["processed"] == 1
    assert calls == [("app_debug", "tbl_debug", "rec_001")]
    app.dependency_overrides.clear()


def test_feishu_ws_routes_debug_paper_record_changed(monkeypatch):
    calls = []

    class FakeDB:
        def close(self):
            pass

    class FakeProjectService:
        def __init__(self, db):
            pass

        def find_by_feishu_table(self, app_token, table_id):
            return None

    class FakeDebugPaperFormService:
        async def handles_table(self, app_token, table_id):
            return app_token == "app_debug" and table_id == "tbl_debug"

        async def process_record_by_table(self, *, app_token, table_id, record_id):
            calls.append((app_token, table_id, record_id))

    class FakeVideoDownloadService:
        async def handles_table(self, app_token, table_id):
            raise AssertionError("调试纸表格事件不应落到视频下载工作流")

    monkeypatch.setattr(run_feishu_ws, "SessionLocal", lambda: FakeDB())
    monkeypatch.setattr(run_feishu_ws, "ProjectService", FakeProjectService)
    monkeypatch.setattr(run_feishu_ws, "DebugPaperFormService", FakeDebugPaperFormService)
    monkeypatch.setattr(run_feishu_ws, "VideoDownloadService", FakeVideoDownloadService)
    run_feishu_ws._pending_record_runs.clear()

    run_feishu_ws._run_single_record_changed("app_debug", "tbl_debug", "rec_001")

    assert calls == [("app_debug", "tbl_debug", "rec_001")]
    assert run_feishu_ws._pending_record_runs == {}


def test_debug_paper_qr_command_returns_feishu_form_entry(monkeypatch):
    db = make_db()
    sent_cards = []

    class FakeDebugPaperFormService:
        async def ensure_workspace(self):
            return DebugPaperWorkspace(
                folder_token="fld_debug",
                folder_url="https://feishu.cn/drive/folder/fld_debug",
                target_folder_url="https://feishu.cn/drive/folder/fld_debug",
                app_token="app_debug",
                table_id="tbl_debug",
                table_url="https://ocnwptzvwvt6.feishu.cn/base/app_debug?table=tbl_debug",
                form_id="viw_debug",
                form_url="https://feishu.cn/share/form_debug",
            )

    class FakeFeishuClient:
        async def send_card(self, receive_id, card, receive_id_type="chat_id"):
            sent_cards.append(card)
            return {"ok": True}

        async def send_text(self, receive_id, text, receive_id_type="chat_id"):
            return {"ok": True}

    monkeypatch.setattr(bot_commands, "DebugPaperFormService", FakeDebugPaperFormService)
    monkeypatch.setattr(bot_commands, "FeishuClient", FakeFeishuClient)

    result = asyncio.run(
        bot_commands.handle_bot_text(
            db,
            text="/调试纸二维码",
            chat_id="oc_group",
            chat_type="group",
            sender_open_id="ou_alice",
        )
    )

    assert result["message"] == "调试纸二维码入口已发送"
    card_text = str(sent_cards[0])
    assert "https://feishu.cn/share/form_debug" in card_text
    assert "飞书原生表单" in card_text
    assert "新文档名称" in card_text
    assert "新文档保存文件夹" in card_text
    assert "生成后的文档打开链接" in card_text
    assert "第二行由后端自动维护" in card_text
    assert "https://feishu.cn/drive/folder/fld_debug" in card_text
    assert "/tools/debug-paper-copy" not in card_text
