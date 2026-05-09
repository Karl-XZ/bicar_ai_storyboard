from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from app.adapters.feishu import FeishuClient, FeishuApiError
from app.core.config import settings


@dataclass(frozen=True)
class FeishuDocumentResult:
    document_id: str
    url: str
    folder_token: str


class FeishuWorkspaceService:
    def __init__(self, feishu: FeishuClient | None = None) -> None:
        self.feishu = feishu or FeishuClient()

    async def ensure_default_workspace_folder(self) -> dict[str, str | int]:
        parent_token = self._folder_token_from_url(getattr(settings, "feishu_workspace_parent_url", "")) or "root"
        try:
            target_folder = await self.ensure_named_folder(parent_token=parent_token, name=settings.feishu_workspace_folder_name)
            target_token = self._extract_token(target_folder)
            moved_items = 0
            source_token = settings.feishu_root_folder_token
            if source_token and source_token != target_token:
                moved_items = await self.move_all_items(source_folder_token=source_token, target_folder_token=target_token)
            return {
                "folder_token": target_token,
                "folder_url": self._extract_url(target_folder) or self._drive_folder_url(target_token),
                "moved_items": moved_items,
            }
        except FeishuApiError:
            current_token = settings.feishu_root_folder_token or "root"
            return {
                "folder_token": current_token,
                "folder_url": self._drive_folder_url(current_token),
                "moved_items": 0,
            }

    async def ensure_named_folder(self, *, parent_token: str, name: str) -> dict:
        page_token: str | None = None
        while True:
            response = await self.feishu.list_folder_items(parent_token, page_size=200, page_token=page_token)
            data = response.get("data", {})
            items = data.get("files") or data.get("items") or []
            for item in items:
                if str(item.get("name") or "").strip() == name and self._item_type(item) == "folder":
                    return {"code": 0, "data": {"token": self._item_token(item), "url": self._item_url(item)}}
            page_token = data.get("next_page_token") or data.get("page_token")
            if not page_token or not data.get("has_more"):
                break
        return await self.feishu.create_folder(parent_token, name)

    async def move_all_items(self, *, source_folder_token: str, target_folder_token: str) -> int:
        moved = 0
        page_token: str | None = None
        while True:
            response = await self.feishu.list_folder_items(source_folder_token, page_size=200, page_token=page_token)
            data = response.get("data", {})
            items = data.get("files") or data.get("items") or []
            for item in items:
                token = self._item_token(item)
                if not token:
                    continue
                file_type = self._item_type(item)
                if file_type == "folder" and token == target_folder_token:
                    continue
                try:
                    await self.feishu.move_file(token, folder_token=target_folder_token, file_type=file_type)
                    moved += 1
                except FeishuApiError:
                    continue
            page_token = data.get("next_page_token") or data.get("page_token")
            if not page_token or not data.get("has_more"):
                break
        return moved

    async def save_markdown_document(self, *, title: str, markdown: str, folder_token: str | None = None) -> FeishuDocumentResult:
        target_folder = folder_token or settings.feishu_root_folder_token or "root"
        try:
            response = await self.feishu.create_document(title)
            document_id = self._extract_document_id(response)
            if not document_id:
                raise RuntimeError("Feishu create_document did not return document_id")
            blocks = self._markdown_to_blocks(markdown)
            if blocks:
                for chunk in self._chunk_blocks(blocks, size=50):
                    await self.feishu.append_document_blocks(document_id, document_id, chunk)
            await self.feishu.move_file(document_id, folder_token=target_folder, file_type="docx")
            return FeishuDocumentResult(
                document_id=document_id,
                url=self._doc_url(document_id),
                folder_token=target_folder,
            )
        except FeishuApiError:
            pass

        filename = f"{title}.md"
        upload = await self.feishu.upload_file(target_folder, filename, markdown.encode("utf-8"))
        file_token = str((upload.get("data") or {}).get("file_token") or (upload.get("data") or {}).get("file", {}).get("file_token") or "")
        return FeishuDocumentResult(
            document_id=file_token or title,
            url=self._drive_file_url(file_token) if file_token else self._drive_folder_url(target_folder),
            folder_token=target_folder,
        )

    async def read_reference(self, url_or_token: str) -> dict:
        value = str(url_or_token or "").strip()
        document_id = self._document_id_from_url(value)
        if document_id:
            raw = await self.feishu.get_document_raw_content(document_id)
            return {
                "type": "feishu_doc",
                "document_id": document_id,
                "url": self._doc_url(document_id),
                "content_json": raw.get("data") or raw,
            }
        file_token = self._file_token_from_url(value) or self._feishu_token(value)
        if file_token:
            filename, content, mime_type = await self.feishu.download_drive_file(file_token)
            return {
                "type": "feishu_file",
                "file_token": file_token,
                "filename": filename,
                "mime_type": mime_type,
                "text_content": self._decode_file_content(content, mime_type=mime_type, filename=filename),
            }
        raise RuntimeError("无法识别飞书文档或文件链接")

    def _folder_token_from_url(self, url: str | None) -> str | None:
        if not url:
            return None
        parsed = urlparse(str(url))
        parts = [part for part in parsed.path.split("/") if part]
        try:
            index = parts.index("folder")
            return parts[index + 1] if len(parts) > index + 1 else None
        except ValueError:
            return None

    def _document_id_from_url(self, url: str | None) -> str | None:
        if not url:
            return None
        parsed = urlparse(str(url))
        parts = [part for part in parsed.path.split("/") if part]
        for marker in ("docx", "docs"):
            if marker in parts:
                index = parts.index(marker)
                if len(parts) > index + 1:
                    return parts[index + 1]
        return None

    def _file_token_from_url(self, url: str | None) -> str | None:
        if not url:
            return None
        parsed = urlparse(str(url))
        parts = [part for part in parsed.path.split("/") if part]
        if "file" in parts:
            index = parts.index("file")
            if len(parts) > index + 1:
                return parts[index + 1]
        return None

    def _feishu_token(self, value: str) -> str | None:
        if value.startswith("feishu://"):
            return value.replace("feishu://", "", 1).strip().strip("/")
        return None

    def _extract_token(self, response: dict) -> str:
        data = response.get("data", {})
        return str(data.get("token") or data.get("folder_token") or data.get("node", {}).get("token") or "")

    def _extract_url(self, response: dict) -> str | None:
        data = response.get("data", {})
        return data.get("url") or data.get("node", {}).get("url")

    def _extract_document_id(self, response: dict) -> str:
        data = response.get("data", {})
        document = data.get("document") or {}
        return str(document.get("document_id") or data.get("document_id") or data.get("token") or "")

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

    def _doc_url(self, document_id: str) -> str:
        return f"{self._feishu_site_url()}/docx/{document_id}"

    def _drive_folder_url(self, folder_token: str) -> str:
        return f"{self._feishu_site_url()}/drive/folder/{folder_token}"

    def _drive_file_url(self, file_token: str) -> str:
        return f"{self._feishu_site_url()}/file/{file_token}"

    def _feishu_site_url(self) -> str:
        domain = settings.feishu_base_url.replace("https://open.", "").replace("http://open.", "")
        return f"https://{domain}"

    def _decode_file_content(self, content: bytes, *, mime_type: str, filename: str) -> str:
        suffix = Path(filename).suffix.lower()
        if mime_type.startswith("text/") or suffix in {".md", ".markdown", ".txt", ".json", ".csv", ".tsv"}:
            try:
                return content.decode("utf-8")
            except UnicodeDecodeError:
                return content.decode("utf-8", errors="replace")
        if suffix == ".json":
            try:
                return json.dumps(json.loads(content.decode("utf-8")), ensure_ascii=False, indent=2)
            except Exception:
                pass
        return f"[binary file omitted] mime_type={mime_type} filename={filename} bytes={len(content)}"

    def _markdown_to_blocks(self, markdown: str) -> list[dict]:
        lines = (markdown or "").splitlines()
        blocks: list[dict] = []
        in_code = False
        code_lines: list[str] = []
        for raw_line in lines:
            line = raw_line.rstrip()
            if line.strip().startswith("```"):
                if in_code and code_lines:
                    blocks.append(self._paragraph_block("\n".join(code_lines)))
                    code_lines = []
                in_code = not in_code
                continue
            if in_code:
                code_lines.append(line)
                continue
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith(("# ", "## ", "### ", "#### ", "##### ", "###### ")):
                blocks.append(self._paragraph_block(re.sub(r"^#+\s*", "", stripped)))
                continue
            if re.match(r"^[-*]\s+", stripped):
                bullet_text = re.sub(r"^[-*]\s+", "", stripped)
                blocks.append(self._paragraph_block(f"• {bullet_text}"))
                continue
            if re.match(r"^\\d+\\.\\s+", stripped):
                blocks.append(self._paragraph_block(stripped))
                continue
            blocks.append(self._paragraph_block(stripped))
        if in_code and code_lines:
            blocks.append(self._paragraph_block("\n".join(code_lines)))
        return blocks

    def _paragraph_block(self, text: str) -> dict:
        return {
            "block_type": 2,
            "text": {
                "elements": [
                    {
                        "text_run": {
                            "content": text,
                        }
                    }
                ]
            },
        }

    def _chunk_blocks(self, blocks: list[dict], *, size: int) -> list[list[dict]]:
        if size <= 0:
            return [blocks]
        return [blocks[index : index + size] for index in range(0, len(blocks), size)]
