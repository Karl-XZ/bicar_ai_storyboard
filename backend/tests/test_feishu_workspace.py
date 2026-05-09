import asyncio

from app.core.config import settings
from app.services.feishu_workspace import FeishuWorkspaceService


class FakeFeishuClient:
    def __init__(self) -> None:
        self.moves = []
        self.uploads = []
        self.appended_blocks = []
        self.append_calls = []

    async def list_folder_items(self, folder_token: str, *, page_size: int = 200, page_token: str | None = None) -> dict:
        if folder_token == "parent_folder":
            return {
                "code": 0,
                "data": {
                    "files": [
                        {"name": "AI分镜", "token": "new_root", "type": "folder", "url": "https://feishu.test/drive/folder/new_root"}
                    ],
                    "has_more": False,
                },
            }
        if folder_token == "old_root":
            return {
                "code": 0,
                "data": {
                    "files": [
                        {"name": "旧文档", "token": "doc_1", "type": "docx"},
                        {"name": "旧表格", "token": "sheet_1", "type": "sheet"},
                    ],
                    "has_more": False,
                },
            }
        return {"code": 0, "data": {"files": [], "has_more": False}}

    async def move_file(self, file_token: str, *, folder_token: str, file_type: str = "file") -> dict:
        self.moves.append((file_token, folder_token, file_type))
        return {"code": 0, "data": {}}

    async def create_folder(self, parent_token: str, name: str) -> dict:
        return {"code": 0, "data": {"token": "new_root", "url": "https://feishu.test/drive/folder/new_root"}}

    async def create_document(self, title: str) -> dict:
        return {"code": 0, "data": {"document": {"document_id": "docx_123"}}}

    async def append_document_blocks(self, document_id: str, parent_block_id: str, blocks: list[dict]) -> dict:
        assert document_id == "docx_123"
        assert parent_block_id == "docx_123"
        self.append_calls.append(list(blocks))
        self.appended_blocks.extend(blocks)
        return {"code": 0, "data": {}}

    async def upload_file(self, folder_token: str, name: str, content: bytes) -> dict:
        self.uploads.append((folder_token, name, content))
        return {"code": 0, "data": {"file_token": "file_123"}}

    async def get_document_raw_content(self, document_id: str) -> dict:
        return {"code": 0, "data": {"content": [{"type": "text", "text": "hello"}], "document_id": document_id}}

    async def download_drive_file(self, file_token: str) -> tuple[str, bytes, str]:
        return ("notes.md", b"# hello", "text/markdown")


def test_workspace_service_migrates_old_root_into_new_ai_folder(monkeypatch):
    monkeypatch.setattr(settings, "feishu_workspace_parent_url", "https://feishu.test/drive/folder/parent_folder")
    monkeypatch.setattr(settings, "feishu_workspace_folder_name", "AI分镜")
    monkeypatch.setattr(settings, "feishu_root_folder_token", "old_root")
    service = FeishuWorkspaceService(feishu=FakeFeishuClient())

    result = asyncio.run(service.ensure_default_workspace_folder())

    assert result["folder_token"] == "new_root"
    assert result["moved_items"] == 2
    assert result["folder_url"] == "https://feishu.test/drive/folder/new_root"


def test_workspace_service_saves_markdown_doc(monkeypatch):
    monkeypatch.setattr(settings, "feishu_root_folder_token", "new_root")
    fake = FakeFeishuClient()
    service = FeishuWorkspaceService(feishu=fake)

    result = asyncio.run(service.save_markdown_document(title="报告", markdown="# 报告\n内容"))

    assert result.document_id == "docx_123"
    assert result.folder_token == "new_root"
    assert result.url.endswith("/docx/docx_123")
    assert fake.moves == [("docx_123", "new_root", "docx")]
    assert fake.appended_blocks


def test_workspace_service_chunks_large_markdown_into_multiple_append_calls(monkeypatch):
    monkeypatch.setattr(settings, "feishu_root_folder_token", "new_root")
    fake = FakeFeishuClient()
    service = FeishuWorkspaceService(feishu=fake)
    markdown = "\n".join(f"- 第 {index} 行" for index in range(1, 70))

    result = asyncio.run(service.save_markdown_document(title="长报告", markdown=markdown))

    assert result.document_id == "docx_123"
    assert len(fake.append_calls) == 2
    assert len(fake.append_calls[0]) == 50
    assert len(fake.append_calls[1]) == 19


def test_workspace_service_reads_doc_and_file_sources():
    service = FeishuWorkspaceService(feishu=FakeFeishuClient())

    doc = asyncio.run(service.read_reference("https://feishu.test/docx/doc_abc"))
    file_item = asyncio.run(service.read_reference("https://feishu.test/file/file_abc"))

    assert doc["type"] == "feishu_doc"
    assert doc["content_json"]["document_id"] == "doc_abc"
    assert file_item["type"] == "feishu_file"
    assert "# hello" in file_item["text_content"]
