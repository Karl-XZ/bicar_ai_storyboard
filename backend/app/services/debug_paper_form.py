from __future__ import annotations

import json
import re
import asyncio
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.adapters.feishu import FeishuClient
from app.core.config import settings
from app.services.feishu_workspace import FeishuWorkspaceService

DEBUG_PAPER_APP_NAME = "调试纸副本申请"
DEBUG_PAPER_TABLE_NAME = "申请记录"
DEBUG_PAPER_FORM_NAME = "创建调试纸副本文档"
DEBUG_PAPER_FORM_DESCRIPTION = "请填写新文档名称。提交后，哔车AI助手会自动复制一份《调试纸CN.docx》，并把新文档链接写回处理记录表。你只需要填这一项。"
DEBUG_PAPER_FORM_INPUT_TITLE = "新文档名称"
DEBUG_PAPER_FORM_INPUT_DESCRIPTION = "例如：调试纸-张三-20260622。不写 .docx 也可以，系统会自动补全。"
DEBUG_PAPER_FORM_RESULT_TITLE = "生成后的文档打开链接（提交后自动填写）"
DEBUG_PAPER_FORM_RESULT_DESCRIPTION = "不用填写。提交后稍等片刻，机器人会把新建文档的打开链接自动写到这里；如果没显示，请刷新填写记录。"
DEBUG_PAPER_COMPANY_EDIT_PERMISSION = {
    "link_share_entity": "tenant_editable",
    "share_entity": "same_tenant",
    "external_access": False,
    "invite_external": False,
    "comment_entity": "anyone_can_edit",
}
STATUS_PENDING = "待处理"
STATUS_RUNNING = "处理中"
STATUS_DONE = "已完成"
STATUS_FAILED = "处理失败"


@dataclass(frozen=True)
class DebugPaperWorkspace:
    folder_token: str
    folder_url: str
    target_folder_url: str
    app_token: str
    table_id: str
    table_url: str
    form_id: str
    form_url: str


class DebugPaperFormService:
    def __init__(
        self,
        *,
        feishu: FeishuClient | None = None,
        workspace: FeishuWorkspaceService | None = None,
    ) -> None:
        self.feishu = feishu or FeishuClient()
        self.workspace = workspace or FeishuWorkspaceService(self.feishu)

    async def ensure_workspace(self) -> DebugPaperWorkspace:
        folder = await self.workspace.ensure_workspace_subfolder("调试纸")
        folder_token = str(folder.get("folder_token") or "")
        folder_url = str(folder.get("folder_url") or self._drive_folder_url(folder_token))
        metadata = self._load_metadata()
        if metadata and metadata.get("folder_token") == folder_token and metadata.get("app_token") and metadata.get("table_id"):
            current = DebugPaperWorkspace(
                folder_token=folder_token,
                folder_url=folder_url,
                target_folder_url=self._target_folder_url(folder_url),
                app_token=str(metadata["app_token"]),
                table_id=str(metadata["table_id"]),
                table_url=self._metadata_table_url(metadata),
                form_id=str(metadata.get("form_id") or ""),
                form_url=str(metadata.get("form_url") or ""),
            )
            try:
                await self._ensure_table_fields(current)
                current = await self._ensure_form(current)
                await self._ensure_company_editable(current.app_token, file_type="bitable")
                await self.feishu.subscribe_file_events(current.app_token, "bitable")
                self._save_metadata(current)
                return current
            except Exception:
                pass

        app_token, app_url = await self._find_or_create_bitable_app(folder_token)
        table_id = await self._find_or_create_table(app_token)
        workspace = DebugPaperWorkspace(
            folder_token=folder_token,
            folder_url=folder_url,
            target_folder_url=self._target_folder_url(folder_url),
            app_token=app_token,
            table_id=table_id,
            table_url=self._bitable_table_url(app_token, table_id, app_url=app_url),
            form_id="",
            form_url="",
        )
        await self._ensure_table_fields(workspace)
        workspace = await self._ensure_form(workspace)
        await self._ensure_company_editable(app_token, file_type="bitable")
        await self.feishu.subscribe_file_events(app_token, "bitable")
        self._save_metadata(workspace)
        return workspace

    async def handles_table(self, app_token: str, table_id: str) -> bool:
        metadata = self._load_metadata()
        if metadata and metadata.get("app_token") and metadata.get("table_id"):
            return str(metadata.get("app_token")) == str(app_token) and str(metadata.get("table_id")) == str(table_id)
        return False

    async def process_record_by_table(self, *, app_token: str, table_id: str, record_id: str) -> None:
        workspace = await self.ensure_workspace()
        if workspace.app_token != app_token or workspace.table_id != table_id:
            return
        record = await self._get_record(workspace, record_id)
        if not record:
            return
        fields = record.get("fields") or {}
        status = self._field_text(fields.get("处理状态"))
        if status in {STATUS_RUNNING, STATUS_DONE}:
            return
        title = self._field_text(fields.get("副本文件名"))
        if not title:
            await self._update_record(
                workspace,
                record_id,
                {
                    "处理状态": STATUS_FAILED,
                    "日志": "副本文件名为空，无法创建。",
                    "处理时间": self._now_text(),
                },
            )
            return
        await self._update_record(workspace, record_id, {"处理状态": STATUS_RUNNING, "日志": "正在复制调试纸文档。"})
        try:
            result = await self.workspace.copy_local_docx_to_workspace(
                source_path=settings.debug_paper_template_path,
                title=title,
                folder_token=workspace.folder_token,
            )
            await self._ensure_company_editable(result.document_id, file_type="file")
            await self._update_record(
                workspace,
                record_id,
                {
                    "处理状态": STATUS_DONE,
                    "副本位置": {"link": result.url, "text": f"{self._docx_filename(title)}"},
                    "日志": "副本创建完成。",
                    "处理时间": self._now_text(),
                },
            )
        except Exception as exc:
            await self._update_record(
                workspace,
                record_id,
                {
                    "处理状态": STATUS_FAILED,
                    "日志": f"{type(exc).__name__}: {exc}",
                    "处理时间": self._now_text(),
                },
            )

    async def _find_or_create_bitable_app(self, folder_token: str) -> tuple[str, str | None]:
        page_token: str | None = None
        while True:
            response = await self.feishu.list_folder_items(folder_token, page_size=200, page_token=page_token)
            data = response.get("data") or {}
            items = data.get("files") or data.get("items") or []
            for item in items:
                if str(item.get("name") or "").strip() == DEBUG_PAPER_APP_NAME and self._item_type(item) == "bitable":
                    return self._item_token(item), self._item_url(item)
            page_token = data.get("next_page_token") or data.get("page_token")
            if not page_token or not data.get("has_more"):
                break
        created, _resolved = await self.workspace.create_bitable_with_fallback(folder_token=folder_token, name=DEBUG_PAPER_APP_NAME)
        return self._extract_token(created), self._extract_url(created)

    async def _find_or_create_table(self, app_token: str) -> str:
        tables_response = await self.feishu.list_tables(app_token)
        items = (tables_response.get("data") or {}).get("items") or []
        for item in items:
            if str(item.get("name") or item.get("table_name") or "").strip() == DEBUG_PAPER_TABLE_NAME:
                return str(item.get("table_id") or item.get("id") or "")
        created = await self.feishu.create_table(app_token, DEBUG_PAPER_TABLE_NAME, _debug_paper_field_definitions())
        table = (created.get("data") or {}).get("table") or created.get("data") or {}
        return str(table.get("table_id") or table.get("id") or "")

    async def _ensure_table_fields(self, workspace: DebugPaperWorkspace) -> None:
        fields_response = await self.feishu.list_fields(workspace.app_token, workspace.table_id)
        items = (fields_response.get("data") or {}).get("items") or []
        existing = {item.get("field_name"): item for item in items if item.get("field_name")}
        for definition in _debug_paper_field_definitions():
            field_name = definition["field_name"]
            existing_item = existing.get(field_name)
            if not existing_item:
                await self.feishu.create_field(workspace.app_token, workspace.table_id, definition)
                continue
            if field_name == "处理状态":
                current_options = {str(option.get("name") or "").strip() for option in ((existing_item.get("property") or {}).get("options") or [])}
                expected_options = {str(option.get("name") or "").strip() for option in ((definition.get("property") or {}).get("options") or [])}
                if current_options != expected_options:
                    field_id = str(existing_item.get("field_id") or existing_item.get("id") or "")
                    if field_id:
                        await self.feishu.update_field(workspace.app_token, workspace.table_id, field_id, definition)

    async def _ensure_form(self, workspace: DebugPaperWorkspace) -> DebugPaperWorkspace:
        form_id = workspace.form_id
        page_token: str | None = None
        while not form_id:
            response = await self.feishu.list_views(workspace.app_token, workspace.table_id)
            data = response.get("data") or {}
            for item in data.get("items") or []:
                view_name = str(item.get("view_name") or "").strip()
                if str(item.get("view_type") or "").lower() == "form" and view_name in {DEBUG_PAPER_FORM_NAME, "调试纸副本命名"}:
                    form_id = str(item.get("view_id") or item.get("id") or "")
                    break
            page_token = data.get("next_page_token") or data.get("page_token")
            if form_id or not page_token or not data.get("has_more"):
                break
        if not form_id:
            created = await self.feishu.create_view(workspace.app_token, workspace.table_id, view_name=DEBUG_PAPER_FORM_NAME, view_type="form")
            view = (created.get("data") or {}).get("view") or created.get("data") or {}
            form_id = str(view.get("view_id") or view.get("id") or "")
        form_url = workspace.form_url
        try:
            patched = await self.feishu.update_form_metadata(
                workspace.app_token,
                workspace.table_id,
                form_id,
                {
                    "name": DEBUG_PAPER_FORM_NAME,
                    "description": self._form_description(workspace),
                    "shared": True,
                    "shared_limit": "tenant_editable",
                    "submit_limit_once": False,
                },
            )
            form = (patched.get("data") or {}).get("form") or patched.get("data") or {}
            form_url = str(form.get("shared_url") or form_url or self._bitable_table_url(workspace.app_token, workspace.table_id, view_id=form_id))
        except Exception:
            if not form_url:
                form_url = self._bitable_table_url(workspace.app_token, workspace.table_id, view_id=form_id)
        configured = DebugPaperWorkspace(
            folder_token=workspace.folder_token,
            folder_url=workspace.folder_url,
            target_folder_url=workspace.target_folder_url,
            app_token=workspace.app_token,
            table_id=workspace.table_id,
            table_url=workspace.table_url,
            form_id=form_id,
            form_url=form_url,
        )
        await self._configure_form_fields(configured)
        return configured

    async def _configure_form_fields(self, workspace: DebugPaperWorkspace) -> None:
        table_field_ids = await self._table_field_ids_by_name(workspace)
        input_field_id = table_field_ids.get("副本文件名")
        result_field_id = table_field_ids.get("副本位置")
        response = await self.feishu.list_form_fields(workspace.app_token, workspace.table_id, workspace.form_id)
        items = (response.get("data") or {}).get("items") or []
        for item in items:
            field_id = str(item.get("field_id") or item.get("id") or "")
            title = str(item.get("title") or "").strip()
            if not field_id or not title:
                continue
            is_input = field_id == input_field_id or (not input_field_id and title in {"副本文件名", DEBUG_PAPER_FORM_INPUT_TITLE})
            is_result = field_id == result_field_id or (not result_field_id and title in {"副本位置", DEBUG_PAPER_FORM_RESULT_TITLE})
            if is_input:
                payload = {
                    "title": DEBUG_PAPER_FORM_INPUT_TITLE,
                    "description": DEBUG_PAPER_FORM_INPUT_DESCRIPTION,
                    "required": True,
                    "visible": True,
                }
                if (
                    bool(item.get("visible")) is True
                    and bool(item.get("required")) is True
                    and title == DEBUG_PAPER_FORM_INPUT_TITLE
                    and str(item.get("description") or "") == payload["description"]
                ):
                    continue
            elif is_result:
                payload = {
                    "title": DEBUG_PAPER_FORM_RESULT_TITLE,
                    "description": DEBUG_PAPER_FORM_RESULT_DESCRIPTION,
                    "required": False,
                    "visible": True,
                }
                if (
                    bool(item.get("visible")) is True
                    and bool(item.get("required")) is False
                    and title == DEBUG_PAPER_FORM_RESULT_TITLE
                    and str(item.get("description") or "") == payload["description"]
                ):
                    continue
            else:
                payload = {"visible": False}
                if bool(item.get("visible")) is False:
                    continue
            await self._update_form_field_with_retry(workspace, field_id, payload)

    async def _table_field_ids_by_name(self, workspace: DebugPaperWorkspace) -> dict[str, str]:
        response = await self.feishu.list_fields(workspace.app_token, workspace.table_id)
        items = (response.get("data") or {}).get("items") or []
        result: dict[str, str] = {}
        for item in items:
            name = str(item.get("field_name") or "").strip()
            field_id = str(item.get("field_id") or item.get("id") or "").strip()
            if name and field_id:
                result[name] = field_id
        return result

    async def _update_form_field_with_retry(self, workspace: DebugPaperWorkspace, field_id: str, payload: dict[str, Any]) -> None:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                await self.feishu.update_form_field(workspace.app_token, workspace.table_id, workspace.form_id, field_id, payload)
                return
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(0.8 * (attempt + 1))
        if last_error:
            raise last_error

    async def _get_record(self, workspace: DebugPaperWorkspace, record_id: str) -> dict | None:
        response = await self.feishu.search_records(workspace.app_token, workspace.table_id, {"page_size": 500})
        for item in (response.get("data") or {}).get("items") or []:
            if str(item.get("record_id") or "") == record_id:
                return item
        return None

    async def _update_record(self, workspace: DebugPaperWorkspace, record_id: str, fields: dict[str, Any]) -> None:
        await self.feishu.batch_update_records(
            workspace.app_token,
            workspace.table_id,
            [{"record_id": record_id, "fields": fields}],
        )

    async def _ensure_company_editable(self, token: str, *, file_type: str) -> None:
        if not token:
            return
        await self.feishu.update_permission_public(token, file_type=file_type, payload=DEBUG_PAPER_COMPANY_EDIT_PERMISSION)

    def _metadata_path(self) -> Path:
        root = Path(settings.storage_local_root)
        root.mkdir(parents=True, exist_ok=True)
        return root / "debug_paper_form_workspace.json"

    def _load_metadata(self) -> dict[str, Any] | None:
        path = self._metadata_path()
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text("utf-8"))
        except Exception:
            return None

    def _save_metadata(self, workspace: DebugPaperWorkspace) -> None:
        self._metadata_path().write_text(
            json.dumps(
                {
                    "folder_token": workspace.folder_token,
                    "folder_url": workspace.folder_url,
                    "target_folder_url": workspace.target_folder_url,
                    "app_token": workspace.app_token,
                    "table_id": workspace.table_id,
                    "table_url": workspace.table_url,
                    "form_id": workspace.form_id,
                    "form_url": workspace.form_url,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def _field_text(self, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, dict):
            if value.get("text"):
                return str(value.get("text")).strip()
            if value.get("link"):
                return str(value.get("link")).strip()
            return " ".join(self._field_text(item) for item in value.values()).strip()
        if isinstance(value, list):
            return " ".join(self._field_text(item) for item in value).strip()
        return str(value).strip()

    def _docx_filename(self, title: str) -> str:
        value = re.sub(r"\s+", " ", str(title or "").strip())
        value = re.sub(r'[\\/:*?"<>|]+', "_", value).strip(" .")
        if value.lower().endswith(".docx"):
            value = value[:-5].strip(" .")
        return f"{(value or '调试纸副本')[:120]}.docx"

    def _extract_token(self, response: dict) -> str:
        data = response.get("data") or {}
        app = data.get("app") if isinstance(data.get("app"), dict) else {}
        return str(
            data.get("token")
            or data.get("app_token")
            or app.get("app_token")
            or app.get("token")
            or data.get("folder_token")
            or data.get("node", {}).get("token")
            or ""
        )

    def _extract_url(self, response: dict) -> str | None:
        data = response.get("data") or {}
        app = data.get("app") if isinstance(data.get("app"), dict) else {}
        return data.get("url") or app.get("url") or data.get("node", {}).get("url")

    def _item_token(self, item: dict) -> str:
        return str(item.get("token") or item.get("file_token") or item.get("node_token") or "")

    def _item_type(self, item: dict) -> str:
        raw = str(item.get("type") or item.get("mime_type") or item.get("file_type") or "").lower()
        if raw in {"folder", "explorer"}:
            return "folder"
        if raw in {"doc", "docx", "sheet", "mindnote", "bitable"}:
            return raw
        return "file"

    def _item_url(self, item: dict) -> str | None:
        return item.get("url") or item.get("link")

    def _feishu_site_url(self) -> str:
        for configured_url in (settings.feishu_workspace_parent_url, settings.debug_paper_target_folder_url):
            netloc = urlparse(str(configured_url or "")).netloc
            if netloc:
                return f"https://{netloc}"
        domain = settings.feishu_base_url.replace("https://open.", "").replace("http://open.", "")
        return f"https://{domain}"

    def _drive_folder_url(self, folder_token: str) -> str:
        return f"{self._feishu_site_url()}/drive/folder/{folder_token}"

    def _bitable_table_url(self, app_token: str, table_id: str, *, app_url: str | None = None, view_id: str | None = None) -> str:
        if app_url and "?table=" in app_url:
            return app_url
        url = f"{self._feishu_site_url()}/base/{app_token}?table={table_id}"
        if view_id:
            url += f"&view={view_id}"
        return url

    def _metadata_table_url(self, metadata: dict[str, Any]) -> str:
        app_token = str(metadata["app_token"])
        table_id = str(metadata["table_id"])
        table_url = str(metadata.get("table_url") or "")
        if table_url and "://feishu.cn/" not in table_url:
            return table_url
        return self._bitable_table_url(app_token, table_id)

    def _target_folder_url(self, fallback_folder_url: str) -> str:
        return fallback_folder_url

    def _form_description(self, workspace: DebugPaperWorkspace) -> str:
        return "\n".join(
            [
                DEBUG_PAPER_FORM_DESCRIPTION,
                "",
                f"保存位置：{workspace.target_folder_url}",
                f"提交后查看新文档链接：在你的填写记录里查看「{DEBUG_PAPER_FORM_RESULT_TITLE}」，或打开处理记录表 {workspace.table_url} 查看。",
                "说明：飞书原生表单提交成功页不会实时刷新后端结果；提交后稍等片刻，再查看/刷新填写记录即可看到链接。",
            ]
        )

    def _now_text(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _debug_paper_field_definitions() -> list[dict[str, Any]]:
    return [
        {"field_name": "副本文件名", "type": 1},
        {
            "field_name": "处理状态",
            "type": 3,
            "property": {
                "options": [
                    {"name": STATUS_PENDING},
                    {"name": STATUS_RUNNING},
                    {"name": STATUS_DONE},
                    {"name": STATUS_FAILED},
                ]
            },
        },
        {"field_name": "副本位置", "type": 15},
        {"field_name": "处理时间", "type": 1},
        {"field_name": "日志", "type": 1},
    ]
