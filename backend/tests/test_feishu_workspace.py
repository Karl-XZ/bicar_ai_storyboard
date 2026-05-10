import asyncio
import io
import zipfile

from app.adapters.feishu import FeishuApiError
from app.core.config import settings
from app.services.feishu_workspace import FeishuWorkspaceService


class FakeFeishuClient:
    def __init__(self) -> None:
        self.moves = []
        self.uploads = []

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

    result = asyncio.run(service.save_markdown_document(title="报告", markdown="# 报告\n内容 [来源](https://example.com)"))

    assert result.document_id == "file_123"
    assert result.folder_token == "new_root"
    assert result.url.endswith("/file/file_123")
    assert fake.moves == []
    assert fake.uploads[0][0] == "new_root"
    assert fake.uploads[0][1].endswith(".docx")
    with zipfile.ZipFile(io.BytesIO(fake.uploads[0][2])) as archive:
        assert "word/document.xml" in archive.namelist()
        assert "word/numbering.xml" in archive.namelist()
        document_xml = archive.read("word/document.xml").decode("utf-8")
        rels_xml = archive.read("word/_rels/document.xml.rels").decode("utf-8")
        assert "报告" in document_xml
        assert "内容" in document_xml
        assert 'Target="https://example.com"' in rels_xml


def test_workspace_service_renders_real_links_and_lists(monkeypatch):
    monkeypatch.setattr(settings, "feishu_root_folder_token", "new_root")
    fake = FakeFeishuClient()
    service = FeishuWorkspaceService(feishu=fake)
    markdown = "\n".join(
        [
            "# 研究摘要",
            "- 第一条 [来源一](https://example.com/a)",
            "- 第二条",
            "1. 编号一",
            "2. 编号二",
        ]
    )

    asyncio.run(service.save_markdown_document(title="结构测试", markdown=markdown))

    with zipfile.ZipFile(io.BytesIO(fake.uploads[0][2])) as archive:
        document_xml = archive.read("word/document.xml").decode("utf-8")
        rels_xml = archive.read("word/_rels/document.xml.rels").decode("utf-8")
        numbering_xml = archive.read("word/numbering.xml").decode("utf-8")
        assert "<w:hyperlink " in document_xml
        assert "<w:numPr>" in document_xml
        assert 'Target="https://example.com/a"' in rels_xml
        assert 'w:numFmt w:val="bullet"' in numbering_xml
        assert 'w:numFmt w:val="decimal"' in numbering_xml


def test_workspace_service_renders_large_markdown_as_docx(monkeypatch):
    monkeypatch.setattr(settings, "feishu_root_folder_token", "new_root")
    fake = FakeFeishuClient()
    service = FeishuWorkspaceService(feishu=fake)
    markdown = "\n".join(f"- 第 {index} 行" for index in range(1, 70))

    result = asyncio.run(service.save_markdown_document(title="长报告", markdown=markdown))

    assert result.document_id == "file_123"
    with zipfile.ZipFile(io.BytesIO(fake.uploads[0][2])) as archive:
        document_xml = archive.read("word/document.xml").decode("utf-8")
        assert "第 1 行" in document_xml
        assert "第 69 行" in document_xml


def test_workspace_service_reads_doc_and_file_sources():
    service = FeishuWorkspaceService(feishu=FakeFeishuClient())

    doc = asyncio.run(service.read_reference("https://feishu.test/docx/doc_abc"))
    file_item = asyncio.run(service.read_reference("https://feishu.test/file/file_abc"))

    assert doc["type"] == "feishu_doc"
    assert doc["content_json"]["document_id"] == "doc_abc"
    assert file_item["type"] == "feishu_file"
    assert "# hello" in file_item["text_content"]


def test_workspace_service_uploads_to_default_workspace_when_target_folder_is_missing(monkeypatch):
    monkeypatch.setattr(settings, "feishu_workspace_parent_url", "https://feishu.test/drive/folder/parent_folder")
    monkeypatch.setattr(settings, "feishu_workspace_folder_name", "AI分镜")
    fake = FakeFeishuClient()

    async def failing_upload(folder_token: str, name: str, content: bytes) -> dict:
        fake.uploads.append((folder_token, name, content))
        if folder_token == "deleted_folder":
            raise FeishuApiError("Feishu HTTP error: 400, msg=parent node not exist.")
        return {"code": 0, "data": {"file_token": "file_456"}}

    fake.upload_file = failing_upload
    service = FeishuWorkspaceService(feishu=fake)

    result = asyncio.run(service.save_markdown_document(title="回退文档", markdown="# 回退", folder_token="deleted_folder"))

    assert result.document_id == "file_456"
    assert result.folder_token == "new_root"
    assert [item[0] for item in fake.uploads] == ["deleted_folder", "new_root"]
