from __future__ import annotations

import json
import mimetypes
import re
from urllib.parse import unquote
from typing import Any

import httpx

from app.adapters.feishu_auth import FeishuAuthClient
from app.core.config import settings


class FeishuApiError(RuntimeError):
    def __init__(self, message: str, *, code: int | None = None, body: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.body = body or {}


class FeishuClient:
    def __init__(self, auth: FeishuAuthClient | None = None, base_url: str | None = None) -> None:
        self.auth = auth or FeishuAuthClient()
        self.base_url = (base_url or settings.feishu_base_url).rstrip("/")

    async def send_card(self, receive_id: str, card: dict, receive_id_type: str = "chat_id") -> dict:
        return await self._request(
            "POST",
            "/open-apis/im/v1/messages",
            params={"receive_id_type": receive_id_type},
            json={"receive_id": receive_id, "msg_type": "interactive", "content": json.dumps(card, ensure_ascii=False)},
        )

    async def send_text(self, receive_id: str, text: str, receive_id_type: str = "chat_id") -> dict:
        return await self._request(
            "POST",
            "/open-apis/im/v1/messages",
            params={"receive_id_type": receive_id_type},
            json={"receive_id": receive_id, "msg_type": "text", "content": json.dumps({"text": text}, ensure_ascii=False)},
        )

    async def add_message_reaction(self, message_id: str, emoji_type: str) -> dict:
        return await self._request(
            "POST",
            f"/open-apis/im/v1/messages/{message_id}/reactions",
            json={"reaction_type": {"emoji_type": emoji_type}},
        )

    async def remove_message_reaction(self, message_id: str, reaction_id: str) -> dict:
        return await self._request(
            "DELETE",
            f"/open-apis/im/v1/messages/{message_id}/reactions/{reaction_id}",
        )

    async def create_bitable_app(self, name: str, folder_token: str = "") -> dict:
        body: dict[str, Any] = {"name": name}
        if folder_token:
            body["folder_token"] = folder_token
        return await self._request("POST", "/open-apis/bitable/v1/apps", json=body)

    async def create_table(self, app_token: str, table_name: str, fields: list[dict]) -> dict:
        return await self._request(
            "POST",
            f"/open-apis/bitable/v1/apps/{app_token}/tables",
            json={"table": {"name": table_name, "default_view_name": "全部分镜", "fields": fields}},
        )

    async def batch_create_records(self, app_token: str, table_id: str, records: list[dict]) -> dict:
        return await self._request(
            "POST",
            f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_create",
            json={"records": records},
        )

    async def search_records(self, app_token: str, table_id: str, payload: dict | None = None) -> dict:
        return await self._request(
            "POST",
            f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/search",
            json=payload or {},
        )

    async def batch_update_records(self, app_token: str, table_id: str, records: list[dict]) -> dict:
        return await self._request(
            "POST",
            f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_update",
            json={"records": records},
        )

    async def list_fields(self, app_token: str, table_id: str) -> dict:
        return await self._request("GET", f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields")

    async def create_field(self, app_token: str, table_id: str, field: dict) -> dict:
        return await self._request(
            "POST",
            f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields",
            json=field,
        )

    async def delete_field(self, app_token: str, table_id: str, field_id: str) -> dict:
        return await self._request(
            "DELETE",
            f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields/{field_id}",
        )

    async def subscribe_file_events(self, file_token: str, file_type: str = "bitable") -> dict:
        return await self._request(
            "POST",
            f"/open-apis/drive/v1/files/{file_token}/subscribe",
            params={"file_type": file_type},
        )

    async def create_folder(self, parent_token: str, name: str) -> dict:
        return await self._request(
            "POST",
            "/open-apis/drive/v1/files/create_folder",
            json={"folder_token": parent_token, "name": name},
        )

    async def list_folder_items(self, folder_token: str, *, page_size: int = 200, page_token: str | None = None) -> dict:
        params = {"folder_token": folder_token, "page_size": page_size}
        if page_token:
            params["page_token"] = page_token
        return await self._request(
            "GET",
            "/open-apis/drive/v1/files",
            params=params,
        )

    async def move_file(self, file_token: str, *, folder_token: str, file_type: str = "file") -> dict:
        return await self._request(
            "POST",
            f"/open-apis/drive/v1/files/{file_token}/move",
            json={"type": file_type, "folder_token": folder_token},
        )

    async def create_document(self, title: str) -> dict:
        return await self._request(
            "POST",
            "/open-apis/docx/v1/documents",
            json={"title": title},
        )

    async def get_document_raw_content(self, document_id: str) -> dict:
        return await self._request(
            "GET",
            f"/open-apis/docx/v1/documents/{document_id}/raw_content",
        )

    async def convert_document_markdown(self, document_id: str, markdown: str) -> dict:
        return await self._request(
            "POST",
            f"/open-apis/docx/v1/documents/{document_id}/convert",
            json={"content": markdown, "content_type": "markdown"},
        )

    async def append_document_blocks(self, document_id: str, parent_block_id: str, children: list[dict], *, index: int = -1) -> dict:
        return await self._request(
            "POST",
            f"/open-apis/docx/v1/documents/{document_id}/blocks/{parent_block_id}/children",
            json={"children": children, "index": index},
        )

    async def upload_file(self, folder_token: str, name: str, content: bytes) -> dict:
        token = await self.auth.get_tenant_access_token()
        files = {"file": (name, content)}
        data = {"parent_type": "explorer", "parent_node": folder_token, "file_name": name, "size": str(len(content))}
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                f"{self.base_url}/open-apis/drive/v1/files/upload_all",
                headers={"Authorization": f"Bearer {token}"},
                data=data,
                files=files,
            )
        return self._decode_response(response)

    async def upload_bitable_attachment(self, app_token: str, name: str, content: bytes) -> dict:
        token = await self.auth.get_tenant_access_token()
        mime_type = mimetypes.guess_type(name)[0] or ""
        parent_type = "bitable_image" if mime_type.startswith("image/") else "bitable_file"
        files = {"file": (name, content)}
        data = {"parent_type": parent_type, "parent_node": app_token, "file_name": name, "size": str(len(content))}
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                f"{self.base_url}/open-apis/drive/v1/medias/upload_all",
                headers={"Authorization": f"Bearer {token}"},
                data=data,
                files=files,
            )
        return self._decode_response(response)

    async def download_drive_file(self, file_token: str) -> tuple[str, bytes, str]:
        token = await self.auth.get_tenant_access_token()
        errors: list[Exception] = []
        for path in (
            f"/open-apis/drive/v1/files/{file_token}/download",
            f"/open-apis/drive/v1/medias/{file_token}/download",
        ):
            try:
                return await self._download_binary(path, token=token, file_token=file_token)
            except FeishuApiError as exc:
                errors.append(exc)
                continue
        raise FeishuApiError(
            f"Feishu download error: file_token={file_token}",
            body={"errors": [str(item) for item in errors]},
        )

    async def _download_binary(self, path: str, *, token: str, file_token: str) -> tuple[str, bytes, str]:
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            response = await client.get(
                f"{self.base_url}{path}",
                headers={"Authorization": f"Bearer {token}"},
            )
        try:
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise FeishuApiError(
                f"Feishu download error: status={response.status_code}, file_token={file_token}, path={path}"
            ) from exc
        content_type = response.headers.get("content-type", "application/octet-stream").split(";")[0]
        disposition = response.headers.get("content-disposition", "")
        filename = _filename_from_disposition(disposition) or f"{file_token}{mimetypes.guess_extension(content_type) or ''}"
        return filename, response.content, content_type

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
        timeout: float = 30,
    ) -> dict:
        token = await self.auth.get_tenant_access_token()
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.request(
                method,
                f"{self.base_url}{path}",
                headers={"Authorization": f"Bearer {token}"},
                params=params,
                json=json,
            )
        return self._decode_response(response)

    def _decode_response(self, response: httpx.Response) -> dict:
        body: dict[str, Any] = {}
        try:
            body = response.json()
        except ValueError:
            body = {"raw": response.text[:1000]}
        try:
            response.raise_for_status()
        except httpx.HTTPError as exc:
            msg = body.get("msg") or body.get("message") or response.text[:300]
            raise FeishuApiError(f"Feishu HTTP error: {response.status_code}, msg={msg}", body=body) from exc
        if body.get("code", 0) not in (0, None):
            raise FeishuApiError(
                f"Feishu API error: code={body.get('code')}, msg={body.get('msg')}",
                code=body.get("code"),
                body=body,
            )
        return body


def _filename_from_disposition(value: str) -> str | None:
    if not value:
        return None
    match = re.search(r"filename\*=UTF-8''([^;]+)", value, flags=re.IGNORECASE)
    if match:
        return unquote(match.group(1)).strip('"')
    match = re.search(r'filename="?([^";]+)"?', value, flags=re.IGNORECASE)
    if match:
        return unquote(match.group(1))
    return None
