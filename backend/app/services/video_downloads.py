from __future__ import annotations

import asyncio
import json
import re
import shutil
import sys
import tempfile
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from app.adapters.feishu import FeishuApiError, FeishuClient
from app.core.config import settings
from app.services.feishu_workspace import FeishuWorkspaceService

DOWNLOAD_APP_NAME = "视频下载"
DOWNLOAD_TABLE_NAME = "下载任务"
DOWNLOAD_STATUS_PENDING = "未开始"
DOWNLOAD_STATUS_START = "启动"
DOWNLOAD_STATUS_RUNNING = "正在下载"
DOWNLOAD_STATUS_DONE = "已下载"
DOWNLOAD_STATUS_FAILED = "下载失败"
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".flv", ".ts"}
DOWNLOAD_TIMEOUT_SECONDS = 120 * 60
STALE_RUNNING_SECONDS = 35 * 60
DOWNLOAD_RETRY_COUNT = 3
DOWNLOAD_RETRY_BACKOFF_SECONDS = (5, 15, 30)
MAX_LOG_CHARS = 50000
NON_DOCUMENT_DOWNLOAD_FOLDER_NAME = "非文档视频下载"


@dataclass(frozen=True)
class VideoDownloadWorkspace:
    folder_token: str
    folder_url: str
    app_token: str
    table_id: str
    table_url: str


@dataclass(frozen=True)
class VideoDownloadRequest:
    url: str
    comments: str
    filename_hint: str | None = None
    source_session: str | None = None


@dataclass(frozen=True)
class VideoDownloadResult:
    record_id: str
    status: str
    file_name: str | None = None
    file_url: str | None = None
    folder_url: str | None = None
    log: str | None = None


@dataclass(frozen=True)
class _DownloadTargetFolder:
    token: str
    url: str
    name: str


@dataclass(frozen=True)
class VideoDownloadOrganizeReport:
    workspace_url: str
    records_scanned: int
    records_updated: int
    record_files_moved: int
    orphan_root_videos_moved: int
    document_links_indexed: int = 0
    empty_legacy_folders_deleted: int = 0
    skipped: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


class VideoDownloadService:
    def __init__(
        self,
        *,
        feishu: FeishuClient | None = None,
        workspace: FeishuWorkspaceService | None = None,
    ) -> None:
        self.feishu = feishu or FeishuClient()
        self.workspace = workspace or FeishuWorkspaceService(self.feishu)
        self._document_title_cache: dict[str, str] = {}

    async def ensure_workspace(self) -> VideoDownloadWorkspace:
        folder = await self.workspace.ensure_workspace_subfolder(settings.feishu_workspace_video_download_folder_name)
        folder_token = str(folder.get("folder_token") or "")
        folder_url = str(folder.get("folder_url") or self._drive_folder_url(folder_token))

        metadata = self._load_metadata()
        if metadata and metadata.get("folder_token") == folder_token and metadata.get("app_token") and metadata.get("table_id"):
            workspace = VideoDownloadWorkspace(
                folder_token=folder_token,
                folder_url=folder_url,
                app_token=str(metadata["app_token"]),
                table_id=str(metadata["table_id"]),
                table_url=str(metadata.get("table_url") or self._bitable_table_url(str(metadata["app_token"]), str(metadata["table_id"]))),
            )
            try:
                await self._ensure_table_fields(workspace)
                await self.feishu.subscribe_file_events(workspace.app_token, "bitable")
                self._save_metadata(workspace)
                return workspace
            except Exception:
                pass

        app_token, app_url = await self._find_or_create_bitable_app(folder_token)
        table_id = await self._find_or_create_table(app_token)
        workspace = VideoDownloadWorkspace(
            folder_token=folder_token,
            folder_url=folder_url,
            app_token=app_token,
            table_id=table_id,
            table_url=self._bitable_table_url(app_token, table_id, app_url=app_url),
        )
        await self._ensure_table_fields(workspace)
        await self.feishu.subscribe_file_events(app_token, "bitable")
        self._save_metadata(workspace)
        return workspace

    async def handles_table(self, app_token: str, table_id: str) -> bool:
        workspace = await self.ensure_workspace()
        return workspace.app_token == app_token and workspace.table_id == table_id

    async def create_chat_download_task(self, request: VideoDownloadRequest) -> VideoDownloadResult:
        workspace = await self.ensure_workspace()
        return await self.create_chat_download_task_in_workspace(workspace=workspace, request=request)

    async def organize_existing_downloads(
        self,
        *,
        workspace: VideoDownloadWorkspace | None = None,
        dry_run: bool = False,
        document_urls: tuple[str, ...] = (),
        cleanup_legacy_empty_folders: bool = False,
    ) -> VideoDownloadOrganizeReport:
        workspace = workspace or await self.ensure_workspace()
        records = await self._list_all_records(workspace)
        folder_index = await self._index_download_folder_items(workspace)
        document_sources_by_identity = await self._index_document_video_sources(document_urls)
        target_cache: dict[str, _DownloadTargetFolder] = {}
        skipped: list[str] = []
        errors: list[str] = []
        records_updated = 0
        record_files_moved = 0
        recorded_file_tokens: set[str] = set()

        for record in records:
            record_id = str(record.get("record_id") or "")
            fields = record.get("fields") or {}
            comments = self._field_text(fields.get("comments"))
            url = self._field_link(fields.get("链接")) or self._field_text(fields.get("链接"))
            target_folder = await self._resolve_target_folder_for_organize(
                workspace,
                comments,
                url=url,
                folder_index=folder_index,
                cache=target_cache,
                create=not dry_run,
                document_sources_by_identity=document_sources_by_identity,
            )
            file_location = self._field_link(fields.get("文件位置")) or self._field_text(fields.get("文件位置"))
            file_token = self._file_token_from_url(file_location)
            if file_token:
                recorded_file_tokens.add(file_token)

            update_fields: dict[str, Any] = {}
            current_target = self._field_link(fields.get("目标文件夹")) or self._field_text(fields.get("目标文件夹"))
            if current_target != target_folder.url:
                update_fields["目标文件夹"] = {"link": target_folder.url, "text": target_folder.name}

            if update_fields:
                if not dry_run:
                    await self._update_record(workspace, record_id, update_fields)
                records_updated += 1

            if not file_token:
                if self._field_text(fields.get("下载状态")) == DOWNLOAD_STATUS_DONE:
                    skipped.append(f"{record_id}: 已下载但无法从文件位置识别 file token")
                continue

            current_folder = folder_index["file_to_folder"].get(file_token)
            if current_folder == target_folder.token:
                continue
            if current_folder is None:
                skipped.append(f"{record_id}: 文件不在视频下载根目录或已知子文件夹中，跳过移动 file_token={file_token}")
                continue
            try:
                if not dry_run:
                    await self.feishu.move_file(file_token, folder_token=target_folder.token, file_type="file")
                    folder_index["file_to_folder"][file_token] = target_folder.token
                record_files_moved += 1
            except Exception as exc:
                errors.append(f"{record_id}: 移动失败 file_token={file_token}, error={type(exc).__name__}: {exc}")

        non_doc_folder = await self._resolve_target_folder_for_organize(
            workspace,
            None,
            url=None,
            folder_index=folder_index,
            cache=target_cache,
            create=not dry_run,
            document_sources_by_identity=document_sources_by_identity,
        )
        orphan_root_videos_moved = 0
        for item in folder_index["root_files"]:
            token = self._item_token(item)
            name = str(item.get("name") or "").strip()
            if not token or token in recorded_file_tokens:
                continue
            if Path(name).suffix.lower() not in VIDEO_EXTENSIONS:
                continue
            try:
                if not dry_run:
                    await self.feishu.move_file(token, folder_token=non_doc_folder.token, file_type="file")
                orphan_root_videos_moved += 1
            except Exception as exc:
                errors.append(f"root:{name}: 移动到非文档文件夹失败 file_token={token}, error={type(exc).__name__}: {exc}")
        empty_legacy_folders_deleted = await self._cleanup_empty_legacy_document_folders(
            workspace,
            folder_index=folder_index,
            dry_run=dry_run,
            enabled=cleanup_legacy_empty_folders,
            errors=errors,
        )

        return VideoDownloadOrganizeReport(
            workspace_url=workspace.folder_url,
            records_scanned=len(records),
            records_updated=records_updated,
            record_files_moved=record_files_moved,
            orphan_root_videos_moved=orphan_root_videos_moved,
            document_links_indexed=len(document_sources_by_identity),
            empty_legacy_folders_deleted=empty_legacy_folders_deleted,
            skipped=tuple(skipped),
            errors=tuple(errors),
        )

    async def create_chat_download_task_in_workspace(
        self,
        *,
        workspace: VideoDownloadWorkspace,
        request: VideoDownloadRequest,
    ) -> VideoDownloadResult:
        existing = await self._find_existing_download_record(workspace, request.url, comments=request.comments)
        if existing:
            return await self.process_record(workspace=workspace, record_id=str(existing["record_id"]))

        target_folder = await self._resolve_target_folder(workspace, request.comments)
        record_fields = {
            "链接": {"link": request.url, "text": request.url},
            "下载状态": DOWNLOAD_STATUS_PENDING,
            "目标文件夹": {"link": target_folder.url, "text": target_folder.name},
            "文件名": request.filename_hint or "",
            "comments": request.comments or "",
            "来源会话": request.source_session or "",
            "创建时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        created = await self.feishu.batch_create_records(
            workspace.app_token,
            workspace.table_id,
            [{"fields": record_fields}],
        )
        items = (created.get("data") or {}).get("records") or (created.get("data") or {}).get("items") or []
        if not items:
            raise RuntimeError("视频下载任务创建成功，但没有返回 record_id")
        record_id = str(items[0].get("record_id") or items[0].get("id") or "")
        if not record_id:
            raise RuntimeError("视频下载任务创建成功，但没有返回有效的 record_id")
        return await self.process_record(workspace=workspace, record_id=record_id)

    async def process_record(self, *, workspace: VideoDownloadWorkspace, record_id: str) -> VideoDownloadResult:
        record = await self._get_record(workspace, record_id)
        if not record:
            raise RuntimeError(f"视频下载任务不存在：{record_id}")
        fields = record.get("fields") or {}
        url = self._field_link(fields.get("链接")) or self._field_text(fields.get("链接"))
        if not url:
            await self._update_record(
                workspace,
                record_id,
                {
                    "下载状态": DOWNLOAD_STATUS_FAILED,
                    "log": "缺少可下载链接，请填写 `链接` 字段。",
                },
            )
            return VideoDownloadResult(record_id=record_id, status=DOWNLOAD_STATUS_FAILED, log="缺少可下载链接")

        status = self._field_text(fields.get("下载状态"))
        file_location = self._field_link(fields.get("文件位置")) or self._field_text(fields.get("文件位置"))
        if status == DOWNLOAD_STATUS_DONE and file_location:
            folder_location = self._field_link(fields.get("目标文件夹")) or self._field_text(fields.get("目标文件夹")) or workspace.folder_url
            return VideoDownloadResult(
                record_id=record_id,
                status=DOWNLOAD_STATUS_DONE,
                file_name=self._field_text(fields.get("文件名")) or None,
                file_url=file_location,
                folder_url=folder_location,
                log=self._field_text(fields.get("log")) or None,
            )
        if status == DOWNLOAD_STATUS_RUNNING:
            if self._is_stale_running_record(fields):
                stale_log = "下载任务长时间停留在“正在下载”，已自动标记为失败，请重试。"
                await self._update_record(
                    workspace,
                    record_id,
                    {
                        "下载状态": DOWNLOAD_STATUS_FAILED,
                        "log": stale_log,
                    },
                )
                return VideoDownloadResult(
                    record_id=record_id,
                    status=DOWNLOAD_STATUS_FAILED,
                    file_name=self._field_text(fields.get("文件名")) or None,
                    file_url=file_location or None,
                    folder_url=workspace.folder_url,
                    log=stale_log,
                )
            return VideoDownloadResult(
                record_id=record_id,
                status=DOWNLOAD_STATUS_RUNNING,
                file_name=self._field_text(fields.get("文件名")) or None,
                file_url=file_location or None,
                folder_url=workspace.folder_url,
                log=self._field_text(fields.get("log")) or None,
            )
        if status not in {DOWNLOAD_STATUS_PENDING, DOWNLOAD_STATUS_START, DOWNLOAD_STATUS_FAILED, ""}:
            return VideoDownloadResult(
                record_id=record_id,
                status=status,
                file_name=self._field_text(fields.get("文件名")) or None,
                file_url=file_location or None,
                folder_url=workspace.folder_url,
                log=self._field_text(fields.get("log")) or None,
            )

        filename_hint = self._select_filename_hint(fields)
        comments = self._field_text(fields.get("comments"))
        comment_context_hint = self._extract_comment_context_hint(comments)
        target_folder = await self._resolve_target_folder(workspace, comments)
        await self._update_record(
            workspace,
            record_id,
            {
                "下载状态": DOWNLOAD_STATUS_RUNNING,
                "log": "已开始下载，正在调用高清视频下载器。",
            },
        )
        attempt_logs: list[str] = []
        total_attempts = DOWNLOAD_RETRY_COUNT + 1
        for attempt in range(1, total_attempts + 1):
            try:
                download = await self._download_video(url, filename_hint=filename_hint)
                if not filename_hint and self._is_youtube_url(url) and comment_context_hint:
                    contextual_file_name = self._append_comment_context_to_filename(download.file_name, comment_context_hint)
                    if contextual_file_name != download.file_name:
                        download = _DownloadedVideo(
                            file_name=contextual_file_name,
                            content=download.content,
                            log=f"{download.log}\n\n使用 YouTube 原标题，并追加批注内容：{download.file_name} -> {contextual_file_name}",
                            downloader=getattr(download, "downloader", "unknown"),
                            resolution=getattr(download, "resolution", None),
                        )
                unique_file_name = await self._unique_upload_name(
                    workspace,
                    target_folder_token=target_folder.token,
                    target_folder_url=target_folder.url,
                    desired_name=download.file_name,
                    record_id=record_id,
                    url=url,
                )
                final_comments = comments
                if unique_file_name != download.file_name:
                    download = _DownloadedVideo(
                        file_name=unique_file_name,
                        content=download.content,
                        log=f"{download.log}\n\n上传前去重改名：{download.file_name} -> {unique_file_name}",
                        downloader=getattr(download, "downloader", "unknown"),
                        resolution=getattr(download, "resolution", None),
                    )
                if not filename_hint and download.file_name and download.file_name not in comments:
                    final_comments = (comments.strip() + f"\n自动命名：{download.file_name}").strip()
                upload, resolved_folder = await self.workspace.upload_file_with_fallback(
                    target_folder=target_folder.token,
                    name=download.file_name,
                    content=download.content,
                )
                file_token = str((upload.get("data") or {}).get("file_token") or (upload.get("data") or {}).get("file", {}).get("file_token") or "")
                file_url = self._extract_feishu_file_url(upload) or (self._drive_file_url(file_token) if file_token else "")
                if not file_url:
                    raise RuntimeError("飞书上传完成，但没有返回可访问的文件链接")
                success_log = self._clip_log("\n\n".join([*attempt_logs, download.log]))
                update_fields = {
                    "下载状态": DOWNLOAD_STATUS_DONE,
                    "文件名": download.file_name,
                    "文件位置": {"link": file_url, "text": download.file_name},
                    "目标文件夹": {"link": self._drive_folder_url(resolved_folder), "text": target_folder.name},
                    "log": success_log,
                    "下载器": getattr(download, "downloader", "unknown"),
                }
                if getattr(download, "resolution", None):
                    update_fields["清晰度"] = download.resolution
                if final_comments:
                    update_fields["comments"] = final_comments
                await self._update_record(workspace, record_id, update_fields)
                return VideoDownloadResult(
                    record_id=record_id,
                    status=DOWNLOAD_STATUS_DONE,
                    file_name=download.file_name,
                    file_url=file_url,
                    folder_url=self._drive_folder_url(resolved_folder),
                    log=success_log,
                )
            except Exception as exc:
                attempt_log = self._format_attempt_exception(attempt=attempt, total_attempts=total_attempts, exc=exc)
                attempt_logs.append(attempt_log)
                if attempt <= DOWNLOAD_RETRY_COUNT:
                    retry_log = self._clip_log(
                        "\n\n".join(
                            [
                                *attempt_logs,
                                f"第 {attempt} 次尝试失败，等待 {DOWNLOAD_RETRY_BACKOFF_SECONDS[attempt - 1]} 秒后自动重试（{attempt}/{DOWNLOAD_RETRY_COUNT}）。",
                            ]
                        )
                    )
                    await self._update_record(
                        workspace,
                        record_id,
                        {
                            "下载状态": DOWNLOAD_STATUS_RUNNING,
                            "log": retry_log,
                        },
                    )
                    await asyncio.sleep(DOWNLOAD_RETRY_BACKOFF_SECONDS[attempt - 1])
                    continue
                error_log = self._clip_log(
                    "\n\n".join(
                        [
                            *attempt_logs,
                            f"最终失败：初始尝试 + {DOWNLOAD_RETRY_COUNT} 次自动重试均未成功。",
                        ]
                    )
                )
                await self._update_record(
                    workspace,
                    record_id,
                    {
                        "下载状态": DOWNLOAD_STATUS_FAILED,
                        "log": error_log,
                    },
                )
                return VideoDownloadResult(record_id=record_id, status=DOWNLOAD_STATUS_FAILED, log=error_log)

    async def process_record_by_table(self, *, app_token: str, table_id: str, record_id: str) -> VideoDownloadResult:
        workspace = await self.ensure_workspace()
        if workspace.app_token != app_token or workspace.table_id != table_id:
            raise RuntimeError("收到的多维表格事件不属于视频下载工作流")
        return await self.process_record(workspace=workspace, record_id=record_id)

    def parse_request_from_conversation(self, text: str, *, recent_texts: list[str] | None = None, source_session: str | None = None) -> VideoDownloadRequest | None:
        normalized = str(text or "").strip()
        if not normalized:
            return None
        comments = normalized
        urls = self._extract_external_urls(normalized)
        if not urls and recent_texts:
            for item in recent_texts:
                urls = self._extract_external_urls(item)
                if urls:
                    comments = f"{normalized}\n\n引用的最近资料：\n{item.strip()}"
                    break
        if not urls:
            return None
        if not self._looks_like_download_intent(normalized):
            return None
        filename_hint = self._extract_filename_hint(normalized)
        return VideoDownloadRequest(
            url=urls[0],
            comments=comments,
            filename_hint=filename_hint,
            source_session=source_session,
        )

    async def _find_or_create_bitable_app(self, folder_token: str) -> tuple[str, str | None]:
        page_token: str | None = None
        while True:
            response = await self.feishu.list_folder_items(folder_token, page_size=200, page_token=page_token)
            data = response.get("data") or {}
            items = data.get("files") or data.get("items") or []
            for item in items:
                item_name = str(item.get("name") or "").strip()
                item_type = self._item_type(item)
                if item_name == DOWNLOAD_APP_NAME and item_type == "bitable":
                    return self._item_token(item), self._item_url(item)
            page_token = data.get("next_page_token") or data.get("page_token")
            if not page_token or not data.get("has_more"):
                break
        created, _resolved = await self.workspace.create_bitable_with_fallback(folder_token=folder_token, name=DOWNLOAD_APP_NAME)
        app_token = self._extract_token(created)
        return app_token, self._extract_url(created)

    async def _find_or_create_table(self, app_token: str) -> str:
        tables_response = await self.feishu.list_tables(app_token)
        items = (tables_response.get("data") or {}).get("items") or []
        for item in items:
            if str(item.get("name") or item.get("table_name") or "").strip() == DOWNLOAD_TABLE_NAME:
                return str(item.get("table_id") or item.get("id") or "")
        created = await self.feishu.create_table(app_token, DOWNLOAD_TABLE_NAME, _video_download_field_definitions())
        table = (created.get("data") or {}).get("table") or created.get("data") or {}
        return str(table.get("table_id") or table.get("id") or "")

    async def _ensure_table_fields(self, workspace: VideoDownloadWorkspace) -> None:
        fields_response = await self.feishu.list_fields(workspace.app_token, workspace.table_id)
        items = (fields_response.get("data") or {}).get("items") or []
        existing = {item.get("field_name"): item for item in items if item.get("field_name")}
        for definition in _video_download_field_definitions():
            field_name = definition["field_name"]
            existing_item = existing.get(field_name)
            if not existing_item:
                await self.feishu.create_field(workspace.app_token, workspace.table_id, definition)
                continue
            if field_name == "下载状态":
                current_options = {
                    str(option.get("name") or "").strip()
                    for option in ((existing_item.get("property") or {}).get("options") or [])
                    if option.get("name")
                }
                expected_options = {
                    str(option.get("name") or "").strip()
                    for option in ((definition.get("property") or {}).get("options") or [])
                    if option.get("name")
                }
                if current_options != expected_options:
                    field_id = str(existing_item.get("field_id") or existing_item.get("id") or "")
                    if field_id:
                        await self.feishu.update_field(workspace.app_token, workspace.table_id, field_id, definition)

    async def _get_record(self, workspace: VideoDownloadWorkspace, record_id: str) -> dict | None:
        items = await self._list_all_records(workspace)
        return next((item for item in items if str(item.get("record_id") or "") == record_id), None)

    async def _find_existing_download_record(self, workspace: VideoDownloadWorkspace, url: str, *, comments: str | None = None) -> dict | None:
        target_key = self._download_identity(url)
        if not target_key:
            return None
        target_scope = self._download_target_identity(comments)
        items = await self._list_all_records(workspace)
        matches: list[dict] = []
        for item in items:
            fields = item.get("fields") or {}
            existing_url = self._field_link(fields.get("链接")) or self._field_text(fields.get("链接"))
            if self._download_identity(existing_url) == target_key and self._record_download_target_identity(fields) == target_scope:
                matches.append(item)
        if not matches:
            return None

        def priority(item: dict) -> tuple[int, str]:
            fields = item.get("fields") or {}
            status = self._field_text(fields.get("下载状态"))
            file_location = self._field_link(fields.get("文件位置")) or self._field_text(fields.get("文件位置"))
            created_at = self._field_text(fields.get("创建时间")) or "9999"
            if status == DOWNLOAD_STATUS_DONE and file_location:
                return (0, created_at)
            if status == DOWNLOAD_STATUS_RUNNING:
                return (1, created_at)
            if status in {DOWNLOAD_STATUS_START, DOWNLOAD_STATUS_PENDING, ""}:
                return (2, created_at)
            return (3, created_at)

        return sorted(matches, key=priority)[0]

    async def _list_all_records(self, workspace: VideoDownloadWorkspace) -> list[dict]:
        items: list[dict] = []
        page_token: str | None = None
        while True:
            payload: dict[str, Any] = {"page_size": 500}
            if page_token:
                payload["page_token"] = page_token
            response = await self.feishu.search_records(workspace.app_token, workspace.table_id, payload)
            data = response.get("data") or {}
            items.extend(data.get("items") or [])
            page_token = data.get("next_page_token") or data.get("page_token")
            if not data.get("has_more") or not page_token:
                break
        return items

    async def _update_record(self, workspace: VideoDownloadWorkspace, record_id: str, fields: dict[str, Any]) -> None:
        await self.feishu.batch_update_records(
            workspace.app_token,
            workspace.table_id,
            [{"record_id": record_id, "fields": fields}],
        )

    async def _resolve_target_folder(self, workspace: VideoDownloadWorkspace, comments: str | None) -> _DownloadTargetFolder:
        source = self._extract_document_source(comments)
        if source:
            folder_name = await self._document_folder_name(source)
        else:
            folder_name = NON_DOCUMENT_DOWNLOAD_FOLDER_NAME
        return await self._ensure_child_download_folder(workspace, folder_name)

    async def _ensure_child_download_folder(self, workspace: VideoDownloadWorkspace, folder_name: str) -> _DownloadTargetFolder:
        safe_name = self._sanitize_filename(folder_name) or NON_DOCUMENT_DOWNLOAD_FOLDER_NAME
        if not hasattr(self.feishu, "list_folder_items"):
            return _DownloadTargetFolder(token=workspace.folder_token, url=workspace.folder_url, name=DOWNLOAD_APP_NAME)
        page_token: str | None = None
        while True:
            response = await self.feishu.list_folder_items(workspace.folder_token, page_size=200, page_token=page_token)
            data = response.get("data") or {}
            items = data.get("files") or data.get("items") or []
            for item in items:
                if self._item_type(item) == "folder" and str(item.get("name") or "").strip() == safe_name:
                    token = self._item_token(item)
                    return _DownloadTargetFolder(
                        token=token,
                        url=self._item_url(item) or self._drive_folder_url(token),
                        name=safe_name,
                    )
            page_token = data.get("next_page_token") or data.get("page_token")
            if not page_token or not data.get("has_more"):
                break
        if not hasattr(self.feishu, "create_folder"):
            return _DownloadTargetFolder(token=workspace.folder_token, url=workspace.folder_url, name=DOWNLOAD_APP_NAME)
        folder = await self.feishu.create_folder(workspace.folder_token, safe_name)
        token = self._extract_token(folder)
        return _DownloadTargetFolder(
            token=token or workspace.folder_token,
            url=self._extract_url(folder) or self._drive_folder_url(token or workspace.folder_token),
            name=safe_name,
        )

    async def _list_all_folder_items(self, folder_token: str) -> list[dict]:
        items: list[dict] = []
        page_token: str | None = None
        while True:
            response = await self.feishu.list_folder_items(folder_token, page_size=200, page_token=page_token)
            data = response.get("data") or {}
            items.extend(data.get("files") or data.get("items") or [])
            page_token = data.get("next_page_token") or data.get("page_token")
            if not data.get("has_more") or not page_token:
                break
        return items

    async def _index_download_folder_items(self, workspace: VideoDownloadWorkspace) -> dict[str, Any]:
        root_items = await self._list_all_folder_items(workspace.folder_token)
        file_to_folder: dict[str, str] = {}
        root_files: list[dict] = []
        child_folders: list[dict] = []
        child_folders_by_name: dict[str, dict] = {}
        for item in root_items:
            item_type = self._item_type(item)
            token = self._item_token(item)
            if not token:
                continue
            if item_type == "folder":
                child_folders.append(item)
                item_name = str(item.get("name") or "").strip()
                if item_name:
                    child_folders_by_name[item_name] = item
                continue
            if item_type == "file":
                file_to_folder[token] = workspace.folder_token
                root_files.append(item)
        for folder in child_folders:
            folder_token = self._item_token(folder)
            if not folder_token:
                continue
            for item in await self._list_all_folder_items(folder_token):
                if self._item_type(item) != "file":
                    continue
                token = self._item_token(item)
                if token:
                    file_to_folder[token] = folder_token
        return {"file_to_folder": file_to_folder, "root_files": root_files, "child_folders_by_name": child_folders_by_name}

    async def _resolve_target_folder_for_organize(
        self,
        workspace: VideoDownloadWorkspace,
        comments: str | None,
        *,
        url: str | None,
        folder_index: dict[str, Any],
        cache: dict[str, _DownloadTargetFolder],
        create: bool,
        document_sources_by_identity: dict[str, dict[str, str]] | None = None,
    ) -> _DownloadTargetFolder:
        source = (document_sources_by_identity or {}).get(self._download_identity(url or ""))
        if not source:
            source = self._extract_document_source(comments)
        if source:
            folder_name = await self._document_folder_name(source)
        else:
            folder_name = NON_DOCUMENT_DOWNLOAD_FOLDER_NAME
        safe_name = self._sanitize_filename(folder_name) or NON_DOCUMENT_DOWNLOAD_FOLDER_NAME
        if safe_name in cache:
            return cache[safe_name]
        existing = (folder_index.get("child_folders_by_name") or {}).get(safe_name)
        if existing:
            token = self._item_token(existing)
            target = _DownloadTargetFolder(
                token=token,
                url=self._item_url(existing) or self._drive_folder_url(token),
                name=safe_name,
            )
            cache[safe_name] = target
            return target
        if not create:
            target = _DownloadTargetFolder(
                token=f"__pending__/{safe_name}",
                url=f"{workspace.folder_url.rstrip('/')}/{safe_name}",
                name=safe_name,
            )
            cache[safe_name] = target
            return target
        folder = await self.feishu.create_folder(workspace.folder_token, safe_name)
        token = self._extract_token(folder) or workspace.folder_token
        target = _DownloadTargetFolder(
            token=token,
            url=self._extract_url(folder) or self._drive_folder_url(token),
            name=safe_name,
        )
        cache[safe_name] = target
        (folder_index.get("child_folders_by_name") or {})[safe_name] = {
            "name": safe_name,
            "type": "folder",
            "token": token,
            "url": target.url,
        }
        return target

    def _extract_document_source(self, comments: str | None) -> dict[str, str] | None:
        text = str(comments or "")
        if not text.strip():
            return None
        match = re.search(r"https?://[^\s)）>]+/(?:docx|docs)/([A-Za-z0-9]+)[^\s)）>]*", text)
        if match:
            token = match.group(1)
            url = match.group(0)
            title = ""
            for line in text.splitlines():
                if url not in line and token not in line:
                    continue
                candidate = re.sub(r"https?://\S+", "", line)
                candidate = re.sub(r"^(?:来源文档|文档|docx|docs|原文链接)\s*[：:|]?\s*", "", candidate, flags=re.IGNORECASE)
                candidate = candidate.strip(" \t\r\n-:：|()（）[]【】")
                if candidate:
                    title = candidate
                    break
            return {"token": token, "url": url, "title": title}
        title = self._extract_document_title_hint(text)
        if title:
            return {"token": "", "url": "", "title": title}
        return None

    def _extract_document_title_hint(self, text: str | None) -> str:
        value = str(text or "")
        if not value.strip():
            return ""
        lines = [line.strip() for line in value.splitlines()]
        for index, line in enumerate(lines):
            if not re.match(r"^来源文档\s*[：:]?\s*$", line):
                continue
            for next_line in lines[index + 1 : index + 4]:
                candidate = next_line.strip(" \t\r\n-:：|()（）[]【】")
                if not candidate or candidate.startswith("{") or candidate.startswith("http"):
                    continue
                return self._sanitize_filename(candidate)
        match = re.search(r"来源文档\s*[：:]\s*([^\n{]+)", value)
        if match:
            candidate = re.sub(r"https?://\S+", "", match.group(1)).strip(" \t\r\n-:：|()（）[]【】")
            if candidate:
                return self._sanitize_filename(candidate)
        return ""

    async def _document_folder_name(self, source: dict[str, str]) -> str:
        title = self._sanitize_filename(source.get("title") or "")
        token = str(source.get("token") or "").strip()
        if not title and token:
            title = await self._fetch_document_title(token)
        if title:
            return self._truncate_utf8(title, 120).strip(" .") or NON_DOCUMENT_DOWNLOAD_FOLDER_NAME
        if token:
            return f"未命名文档_{token[:8]}"
        return NON_DOCUMENT_DOWNLOAD_FOLDER_NAME

    async def _fetch_document_title(self, token: str) -> str:
        token = str(token or "").strip()
        if not token:
            return ""
        if token in self._document_title_cache:
            return self._document_title_cache[token]
        try:
            response = await self.feishu.get_document_metadata(token)
            document = ((response.get("data") or {}).get("document") or {})
            title = self._sanitize_filename(str(document.get("title") or ""))
        except Exception:
            title = ""
        self._document_title_cache[token] = title
        return title

    async def _index_document_video_sources(self, document_urls: tuple[str, ...]) -> dict[str, dict[str, str]]:
        indexed: dict[str, dict[str, str]] = {}
        for document_url in document_urls:
            source = await self._document_source_from_url(document_url)
            if not source:
                continue
            links = await self._collect_document_video_links(source["token"])
            for link in links:
                identity = self._download_identity(link)
                if identity:
                    indexed[identity] = source
        return indexed

    async def _document_source_from_url(self, document_url: str | None) -> dict[str, str] | None:
        value = str(document_url or "").strip()
        match = re.search(r"https?://[^\s)）>]+/(?:docx|docs)/([A-Za-z0-9]+)[^\s)）>]*", value)
        if not match:
            return None
        token = match.group(1)
        title = await self._fetch_document_title(token)
        return {"token": token, "url": value, "title": title}

    async def _collect_document_video_links(self, document_token: str) -> set[str]:
        links: set[str] = set()
        try:
            raw = await self.feishu.get_document_raw_content(document_token)
            links.update(self._extract_external_urls(json.dumps(raw, ensure_ascii=False)))
        except Exception:
            pass
        page_token: str | None = None
        while True:
            try:
                response = await self.feishu.list_file_comments(document_token, file_type="docx", page_size=100, page_token=page_token)
            except Exception:
                break
            links.update(self._extract_external_urls(json.dumps(response, ensure_ascii=False)))
            data = response.get("data") or {}
            page_token = data.get("next_page_token") or data.get("page_token")
            if not data.get("has_more") or not page_token:
                break
        return links

    async def _cleanup_empty_legacy_document_folders(
        self,
        workspace: VideoDownloadWorkspace,
        *,
        folder_index: dict[str, Any],
        dry_run: bool,
        enabled: bool,
        errors: list[str],
    ) -> int:
        if not enabled:
            return 0
        deleted = 0
        for name, item in list((folder_index.get("child_folders_by_name") or {}).items()):
            if not re.match(r"^文档_[A-Za-z0-9]{8,}$", name):
                continue
            token = self._item_token(item)
            if not token:
                continue
            try:
                children = await self._list_all_folder_items(token)
                if children:
                    continue
                if not dry_run:
                    await self.feishu.delete_file(token, file_type="folder")
                deleted += 1
            except Exception as exc:
                errors.append(f"legacy-folder:{name}: 清理空旧文件夹失败 token={token}, error={type(exc).__name__}: {exc}")
        return deleted

    def _download_target_identity(self, comments: str | None) -> str:
        source = self._extract_document_source(comments)
        if source:
            token = str(source.get("token") or "").strip()
            if token:
                return f"doc:{token}"
            title = self._sanitize_filename(source.get("title") or "")
            if title:
                return f"doc-title:{title}"
        return "non_doc"

    def _record_download_target_identity(self, fields: dict[str, Any]) -> str:
        comments_identity = self._download_target_identity(self._field_text(fields.get("comments")))
        if comments_identity != "non_doc":
            return comments_identity
        record_folder = self._field_link(fields.get("目标文件夹")) or self._field_text(fields.get("目标文件夹"))
        if record_folder and NON_DOCUMENT_DOWNLOAD_FOLDER_NAME not in record_folder and DOWNLOAD_APP_NAME not in record_folder:
            return f"folder:{record_folder}"
        return "non_doc"

    async def _download_video(self, url: str, *, filename_hint: str | None = None) -> "_DownloadedVideo":
        if self._is_youtube_url(url):
            ytdlp_error: Exception | None = None
            try:
                return await self._download_video_with_ytdlp(url, filename_hint=filename_hint)
            except Exception as exc:
                ytdlp_error = exc
            try:
                fallback = await self._download_video_with_videodl(url, filename_hint=filename_hint)
                return _DownloadedVideo(
                    file_name=fallback.file_name,
                    content=fallback.content,
                    log=(
                        "yt-dlp 高清下载失败，已回退 videodl。\n"
                        f"yt-dlp 错误：{ytdlp_error}\n\n"
                        f"{fallback.log}"
                    ),
                    downloader="videodl-fallback",
                    resolution=fallback.resolution,
                )
            except Exception as fallback_exc:
                raise RuntimeError(
                    "YouTube 视频下载失败：yt-dlp 高清路径与 videodl 回退路径均失败。\n\n"
                    f"yt-dlp 错误：{ytdlp_error}\n\n"
                    f"videodl 错误：{fallback_exc}"
                ) from fallback_exc
        return await self._download_video_with_videodl(url, filename_hint=filename_hint)

    async def _download_video_with_ytdlp(self, url: str, *, filename_hint: str | None = None) -> "_DownloadedVideo":
        with tempfile.TemporaryDirectory(prefix="ytdlp_") as tmpdir:
            cmd = [
                sys.executable,
                "-m",
                "yt_dlp",
                "--no-playlist",
                "--merge-output-format",
                "mp4",
                "--remux-video",
                "mp4",
                "-f",
                "bv*+ba/b",
                "-o",
                "%(title).180B.%(ext)s",
                url,
            ]
            stdout, stderr = await self._run_download_command(cmd, cwd=tmpdir, tool_name="yt-dlp")
            video_path = self._find_downloaded_video(Path(tmpdir))
            if not video_path:
                raise RuntimeError(
                    "yt-dlp 执行成功，但没有找到下载后的视频文件。\n"
                    f"stdout:\n{self._decode_process_output(stdout)}\n"
                    f"stderr:\n{self._decode_process_output(stderr)}"
                )
            final_name = self._resolve_final_filename(video_path.name, filename_hint)
            if final_name != video_path.name:
                renamed = video_path.with_name(final_name)
                video_path.rename(renamed)
                video_path = renamed
            resolution = await self._probe_resolution(video_path)
            return _DownloadedVideo(
                file_name=video_path.name,
                content=video_path.read_bytes(),
                downloader="yt-dlp",
                resolution=resolution,
                log=self._success_log(
                    tool_name="yt-dlp",
                    cmd=cmd,
                    url=url,
                    video_path=video_path,
                    stdout=stdout,
                    stderr=stderr,
                    resolution=resolution,
                ),
            )

    async def _download_video_with_videodl(self, url: str, *, filename_hint: str | None = None) -> "_DownloadedVideo":
        videodl_bin = self._videodl_binary()
        with tempfile.TemporaryDirectory(prefix="videodl_") as tmpdir:
            cmd = [videodl_bin, "-i", url]
            stdout, stderr = await self._run_download_command(cmd, cwd=tmpdir, tool_name="videodl")
            video_path = self._find_downloaded_video(Path(tmpdir))
            if not video_path:
                raise RuntimeError(
                    "videodl 执行成功，但没有找到下载后的视频文件。\n"
                    f"stdout:\n{self._decode_process_output(stdout)}"
                )
            final_name = self._resolve_final_filename(video_path.name, filename_hint)
            if final_name != video_path.name:
                renamed = video_path.with_name(final_name)
                video_path.rename(renamed)
                video_path = renamed
            resolution = await self._probe_resolution(video_path)
            return _DownloadedVideo(
                file_name=video_path.name,
                content=video_path.read_bytes(),
                downloader="videodl",
                resolution=resolution,
                log=self._success_log(
                    tool_name="videodl",
                    cmd=cmd,
                    url=url,
                    video_path=video_path,
                    stdout=stdout,
                    stderr=stderr,
                    resolution=resolution,
                ),
            )

    async def _run_download_command(self, cmd: list[str], *, cwd: str, tool_name: str) -> tuple[bytes, bytes]:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=DOWNLOAD_TIMEOUT_SECONDS)
        except asyncio.TimeoutError as exc:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
            raise RuntimeError(f"{tool_name} 下载超时：超过 {DOWNLOAD_TIMEOUT_SECONDS // 60} 分钟仍未完成\n命令：{' '.join(cmd)}") from exc
        if process.returncode != 0:
            raise RuntimeError(
                f"{tool_name} 下载失败：exit={process.returncode}\n"
                f"命令：{' '.join(cmd)}\n"
                f"stdout:\n{self._decode_process_output(stdout)}\n"
                f"stderr:\n{self._decode_process_output(stderr)}"
            )
        return stdout, stderr

    def _find_downloaded_video(self, directory: Path) -> Path | None:
        candidates = [path for path in directory.rglob("*") if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS]
        if not candidates:
            return None
        candidates.sort(key=lambda path: (path.stat().st_mtime, path.stat().st_size), reverse=True)
        return candidates[0]

    def _resolve_final_filename(self, original_name: str, filename_hint: str | None) -> str:
        if not filename_hint:
            return original_name
        suffix = Path(original_name).suffix or ".mp4"
        safe = self._sanitize_filename(filename_hint)
        if not safe:
            return original_name
        if not Path(safe).suffix:
            safe = f"{safe}{suffix}"
        return safe

    async def _unique_upload_name(
        self,
        workspace: VideoDownloadWorkspace,
        *,
        target_folder_token: str,
        target_folder_url: str,
        desired_name: str,
        record_id: str,
        url: str,
    ) -> str:
        safe_name = self._sanitize_filename(desired_name) or "downloaded_video.mp4"
        suffix = Path(safe_name).suffix or ".mp4"
        if not Path(safe_name).suffix:
            safe_name = f"{safe_name}{suffix}"
        existing_names = await self._existing_download_names(
            workspace,
            target_folder_token=target_folder_token,
            target_folder_url=target_folder_url,
            exclude_record_id=record_id,
        )
        if safe_name not in existing_names:
            return safe_name
        identity_suffix = self._identity_suffix(url)
        stem = Path(safe_name).stem
        first_candidate = f"{stem}_{identity_suffix}{suffix}" if identity_suffix and identity_suffix not in stem else f"{stem}_2{suffix}"
        first_candidate = self._sanitize_filename(first_candidate)
        if first_candidate not in existing_names:
            return first_candidate
        index = 3
        while True:
            candidate = self._sanitize_filename(f"{stem}_{identity_suffix}_{index}{suffix}" if identity_suffix else f"{stem}_{index}{suffix}")
            if candidate not in existing_names:
                return candidate
            index += 1

    async def _existing_download_names(
        self,
        workspace: VideoDownloadWorkspace,
        *,
        target_folder_token: str,
        target_folder_url: str,
        exclude_record_id: str,
    ) -> set[str]:
        names: set[str] = set()
        page_token: str | None = None
        while True:
            response = await self.feishu.list_folder_items(target_folder_token, page_size=200, page_token=page_token)
            data = response.get("data") or {}
            items = data.get("files") or data.get("items") or []
            for item in items:
                item_name = str(item.get("name") or "").strip()
                if item_name:
                    names.add(item_name)
            page_token = data.get("next_page_token") or data.get("page_token")
            if not page_token or not data.get("has_more"):
                break
        response = await self.feishu.search_records(workspace.app_token, workspace.table_id, {})
        items = (response.get("data") or {}).get("items") or []
        for item in items:
            if str(item.get("record_id") or "") == exclude_record_id:
                continue
            fields = item.get("fields") or {}
            record_folder = self._field_link(fields.get("目标文件夹")) or self._field_text(fields.get("目标文件夹"))
            if record_folder and record_folder != target_folder_url:
                continue
            for field_name in ("文件名", "文件位置"):
                item_name = self._field_text(fields.get(field_name))
                if item_name:
                    names.add(Path(item_name).name)
        return names

    def _sanitize_filename(self, raw: str) -> str:
        value = re.sub(r"\s+", " ", str(raw or "").strip())
        value = re.sub(r'[\\\\/:*?"<>|]+', "_", value)
        value = value.strip(" .")
        return value[:180]

    def _select_filename_hint(self, fields: dict[str, Any]) -> str | None:
        table_hint = self._field_text(fields.get("文件名"))
        comments = self._field_text(fields.get("comments"))
        explicit_comment_hint = self._extract_filename_hint(comments)
        if explicit_comment_hint:
            return explicit_comment_hint
        comment_video_name = self._extract_comment_video_name_hint(comments)
        if comment_video_name:
            return comment_video_name
        if table_hint and not self._is_machine_generated_filename_hint(table_hint):
            return table_hint
        return None

    def _append_comment_context_to_filename(self, original_name: str, comment_context: str) -> str:
        path = Path(str(original_name or "").strip())
        suffix = path.suffix or ".mp4"
        stem = path.stem or "video"
        context = self._truncate_utf8(self._sanitize_filename(comment_context), 72).strip(" .")
        if not context:
            return original_name
        annotation = f"（批注内容：{context}）"
        max_stem_bytes = max(8, 220 - len(annotation.encode("utf-8")) - len(suffix.encode("utf-8")))
        safe_stem = self._truncate_utf8(self._sanitize_filename(stem), max_stem_bytes).strip(" .") or "video"
        return f"{safe_stem}{annotation}{suffix}"

    def _truncate_utf8(self, value: str, max_bytes: int) -> str:
        text = str(value or "")
        output: list[str] = []
        total = 0
        for char in text:
            size = len(char.encode("utf-8"))
            if total + size > max_bytes:
                break
            output.append(char)
            total += size
        return "".join(output)

    def _extract_comment_context_hint(self, text: str | None) -> str | None:
        value = str(text or "").strip()
        if not value:
            return None
        lines = [line.strip(" \t\r\n-:：|") for line in value.splitlines() if line.strip()]
        preferred: list[str] = []
        for line in lines:
            if "|" in line:
                preferred.append(line.rsplit("|", 1)[-1].strip())
            elif "原文" in line or "批注" in line or "评论" in line:
                preferred.append(re.sub(r"^(?:原文|批注|评论|语境|引用)\s*[：:|]?\s*", "", line).strip())
        for candidate in preferred:
            candidate = re.sub(r"https?://\S+", "", candidate)
            candidate = candidate.strip(" “”、，。,.…")
            if not candidate or len(candidate) < 2:
                continue
            if candidate.lower() in {"none", "null", "youtube", "video"}:
                continue
            safe = self._sanitize_filename(candidate)
            if safe and not self._is_machine_generated_filename_hint(safe):
                return safe[:80]
        return None

    def _extract_comment_video_name_hint(self, text: str | None) -> str | None:
        value = str(text or "").strip()
        if not value:
            return None
        candidates: list[str] = []
        for line in value.splitlines():
            stripped = line.strip()
            if not stripped or "YouTube视频名" in stripped:
                continue
            match = re.search(r"(?:批注说明|备注)\s*[：:]\s*(.+)", stripped)
            if match:
                candidates.append(match.group(1))
        if not candidates and self._is_youtube_url_text(value):
            cleaned_lines: list[str] = []
            for line in value.splitlines():
                stripped = line.strip()
                if not stripped or "YouTube视频名" in stripped:
                    continue
                if re.match(r"^(?:来源文档|批注位置|批注 ID|原始链接|创建时间|来源会话)\s*[：:]", stripped):
                    continue
                cleaned_lines.append(stripped)
            candidates.append(" ".join(cleaned_lines))
        for candidate in candidates:
            candidate = re.sub(r"https?://\S+", "", candidate)
            candidate = re.sub(r"\{[^{}]*'type':\s*'text'[^{}]*\}", "", candidate)
            candidate = candidate.strip(" “”、，。,.…:：-|\t\r\n")
            if not candidate:
                continue
            candidate = self._sanitize_filename(candidate)
            if candidate and candidate.lower() not in {"none", "null", "youtube", "video"}:
                return self._truncate_utf8(candidate, 180)
        return None

    def _is_youtube_url_text(self, text: str) -> bool:
        return bool(re.search(r"https?://(?:www\.)?(?:youtube\.com|youtu\.be)/\S+", str(text or ""), re.IGNORECASE))

    def _is_machine_generated_filename_hint(self, value: str | None) -> bool:
        name = Path(str(value or "").strip(" `")).stem
        return bool(re.match(r"^(?:克尔维特|视频|youtube|YouTube|下载|download)[ _-]*[A-Za-z0-9_-]{8,15}$", name))

    def _identity_suffix(self, url: str) -> str:
        identity = self._download_identity(url)
        if ":" in identity:
            return identity.split(":", 1)[1][-11:]
        return ""

    def _file_token_from_url(self, value: str | None) -> str:
        raw = str(value or "").strip()
        if not raw:
            return ""
        parsed = urlparse(raw)
        query = parse_qs(parsed.query)
        for key in ("file_token", "token"):
            token = (query.get(key) or [""])[0]
            if token:
                return token
        parts = [part for part in parsed.path.split("/") if part]
        for marker in ("file", "medias"):
            if marker in parts:
                index = parts.index(marker)
                if len(parts) > index + 1:
                    return parts[index + 1]
        if re.fullmatch(r"[A-Za-z0-9_-]{8,}", raw):
            return raw
        return ""

    def _is_youtube_url(self, url: str) -> bool:
        return self._download_identity(url).startswith("youtube:")

    def _videodl_binary(self) -> str:
        candidate = shutil.which("videodl")
        if candidate:
            return candidate
        fallback = str(Path.home() / ".local" / "bin" / "videodl")
        if Path(fallback).exists():
            return fallback
        raise RuntimeError("未找到 videodl，可执行文件不存在。")

    async def _probe_resolution(self, video_path: Path) -> str | None:
        if not shutil.which("ffprobe"):
            return None
        cmd = [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "json",
            str(video_path),
        ]
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _stderr = await asyncio.wait_for(process.communicate(), timeout=30)
            if process.returncode != 0:
                return None
            payload = json.loads(stdout.decode("utf-8", errors="replace") or "{}")
            stream = ((payload.get("streams") or [{}])[0]) or {}
            width = int(stream.get("width") or 0)
            height = int(stream.get("height") or 0)
            if width and height:
                return f"{width}x{height}"
        except Exception:
            return None
        return None

    def _success_log(
        self,
        *,
        tool_name: str,
        cmd: list[str],
        url: str,
        video_path: Path,
        stdout: bytes,
        stderr: bytes,
        resolution: str | None,
    ) -> str:
        return self._clip_log(
            (
                f"下载器：{tool_name}\n"
                f"命令：{' '.join(cmd)}\n"
                f"原始链接：{url}\n"
                f"输出文件：{video_path.name}\n"
                f"清晰度：{resolution or '未知'}\n"
                f"stdout:\n{self._decode_process_output(stdout)}\n"
                f"stderr:\n{self._decode_process_output(stderr)}"
            ).strip()
        )

    def _format_attempt_exception(self, *, attempt: int, total_attempts: int, exc: Exception) -> str:
        return self._clip_log(
            (
                f"第 {attempt}/{total_attempts} 次尝试失败\n"
                f"异常类型：{type(exc).__name__}\n"
                f"异常信息：{exc}\n"
                f"traceback:\n{''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))}"
            ).strip()
        )

    def _decode_process_output(self, output: bytes) -> str:
        return output.decode("utf-8", errors="replace").strip()

    def _clip_log(self, value: str) -> str:
        text = str(value or "")
        if len(text) <= MAX_LOG_CHARS:
            return text
        return text[: MAX_LOG_CHARS - 200] + "\n\n[日志过长，已截断；前面保留完整异常类型、命令、返回码和主要 stdout/stderr。]"

    def _extract_external_urls(self, text: str) -> list[str]:
        urls: list[str] = []
        for raw_url in re.findall(r"https?://\S+", text):
            cleaned = raw_url.rstrip(").,;!?]}>\"'，。；、")
            host = urlparse(cleaned).netloc.lower()
            if "feishu.cn" in host or "larksuite.com" in host:
                continue
            urls.append(cleaned)
        return list(dict.fromkeys(urls))

    def _download_identity(self, url: str) -> str:
        value = str(url or "").strip()
        if not value:
            return ""
        parsed = urlparse(value)
        host = parsed.netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        path_parts = [part for part in parsed.path.split("/") if part]
        if host in {"youtube.com", "m.youtube.com", "music.youtube.com"}:
            video_id = (parse_qs(parsed.query).get("v") or [""])[0]
            if video_id:
                return f"youtube:{video_id}"
            if len(path_parts) >= 2 and path_parts[0] in {"shorts", "embed", "live"}:
                return f"youtube:{path_parts[1]}"
        if host == "youtu.be" and path_parts:
            return f"youtube:{path_parts[0]}"
        normalized = parsed._replace(fragment="").geturl().rstrip("/")
        return f"url:{normalized}"

    def _looks_like_download_intent(self, text: str) -> bool:
        normalized = str(text or "").lower()
        keywords = (
            "下载", "存下来", "保存下来", "抓取", "保存到飞书", "download", "save this video",
        )
        return any(keyword in normalized for keyword in keywords)

    def _extract_filename_hint(self, text: str | None) -> str | None:
        value = str(text or "").strip()
        if not value:
            return None
        patterns = [
            r"(?:文件名|命名为|名字叫|叫做|保存成|改成)\s*[：: ]\s*[\"“]?([^\"”\n]+?)[\"”]?(?:$|\n)",
            r"帮我下载\s*([^，。\n]+?)\s*视频",
        ]
        generic_names = {"这个", "这个视频", "该", "该视频", "它", "这个片子", "这个影片"}
        for pattern in patterns:
            match = re.search(pattern, value, re.IGNORECASE)
            if match:
                candidate_text = re.sub(r"https?://\S+", "", match.group(1)).strip()
                candidate = self._sanitize_filename(candidate_text)
                if candidate in generic_names:
                    continue
                if candidate and not self._extract_external_urls(candidate):
                    return candidate
        return None

    def _field_text(self, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, dict):
            for key in ("text", "name", "title", "value"):
                item = value.get(key)
                if isinstance(item, str) and item.strip():
                    return item.strip()
            if isinstance(value.get("link"), str):
                return str(value["link"]).strip()
        if isinstance(value, list):
            parts = [self._field_text(item) for item in value]
            return "\n".join(part for part in parts if part).strip()
        return str(value).strip()

    def _field_link(self, value: Any) -> str:
        if isinstance(value, dict):
            link = value.get("link") or value.get("url")
            if isinstance(link, str):
                return link.strip()
        return ""

    def _is_stale_running_record(self, fields: dict[str, Any]) -> bool:
        created_text = self._field_text(fields.get("创建时间"))
        if not created_text:
            return False
        try:
            created_at = datetime.strptime(created_text, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return False
        age = (datetime.now() - created_at).total_seconds()
        return age >= STALE_RUNNING_SECONDS

    def _metadata_path(self) -> Path:
        root = Path(settings.storage_local_root).resolve()
        root.mkdir(parents=True, exist_ok=True)
        return root / "video_download_workspace.json"

    def _load_metadata(self) -> dict[str, Any] | None:
        path = self._metadata_path()
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text("utf-8"))
        except Exception:
            return None

    def _save_metadata(self, workspace: VideoDownloadWorkspace) -> None:
        path = self._metadata_path()
        path.write_text(
            json.dumps(
                {
                    "folder_token": workspace.folder_token,
                    "folder_url": workspace.folder_url,
                    "app_token": workspace.app_token,
                    "table_id": workspace.table_id,
                    "table_url": workspace.table_url,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def _extract_token(self, response: dict) -> str:
        data = response.get("data") or {}
        return str(data.get("token") or data.get("folder_token") or data.get("node", {}).get("token") or "")

    def _extract_url(self, response: dict) -> str | None:
        data = response.get("data") or {}
        return data.get("url") or data.get("node", {}).get("url")

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
        domain = settings.feishu_base_url.replace("https://open.", "").replace("http://open.", "")
        return f"https://{domain}"

    def _drive_folder_url(self, folder_token: str) -> str:
        return f"{self._feishu_site_url()}/drive/folder/{folder_token}"

    def _drive_file_url(self, file_token: str) -> str:
        return f"{self._feishu_site_url()}/file/{file_token}"

    def _bitable_table_url(self, app_token: str, table_id: str, *, app_url: str | None = None) -> str:
        if app_url and "?table=" in app_url:
            return app_url
        return f"{self._feishu_site_url()}/base/{app_token}?table={table_id}"

    def _extract_feishu_file_url(self, response: dict) -> str:
        data = response.get("data") or {}
        file_info = data.get("file") or {}
        return str(file_info.get("url") or data.get("url") or "").strip()


@dataclass(frozen=True)
class _DownloadedVideo:
    file_name: str
    content: bytes
    log: str
    downloader: str = "unknown"
    resolution: str | None = None


def _video_download_field_definitions() -> list[dict[str, Any]]:
    return [
        {"field_name": "链接", "type": 15},
        {
            "field_name": "下载状态",
            "type": 3,
            "property": {
                "options": [
                    {"name": DOWNLOAD_STATUS_PENDING},
                    {"name": DOWNLOAD_STATUS_START},
                    {"name": DOWNLOAD_STATUS_RUNNING},
                    {"name": DOWNLOAD_STATUS_DONE},
                    {"name": DOWNLOAD_STATUS_FAILED},
                ]
            },
        },
        {"field_name": "文件位置", "type": 15},
        {"field_name": "目标文件夹", "type": 15},
        {"field_name": "文件名", "type": 1},
        {"field_name": "清晰度", "type": 1},
        {"field_name": "下载器", "type": 1},
        {"field_name": "comments", "type": 1},
        {"field_name": "来源会话", "type": 1},
        {"field_name": "创建时间", "type": 1},
        {"field_name": "log", "type": 1},
    ]
