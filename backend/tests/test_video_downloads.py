from types import SimpleNamespace
from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routes import webhooks
from app.db.session import get_db
from app.main import app
from app.models import Base
from app.services import bot_commands
from app.services.chat_memory import ChatMemoryService
from app.services.video_downloads import (
    DOWNLOAD_STATUS_DONE,
    DOWNLOAD_STATUS_FAILED,
    DOWNLOAD_STATUS_START,
    DOWNLOAD_STATUS_RUNNING,
    VideoDownloadRequest,
    VideoDownloadResult,
    VideoDownloadService,
    VideoDownloadWorkspace,
)
import app.services.video_downloads as video_downloads


def make_db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_parse_request_from_conversation_extracts_url_filename_and_comments():
    service = VideoDownloadService()

    request = service.parse_request_from_conversation(
        "帮我下载这个视频，文件名改成 6450钛金广告参考片 https://www.youtube.com/watch?v=abc123",
        source_session="group:oc_123",
    )

    assert request == VideoDownloadRequest(
        url="https://www.youtube.com/watch?v=abc123",
        comments="帮我下载这个视频，文件名改成 6450钛金广告参考片 https://www.youtube.com/watch?v=abc123",
        filename_hint="6450钛金广告参考片",
        source_session="group:oc_123",
    )


def test_video_download_table_includes_target_folder_field():
    field_names = {definition["field_name"] for definition in video_downloads._video_download_field_definitions()}

    assert "目标文件夹" in field_names


def test_process_chat_download_task_updates_record_and_uploads(monkeypatch):
    class FakeFeishu:
        def __init__(self):
            self.records = {}
            self.folder_items = {"fld_001": [], "fld_non_doc": []}

        async def batch_create_records(self, app_token, table_id, records):
            self.records["rec_001"] = {"record_id": "rec_001", "fields": dict(records[0]["fields"])}
            return {"data": {"records": [{"record_id": "rec_001"}]}}

        async def search_records(self, app_token, table_id, filter_payload):
            return {"data": {"items": list(self.records.values())}}

        async def list_folder_items(self, folder_token, *, page_size=200, page_token=None):
            return {"data": {"items": list(self.folder_items.get(folder_token, [])), "has_more": False}}

        async def create_folder(self, parent_token, name):
            assert parent_token == "fld_001"
            assert name == "非文档视频下载"
            item = {
                "name": name,
                "type": "folder",
                "token": "fld_non_doc",
                "url": "https://feishu.cn/drive/folder/fld_non_doc",
            }
            self.folder_items["fld_001"].append(item)
            return {"data": {"folder_token": "fld_non_doc", "url": item["url"]}}

        async def batch_update_records(self, app_token, table_id, records):
            for record in records:
                self.records[record["record_id"]]["fields"].update(record["fields"])
            return {"data": {"records": records}}

    class FakeWorkspace:
        async def upload_file_with_fallback(self, *, target_folder, name, content):
            assert target_folder == "fld_non_doc"
            return {"data": {"file_token": "file_tok_001", "file": {"file_token": "file_tok_001"}}}, target_folder

    feishu = FakeFeishu()
    service = VideoDownloadService(feishu=feishu, workspace=FakeWorkspace())
    workspace = VideoDownloadWorkspace(
        folder_token="fld_001",
        folder_url="https://feishu.cn/drive/folder/fld_001",
        app_token="app_001",
        table_id="tbl_001",
        table_url="https://feishu.cn/base/app_001?table=tbl_001",
    )

    async def fake_download(url: str, *, filename_hint: str | None = None):
        return SimpleNamespace(
            file_name="video-title.mp4",
            content=b"video-bytes",
            log="download ok",
        )

    monkeypatch.setattr(service, "_download_video", fake_download)

    result = bot_commands.asyncio.run(
        service.create_chat_download_task_in_workspace(
            workspace=workspace,
            request=VideoDownloadRequest(
                url="https://www.youtube.com/watch?v=abc123",
                comments="帮我下载这个视频",
                filename_hint=None,
                source_session="private:ou_1",
            ),
        )
    )

    assert result.status == DOWNLOAD_STATUS_DONE
    assert result.file_url == "https://feishu.cn/file/file_tok_001"
    fields = feishu.records["rec_001"]["fields"]
    assert fields["下载状态"] == DOWNLOAD_STATUS_DONE
    assert fields["文件名"] == "video-title.mp4"
    assert fields["文件位置"]["link"] == "https://feishu.cn/file/file_tok_001"
    assert fields["目标文件夹"]["link"].endswith("/drive/folder/fld_non_doc")
    assert fields["目标文件夹"]["text"] == "非文档视频下载"
    assert "自动命名：video-title.mp4" in fields["comments"]


def test_document_sourced_download_uses_document_child_folder(monkeypatch):
    class FakeFeishu:
        def __init__(self):
            self.records = {}
            self.folder_items = {"fld_001": [], "fld_doc": []}

        async def batch_create_records(self, app_token, table_id, records):
            self.records["rec_doc"] = {"record_id": "rec_doc", "fields": dict(records[0]["fields"])}
            return {"data": {"records": [{"record_id": "rec_doc"}]}}

        async def search_records(self, app_token, table_id, filter_payload):
            return {"data": {"items": list(self.records.values())}}

        async def list_folder_items(self, folder_token, *, page_size=200, page_token=None):
            return {"data": {"items": list(self.folder_items.get(folder_token, [])), "has_more": False}}

        async def create_folder(self, parent_token, name):
            assert parent_token == "fld_001"
            assert name == "克尔维特正文"
            item = {
                "name": name,
                "type": "folder",
                "token": "fld_doc",
                "url": "https://feishu.cn/drive/folder/fld_doc",
            }
            self.folder_items["fld_001"].append(item)
            return {"data": {"folder_token": "fld_doc", "url": item["url"]}}

        async def batch_update_records(self, app_token, table_id, records):
            for record in records:
                self.records[record["record_id"]]["fields"].update(record["fields"])
            return {"data": {"records": records}}

    class FakeWorkspace:
        async def upload_file_with_fallback(self, *, target_folder, name, content):
            assert target_folder == "fld_doc"
            return {"data": {"file_token": "file_tok_doc", "file": {"file_token": "file_tok_doc"}}}, target_folder

    feishu = FakeFeishu()
    service = VideoDownloadService(feishu=feishu, workspace=FakeWorkspace())
    workspace = VideoDownloadWorkspace(
        folder_token="fld_001",
        folder_url="https://feishu.cn/drive/folder/fld_001",
        app_token="app_001",
        table_id="tbl_001",
        table_url="https://feishu.cn/base/app_001?table=tbl_001",
    )

    async def fake_download(url: str, *, filename_hint: str | None = None):
        return SimpleNamespace(
            file_name="doc-video.mp4",
            content=b"video-bytes",
            log="download ok",
            downloader="yt-dlp",
            resolution="1920x1080",
        )

    monkeypatch.setattr(service, "_download_video", fake_download)

    result = bot_commands.asyncio.run(
        service.create_chat_download_task_in_workspace(
            workspace=workspace,
            request=VideoDownloadRequest(
                url="https://www.youtube.com/watch?v=QYY9GUrrkds",
                comments=(
                    "来源文档：克尔维特正文 https://ocnwptzvwvt6.feishu.cn/docx/WUl1dWrkUocDlzxZ655cnmERnwc\n"
                    "批注说明：GM纪录片：哈雷·厄尔——通用汽车设计大师"
                ),
                filename_hint=None,
                source_session="private:ou_1",
            ),
        )
    )

    assert result.status == DOWNLOAD_STATUS_DONE
    assert result.folder_url.endswith("/drive/folder/fld_doc")
    fields = feishu.records["rec_doc"]["fields"]
    assert fields["目标文件夹"]["link"].endswith("/drive/folder/fld_doc")
    assert fields["目标文件夹"]["text"] == "克尔维特正文"


def test_duplicate_url_is_scoped_by_document_folder(monkeypatch):
    class FakeFeishu:
        def __init__(self):
            self.created_records = []
            self.records = {
                "rec_existing": {
                    "record_id": "rec_existing",
                    "fields": {
                        "链接": {"link": "https://www.youtube.com/watch?v=sameid"},
                        "下载状态": DOWNLOAD_STATUS_DONE,
                        "文件名": "doc-a.mp4",
                        "文件位置": {"link": "https://feishu.cn/file/doc-a", "text": "doc-a.mp4"},
                        "目标文件夹": {"link": "https://feishu.cn/drive/folder/doc_a", "text": "文档_DOC_A_TOKEN"},
                        "comments": "来源文档：https://ocnwptzvwvt6.feishu.cn/docx/DOCAAAAAAAAAAAA",
                    },
                }
            }
            self.folder_items = {"fld_001": [], "fld_doc_b": []}

        async def batch_create_records(self, app_token, table_id, records):
            self.created_records.append(records[0]["fields"])
            self.records["rec_new"] = {"record_id": "rec_new", "fields": dict(records[0]["fields"])}
            return {"data": {"records": [{"record_id": "rec_new"}]}}

        async def search_records(self, app_token, table_id, filter_payload):
            return {"data": {"items": list(self.records.values())}}

        async def list_folder_items(self, folder_token, *, page_size=200, page_token=None):
            return {"data": {"items": list(self.folder_items.get(folder_token, [])), "has_more": False}}

        async def create_folder(self, parent_token, name):
            item = {
                "name": name,
                "type": "folder",
                "token": "fld_doc_b",
                "url": "https://feishu.cn/drive/folder/fld_doc_b",
            }
            self.folder_items["fld_001"].append(item)
            return {"data": {"folder_token": "fld_doc_b", "url": item["url"]}}

        async def batch_update_records(self, app_token, table_id, records):
            for record in records:
                self.records[record["record_id"]]["fields"].update(record["fields"])
            return {"data": {"records": records}}

    class FakeWorkspace:
        async def upload_file_with_fallback(self, *, target_folder, name, content):
            assert target_folder == "fld_doc_b"
            return {"data": {"file_token": "file_tok_b", "file": {"file_token": "file_tok_b"}}}, target_folder

    feishu = FakeFeishu()
    service = VideoDownloadService(feishu=feishu, workspace=FakeWorkspace())
    workspace = VideoDownloadWorkspace(
        folder_token="fld_001",
        folder_url="https://feishu.cn/drive/folder/fld_001",
        app_token="app_001",
        table_id="tbl_001",
        table_url="https://feishu.cn/base/app_001?table=tbl_001",
    )

    async def fake_download(url: str, *, filename_hint: str | None = None):
        return SimpleNamespace(file_name="doc-b.mp4", content=b"bytes", log="download ok")

    monkeypatch.setattr(service, "_download_video", fake_download)

    result = bot_commands.asyncio.run(
        service.create_chat_download_task_in_workspace(
            workspace=workspace,
            request=VideoDownloadRequest(
                url="https://youtu.be/sameid",
                comments="来源文档：https://ocnwptzvwvt6.feishu.cn/docx/DOCBBBBBBBBBBBBB",
                filename_hint=None,
                source_session="private:ou_1",
            ),
        )
    )

    assert result.record_id == "rec_new"
    assert result.status == DOWNLOAD_STATUS_DONE
    assert feishu.created_records


def test_organize_existing_downloads_moves_records_and_orphan_root_videos():
    class FakeFeishu:
        def __init__(self):
            self.records = {
                "rec_doc": {
                    "record_id": "rec_doc",
                    "fields": {
                        "链接": {"link": "https://www.youtube.com/watch?v=doc123"},
                        "下载状态": DOWNLOAD_STATUS_DONE,
                        "文件名": "doc-video.mp4",
                        "文件位置": {"link": "https://feishu.cn/file/file_doc", "text": "doc-video.mp4"},
                        "comments": "旧记录没有可靠来源文档字段",
                    },
                },
                "rec_non_doc": {
                    "record_id": "rec_non_doc",
                    "fields": {
                        "链接": {"link": "https://www.youtube.com/watch?v=plain123"},
                        "下载状态": DOWNLOAD_STATUS_DONE,
                        "文件名": "plain-video.mp4",
                        "文件位置": {"link": "https://feishu.cn/file/file_plain", "text": "plain-video.mp4"},
                        "comments": "普通聊天下载",
                    },
                },
            }
            self.folder_items = {
                "fld_001": [
                    {"name": "doc-video.mp4", "type": "file", "token": "file_doc"},
                    {"name": "plain-video.mp4", "type": "file", "token": "file_plain"},
                    {"name": "loose-video.mp4", "type": "file", "token": "file_loose"},
                    {"name": "下载任务", "type": "bitable", "token": "app_001"},
                ],
            }
            self.moves = []

        async def search_records(self, app_token, table_id, filter_payload):
            return {"data": {"items": list(self.records.values()), "has_more": False}}

        async def list_folder_items(self, folder_token, *, page_size=200, page_token=None):
            return {"data": {"items": list(self.folder_items.get(folder_token, [])), "has_more": False}}

        async def create_folder(self, parent_token, name):
            token = {
                "克尔维特正文": "fld_doc",
                "非文档视频下载": "fld_non_doc",
            }[name]
            item = {"name": name, "type": "folder", "token": token, "url": f"https://feishu.cn/drive/folder/{token}"}
            self.folder_items.setdefault("fld_001", []).append(item)
            self.folder_items.setdefault(token, [])
            return {"data": {"folder_token": token, "url": item["url"]}}

        async def move_file(self, file_token, *, folder_token, file_type="file"):
            self.moves.append({"file_token": file_token, "folder_token": folder_token, "file_type": file_type})
            return {"data": {"file_token": file_token}}

        async def batch_update_records(self, app_token, table_id, records):
            for record in records:
                self.records[record["record_id"]]["fields"].update(record["fields"])
            return {"data": {"records": records}}

        async def get_document_metadata(self, document_id):
            assert document_id == "WUl1dWrkUocDlzxZ655cnmERnwc"
            return {"data": {"document": {"title": "克尔维特正文"}}}

        async def get_document_raw_content(self, document_id):
            return {"data": {"content": ""}}

        async def list_file_comments(self, file_token, *, file_type="docx", page_size=100, page_token=None):
            assert file_token == "WUl1dWrkUocDlzxZ655cnmERnwc"
            return {
                "data": {
                    "items": [
                        {
                            "content": {
                                "elements": [
                                    {
                                        "type": "text_run",
                                        "text_run": {"text": "https://www.youtube.com/watch?v=doc123"},
                                    }
                                ]
                            }
                        }
                    ],
                    "has_more": False,
                }
            }

    feishu = FakeFeishu()
    service = VideoDownloadService(feishu=feishu, workspace=SimpleNamespace())
    workspace = VideoDownloadWorkspace(
        folder_token="fld_001",
        folder_url="https://feishu.cn/drive/folder/fld_001",
        app_token="app_001",
        table_id="tbl_001",
        table_url="https://feishu.cn/base/app_001?table=tbl_001",
    )

    report = bot_commands.asyncio.run(
        service.organize_existing_downloads(
            workspace=workspace,
            document_urls=("https://ocnwptzvwvt6.feishu.cn/docx/WUl1dWrkUocDlzxZ655cnmERnwc",),
        )
    )

    assert report.records_scanned == 2
    assert report.records_updated == 2
    assert report.record_files_moved == 2
    assert report.orphan_root_videos_moved == 1
    assert feishu.records["rec_doc"]["fields"]["目标文件夹"]["text"] == "克尔维特正文"
    assert feishu.records["rec_non_doc"]["fields"]["目标文件夹"]["text"] == "非文档视频下载"
    assert feishu.moves == [
        {"file_token": "file_doc", "folder_token": "fld_doc", "file_type": "file"},
        {"file_token": "file_plain", "folder_token": "fld_non_doc", "file_type": "file"},
        {"file_token": "file_loose", "folder_token": "fld_non_doc", "file_type": "file"},
    ]


def test_process_record_prefers_youtube_title_over_comment_context(monkeypatch):
    class FakeFeishu:
        def __init__(self):
            self.records = {
                "rec_001": {
                    "record_id": "rec_001",
                    "fields": {
                        "链接": {"link": "https://www.youtube.com/watch?v=4BPl1oOWcVQ"},
                        "下载状态": DOWNLOAD_STATUS_START,
                        "文件名": "克尔维特_4BPl1oOWcVQ.mp4",
                        "comments": "克尔维特文档评论 | 这座21世纪的灯塔工厂内部生产秩序井然",
                    },
                }
            }

        async def search_records(self, app_token, table_id, filter_payload):
            return {"data": {"items": list(self.records.values())}}

        async def list_folder_items(self, folder_token, *, page_size=200, page_token=None):
            return {"data": {"items": [], "has_more": False}}

        async def batch_update_records(self, app_token, table_id, records):
            for record in records:
                self.records[record["record_id"]]["fields"].update(record["fields"])
            return {"data": {"records": records}}

    class FakeWorkspace:
        async def upload_file_with_fallback(self, *, target_folder, name, content):
            return {"data": {"file_token": "file_tok_001", "file": {"file_token": "file_tok_001"}}}, target_folder

    feishu = FakeFeishu()
    service = VideoDownloadService(feishu=feishu, workspace=FakeWorkspace())
    workspace = VideoDownloadWorkspace(
        folder_token="fld_001",
        folder_url="https://feishu.cn/drive/folder/fld_001",
        app_token="app_001",
        table_id="tbl_001",
        table_url="https://feishu.cn/base/app_001?table=tbl_001",
    )

    async def fake_download(url: str, *, filename_hint: str | None = None):
        assert filename_hint is None
        return SimpleNamespace(file_name="Inside GM Factory.mp4", content=b"bytes", log="download ok", downloader="yt-dlp", resolution="1920x1080")

    monkeypatch.setattr(service, "_download_video", fake_download)

    result = bot_commands.asyncio.run(service.process_record(workspace=workspace, record_id="rec_001"))

    assert result.status == DOWNLOAD_STATUS_DONE
    assert feishu.records["rec_001"]["fields"]["文件名"] == "Inside GM Factory（批注内容：这座21世纪的灯塔工厂内部生产秩序井然）.mp4"
    assert feishu.records["rec_001"]["fields"]["清晰度"] == "1920x1080"
    assert feishu.records["rec_001"]["fields"]["下载器"] == "yt-dlp"


def test_process_record_explicit_filename_overrides_youtube_title_and_comment(monkeypatch):
    class FakeFeishu:
        def __init__(self):
            self.records = {
                "rec_001": {
                    "record_id": "rec_001",
                    "fields": {
                        "链接": {"link": "https://www.youtube.com/watch?v=4BPl1oOWcVQ"},
                        "下载状态": DOWNLOAD_STATUS_START,
                        "文件名": "用户指定文件名.mp4",
                        "comments": "克尔维特文档评论 | 这座21世纪的灯塔工厂内部生产秩序井然",
                    },
                }
            }

        async def search_records(self, app_token, table_id, filter_payload):
            return {"data": {"items": list(self.records.values())}}

        async def list_folder_items(self, folder_token, *, page_size=200, page_token=None):
            return {"data": {"items": [], "has_more": False}}

        async def batch_update_records(self, app_token, table_id, records):
            for record in records:
                self.records[record["record_id"]]["fields"].update(record["fields"])
            return {"data": {"records": records}}

    class FakeWorkspace:
        async def upload_file_with_fallback(self, *, target_folder, name, content):
            assert name == "用户指定文件名.mp4"
            return {"data": {"file_token": "file_tok_001", "file": {"file_token": "file_tok_001"}}}, target_folder

    feishu = FakeFeishu()
    service = VideoDownloadService(feishu=feishu, workspace=FakeWorkspace())
    workspace = VideoDownloadWorkspace(
        folder_token="fld_001",
        folder_url="https://feishu.cn/drive/folder/fld_001",
        app_token="app_001",
        table_id="tbl_001",
        table_url="https://feishu.cn/base/app_001?table=tbl_001",
    )

    async def fake_download(url: str, *, filename_hint: str | None = None):
        assert filename_hint == "用户指定文件名.mp4"
        return SimpleNamespace(file_name=filename_hint, content=b"bytes", log="download ok", downloader="yt-dlp", resolution="1920x1080")

    monkeypatch.setattr(service, "_download_video", fake_download)

    result = bot_commands.asyncio.run(service.process_record(workspace=workspace, record_id="rec_001"))

    assert result.status == DOWNLOAD_STATUS_DONE
    assert feishu.records["rec_001"]["fields"]["文件名"] == "用户指定文件名.mp4"


def test_process_record_uses_text_from_same_comment_as_youtube_link(monkeypatch):
    class FakeFeishu:
        def __init__(self):
            self.records = {
                "rec_001": {
                    "record_id": "rec_001",
                    "fields": {
                        "链接": {"link": "https://www.youtube.com/watch?v=QYY9GUrrkds"},
                        "下载状态": DOWNLOAD_STATUS_START,
                        "文件名": "Harley Earl - GM Designer Extraordinaire.mp4",
                        "comments": (
                            "来源文档：https://ocnwptzvwvt6.feishu.cn/docx/WUl1dWrkUocDlzxZ655cnmERnwc\n"
                            "批注 quote：哈利 J 厄尔\n"
                            "批注 ID：7646619131944389856\n"
                            "备注：https://www.youtube.com/watch?v=QYY9GUrrkds "
                            "GM纪录片：哈雷·厄尔——通用汽车设计大师\n"
                            "原始链接：https://www.youtube.com/watch?v=QYY9GUrrkds"
                        ),
                    },
                }
            }

        async def search_records(self, app_token, table_id, filter_payload):
            return {"data": {"items": list(self.records.values())}}

        async def list_folder_items(self, folder_token, *, page_size=200, page_token=None):
            return {"data": {"items": [], "has_more": False}}

        async def batch_update_records(self, app_token, table_id, records):
            for record in records:
                self.records[record["record_id"]]["fields"].update(record["fields"])
            return {"data": {"records": records}}

    class FakeWorkspace:
        async def upload_file_with_fallback(self, *, target_folder, name, content):
            assert name == "GM纪录片：哈雷·厄尔——通用汽车设计大师.mp4"
            return {"data": {"file_token": "file_tok_001", "file": {"file_token": "file_tok_001"}}}, target_folder

    feishu = FakeFeishu()
    service = VideoDownloadService(feishu=feishu, workspace=FakeWorkspace())
    workspace = VideoDownloadWorkspace(
        folder_token="fld_001",
        folder_url="https://feishu.cn/drive/folder/fld_001",
        app_token="app_001",
        table_id="tbl_001",
        table_url="https://feishu.cn/base/app_001?table=tbl_001",
    )

    async def fake_download(url: str, *, filename_hint: str | None = None):
        assert filename_hint == "GM纪录片：哈雷·厄尔——通用汽车设计大师"
        return SimpleNamespace(file_name=f"{filename_hint}.mp4", content=b"bytes", log="download ok", downloader="yt-dlp", resolution="1920x1080")

    monkeypatch.setattr(service, "_download_video", fake_download)

    result = bot_commands.asyncio.run(service.process_record(workspace=workspace, record_id="rec_001"))

    assert result.status == DOWNLOAD_STATUS_DONE
    assert feishu.records["rec_001"]["fields"]["文件名"] == "GM纪录片：哈雷·厄尔——通用汽车设计大师.mp4"


def test_process_record_deduplicates_upload_filename(monkeypatch):
    class FakeFeishu:
        def __init__(self):
            self.records = {
                "rec_existing": {
                    "record_id": "rec_existing",
                    "fields": {
                        "链接": {"link": "https://www.youtube.com/watch?v=old"},
                        "下载状态": DOWNLOAD_STATUS_DONE,
                        "文件名": "Motorama车展.mp4",
                        "文件位置": {"link": "https://feishu.cn/file/old", "text": "Motorama车展.mp4"},
                    },
                },
                "rec_new": {
                    "record_id": "rec_new",
                    "fields": {
                        "链接": {"link": "https://www.youtube.com/watch?v=EYYc3zs2kg8"},
                        "下载状态": DOWNLOAD_STATUS_START,
                        "文件名": "",
                        "comments": "下载这个视频",
                    },
                },
            }

        async def search_records(self, app_token, table_id, filter_payload):
            return {"data": {"items": list(self.records.values())}}

        async def list_folder_items(self, folder_token, *, page_size=200, page_token=None):
            return {"data": {"items": [{"name": "Motorama车展.mp4", "type": "file"}], "has_more": False}}

        async def batch_update_records(self, app_token, table_id, records):
            for record in records:
                self.records[record["record_id"]]["fields"].update(record["fields"])
            return {"data": {"records": records}}

    class FakeWorkspace:
        async def upload_file_with_fallback(self, *, target_folder, name, content):
            assert name == "Motorama车展_EYYc3zs2kg8.mp4"
            return {"data": {"file_token": "file_tok_new", "file": {"file_token": "file_tok_new"}}}, target_folder

    feishu = FakeFeishu()
    service = VideoDownloadService(feishu=feishu, workspace=FakeWorkspace())
    workspace = VideoDownloadWorkspace(
        folder_token="fld_001",
        folder_url="https://feishu.cn/drive/folder/fld_001",
        app_token="app_001",
        table_id="tbl_001",
        table_url="https://feishu.cn/base/app_001?table=tbl_001",
    )

    async def fake_download(url: str, *, filename_hint: str | None = None):
        return SimpleNamespace(file_name="Motorama车展.mp4", content=b"bytes", log="download ok", downloader="yt-dlp", resolution="1280x720")

    monkeypatch.setattr(service, "_download_video", fake_download)

    result = bot_commands.asyncio.run(service.process_record(workspace=workspace, record_id="rec_new"))

    assert result.file_name == "Motorama车展_EYYc3zs2kg8.mp4"
    assert feishu.records["rec_new"]["fields"]["文件名"] == "Motorama车展_EYYc3zs2kg8.mp4"


def test_process_record_retries_three_times_before_failure(monkeypatch):
    monkeypatch.setattr(video_downloads, "DOWNLOAD_RETRY_BACKOFF_SECONDS", (0, 0, 0))

    class FakeFeishu:
        def __init__(self):
            self.records = {
                "rec_retry": {
                    "record_id": "rec_retry",
                    "fields": {
                        "链接": {"link": "https://www.youtube.com/watch?v=retry123"},
                        "下载状态": DOWNLOAD_STATUS_START,
                        "文件名": "retry.mp4",
                        "comments": "retry",
                    },
                }
            }
            self.update_count = 0

        async def search_records(self, app_token, table_id, filter_payload):
            return {"data": {"items": list(self.records.values())}}

        async def batch_update_records(self, app_token, table_id, records):
            self.update_count += 1
            for record in records:
                self.records[record["record_id"]]["fields"].update(record["fields"])
            return {"data": {"records": records}}

    feishu = FakeFeishu()
    service = VideoDownloadService(feishu=feishu, workspace=SimpleNamespace())
    workspace = VideoDownloadWorkspace(
        folder_token="fld_001",
        folder_url="https://feishu.cn/drive/folder/fld_001",
        app_token="app_001",
        table_id="tbl_001",
        table_url="https://feishu.cn/base/app_001?table=tbl_001",
    )
    attempts = 0

    async def always_fail(url: str, *, filename_hint: str | None = None):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("network boom")

    monkeypatch.setattr(service, "_download_video", always_fail)

    result = bot_commands.asyncio.run(service.process_record(workspace=workspace, record_id="rec_retry"))

    assert attempts == 4
    assert result.status == DOWNLOAD_STATUS_FAILED
    log = feishu.records["rec_retry"]["fields"]["log"]
    assert "初始尝试 + 3 次自动重试均未成功" in log
    assert "network boom" in log


def test_create_chat_download_task_reuses_existing_download(monkeypatch):
    class FakeFeishu:
        def __init__(self):
            self.create_calls = 0
            self.records = {
                "rec_existing": {
                    "record_id": "rec_existing",
                    "fields": {
                        "链接": {
                            "link": "https://www.youtube.com/watch?v=abc123",
                            "text": "https://www.youtube.com/watch?v=abc123",
                        },
                        "下载状态": DOWNLOAD_STATUS_DONE,
                        "文件名": "existing.mp4",
                        "文件位置": {"link": "https://feishu.cn/file/existing", "text": "existing.mp4"},
                        "log": "downloaded earlier",
                    },
                }
            }

        async def batch_create_records(self, app_token, table_id, records):
            self.create_calls += 1
            raise AssertionError("重复链接不应新建下载记录")

        async def search_records(self, app_token, table_id, filter_payload):
            return {"data": {"items": list(self.records.values())}}

        async def batch_update_records(self, app_token, table_id, records):
            raise AssertionError("已完成记录不应被重写")

    feishu = FakeFeishu()
    service = VideoDownloadService(feishu=feishu, workspace=SimpleNamespace())
    workspace = VideoDownloadWorkspace(
        folder_token="fld_001",
        folder_url="https://feishu.cn/drive/folder/fld_001",
        app_token="app_001",
        table_id="tbl_001",
        table_url="https://feishu.cn/base/app_001?table=tbl_001",
    )

    async def fake_download(url: str, *, filename_hint: str | None = None):
        raise AssertionError("重复链接不应再次下载")

    monkeypatch.setattr(service, "_download_video", fake_download)

    result = bot_commands.asyncio.run(
        service.create_chat_download_task_in_workspace(
            workspace=workspace,
            request=VideoDownloadRequest(
                url="https://youtu.be/abc123",
                comments="再次下载同一个视频",
                filename_hint=None,
                source_session="private:ou_1",
            ),
        )
    )

    assert result.record_id == "rec_existing"
    assert result.status == DOWNLOAD_STATUS_DONE
    assert result.file_url == "https://feishu.cn/file/existing"
    assert feishu.create_calls == 0


def test_chatbot_reply_does_not_auto_run_video_download_workflow(monkeypatch):
    db = make_db()

    class FakeVideoDownloadService:
        def parse_request_from_conversation(self, text, *, recent_texts=None, source_session=None):
            raise AssertionError("自然语言聊天不应再被后端硬识别并抢先接管到视频下载工作流")

    monkeypatch.setattr(bot_commands, "VideoDownloadService", FakeVideoDownloadService)

    async def fake_generate_chat_response(**kwargs):
        return "普通回复"

    monkeypatch.setattr(bot_commands, "_generate_chat_response", fake_generate_chat_response)

    reply = bot_commands.asyncio.run(
        bot_commands._chatbot_reply(
            db,
            text="帮我下载这个视频并存到飞书 https://www.youtube.com/watch?v=abc123",
            chat_id="oc_p2p",
            chat_type="p2p",
            sender_open_id="ou_alice",
        )
    )

    assert reply == "普通回复"


def test_handle_bot_text_runs_video_download_workflow_for_explicit_command(monkeypatch):
    db = make_db()
    workspace = VideoDownloadWorkspace(
        folder_token="fld_001",
        folder_url="https://feishu.cn/drive/folder/fld_001",
        app_token="app_001",
        table_id="tbl_001",
        table_url="https://feishu.cn/base/app_001?table=tbl_001",
    )

    class FakeVideoDownloadService:
        def parse_request_from_conversation(self, text, *, recent_texts=None, source_session=None):
            assert source_session == "private:ou_alice"
            return VideoDownloadRequest(
                url="https://www.youtube.com/watch?v=abc123",
                comments=text,
                filename_hint="测试视频",
                source_session=source_session,
            )

        async def ensure_workspace(self):
            return workspace

        async def create_chat_download_task_in_workspace(self, *, workspace, request):
            assert request.filename_hint == "测试视频"
            return VideoDownloadResult(
                record_id="rec_001",
                status=DOWNLOAD_STATUS_DONE,
                file_name="测试视频.mp4",
                file_url="https://feishu.cn/file/file_tok_001",
                folder_url=workspace.folder_url,
                log="download ok",
            )

    monkeypatch.setattr(bot_commands, "VideoDownloadService", FakeVideoDownloadService)
    sent_cards = []

    class FakeFeishuClient:
        async def send_card(self, receive_id, card, receive_id_type="chat_id"):
            sent_cards.append((receive_id, card))
            return {"ok": True}

        async def send_text(self, receive_id, text, receive_id_type="chat_id"):
            return {"ok": True}

    monkeypatch.setattr(bot_commands, "FeishuClient", FakeFeishuClient)

    result = bot_commands.asyncio.run(
        bot_commands.handle_bot_text(
            db,
            text="/下载视频 帮我下载这个视频并存到飞书 https://www.youtube.com/watch?v=abc123",
            chat_id="oc_p2p",
            chat_type="p2p",
            sender_open_id="ou_alice",
        )
    )

    assert result["message"] == "视频下载工作流已触发"
    assert sent_cards
    card_text = str(sent_cards[0][1])
    assert "已按视频下载工作流处理" in card_text
    assert workspace.table_url in card_text
    assert "https://feishu.cn/file/file_tok_001" in card_text


def test_bitable_trigger_routes_video_download_table(monkeypatch):
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
                "app_token": "app_download",
                "table_id": "tbl_download",
                "record_id": "rec_001",
            }
        },
    )

    class FakeVideoDownloadService:
        async def handles_table(self, app_token, table_id):
            return app_token == "app_download" and table_id == "tbl_download"

        async def process_record_by_table(self, *, app_token, table_id, record_id):
            assert record_id == "rec_001"
            return VideoDownloadResult(record_id=record_id, status=DOWNLOAD_STATUS_DONE)

    monkeypatch.setattr(webhooks, "VideoDownloadService", FakeVideoDownloadService)
    client = TestClient(app)
    response = client.post("/webhooks/feishu/bitable-trigger", json={})

    assert response.status_code == 202
    assert response.json()["data"]["processed"] == 1
    app.dependency_overrides.clear()


def test_process_record_marks_stale_running_as_failed():
    class FakeFeishu:
        def __init__(self):
            self.records = {}

        async def search_records(self, app_token, table_id, filter_payload):
            return {"data": {"items": list(self.records.values())}}

        async def list_folder_items(self, folder_token, *, page_size=200, page_token=None):
            return {"data": {"items": [], "has_more": False}}

        async def batch_update_records(self, app_token, table_id, records):
            for record in records:
                self.records[record["record_id"]]["fields"].update(record["fields"])
            return {"data": {"records": records}}

    feishu = FakeFeishu()
    service = VideoDownloadService(feishu=feishu, workspace=SimpleNamespace())
    workspace = VideoDownloadWorkspace(
        folder_token="fld_001",
        folder_url="https://feishu.cn/drive/folder/fld_001",
        app_token="app_001",
        table_id="tbl_001",
        table_url="https://feishu.cn/base/app_001?table=tbl_001",
    )
    created_at = (datetime.now() - timedelta(minutes=40)).strftime("%Y-%m-%d %H:%M:%S")
    feishu.records["rec_stale"] = {
        "record_id": "rec_stale",
        "fields": {
            "链接": {"link": "https://example.com/video.mp4", "text": "https://example.com/video.mp4"},
            "下载状态": DOWNLOAD_STATUS_RUNNING,
            "文件名": "stale.mp4",
            "comments": "stale running task",
            "创建时间": created_at,
            "log": "已开始下载，正在调用 videodl。",
        },
    }

    result = bot_commands.asyncio.run(service.process_record(workspace=workspace, record_id="rec_stale"))

    assert result.status == DOWNLOAD_STATUS_FAILED
    assert feishu.records["rec_stale"]["fields"]["下载状态"] == DOWNLOAD_STATUS_FAILED
    assert "长时间停留在“正在下载”" in feishu.records["rec_stale"]["fields"]["log"]


def test_process_record_restarts_when_status_is_start(monkeypatch):
    class FakeFeishu:
        def __init__(self):
            self.records = {}

        async def search_records(self, app_token, table_id, filter_payload):
            return {"data": {"items": list(self.records.values())}}

        async def list_folder_items(self, folder_token, *, page_size=200, page_token=None):
            return {"data": {"items": [], "has_more": False}}

        async def batch_update_records(self, app_token, table_id, records):
            for record in records:
                self.records[record["record_id"]]["fields"].update(record["fields"])
            return {"data": {"records": records}}

    class FakeWorkspace:
        async def upload_file_with_fallback(self, *, target_folder, name, content):
            return {"data": {"file_token": "file_tok_start", "file": {"file_token": "file_tok_start"}}}, target_folder

    feishu = FakeFeishu()
    service = VideoDownloadService(feishu=feishu, workspace=FakeWorkspace())
    workspace = VideoDownloadWorkspace(
        folder_token="fld_001",
        folder_url="https://feishu.cn/drive/folder/fld_001",
        app_token="app_001",
        table_id="tbl_001",
        table_url="https://feishu.cn/base/app_001?table=tbl_001",
    )
    feishu.records["rec_start"] = {
        "record_id": "rec_start",
        "fields": {
            "链接": {"link": "https://example.com/video.mp4", "text": "https://example.com/video.mp4"},
            "下载状态": DOWNLOAD_STATUS_START,
            "文件名": "restart.mp4",
            "comments": "restart task",
            "创建时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        },
    }

    async def fake_download(url: str, *, filename_hint: str | None = None):
        return SimpleNamespace(file_name="restart.mp4", content=b"bytes", log="download ok")

    monkeypatch.setattr(service, "_download_video", fake_download)

    result = bot_commands.asyncio.run(service.process_record(workspace=workspace, record_id="rec_start"))

    assert result.status == DOWNLOAD_STATUS_DONE
    fields = feishu.records["rec_start"]["fields"]
    assert fields["下载状态"] == DOWNLOAD_STATUS_DONE
    assert fields["文件位置"]["link"] == "https://feishu.cn/file/file_tok_start"
