from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.adapters.feishu import FeishuApiError
from app.domain.enums import AssetType, ShotStatus
from app.models.asset import Asset
from app.models import Base
from app.core.config import settings
from app.services.feishu_storyboard import FeishuStoryboardService
from app.services.projects import ProjectService


class FakeFeishuClient:
    def __init__(self) -> None:
        self.records = [
            {
                "record_id": "rec_001",
                "fields": {
                    "场景描述": "清晨厨房，一杯咖啡在木桌上",
                "生成批次": "batch_001",
                "审核状态": "待优化",
                "首帧同步设置": "否",
                "关键帧生成设置": "否",
                "图片生成状态": "未开始",
                "生成状态": "未开始",
                "重新生成状态": "未开始",
                "Prompt 版本": 1,
            },
        }
        ]
        self.sent_cards = []
        self.updated_records = []
        self.uploaded_files = []
        self.moved_files = []
        self.folder_items = {}
        self.fields = [
            {"field_name": "场景描述", "field_id": "fld_0"},
            {"field_name": "生成批次", "field_id": "fld_1"},
            {"field_name": "审核状态", "field_id": "fld_2"},
            {"field_name": "首帧同步设置", "field_id": "fld_3"},
            {"field_name": "关键帧生成设置", "field_id": "fld_4"},
            {"field_name": "图片生成状态", "field_id": "fld_5"},
            {"field_name": "生成状态", "field_id": "fld_6"},
            {"field_name": "重新生成状态", "field_id": "fld_7"},
            {"field_name": "Prompt 版本", "field_id": "fld_8"},
        ]
        self.deleted_fields = []

    async def send_card(self, receive_id: str, card: dict, receive_id_type: str = "chat_id") -> dict:
        self.sent_cards.append({"receive_id": receive_id, "card": card, "receive_id_type": receive_id_type})
        return {"code": 0, "data": {"message_id": "msg_001"}}

    async def create_folder(self, parent_token: str, name: str) -> dict:
        return {"code": 0, "data": {"token": f"fld_{len(name)}_{len(parent_token)}", "url": f"https://feishu.test/{name}"}}

    async def list_folder_items(self, folder_token: str, *, page_size: int = 200, page_token: str | None = None) -> dict:
        if folder_token in self.folder_items:
            return {"code": 0, "data": {"files": self.folder_items[folder_token], "has_more": False}}
        if folder_token == "parent_folder":
            return {
                "code": 0,
                "data": {
                    "files": [
                        {"name": "AI生成", "token": "new_root", "type": "folder", "url": "https://feishu.test/drive/folder/new_root"}
                    ],
                    "has_more": False,
                },
            }
        if folder_token == "new_root":
            return {
                "code": 0,
                "data": {
                    "files": [
                        {"name": "Deep Research", "token": "deep_research", "type": "folder", "url": "https://feishu.test/drive/folder/deep_research"},
                        {"name": "分镜项目", "token": "storyboards", "type": "folder", "url": "https://feishu.test/drive/folder/storyboards"},
                    ],
                    "has_more": False,
                },
            }
        return {"code": 0, "data": {"files": [], "has_more": False}}

    async def move_file(self, file_token: str, *, folder_token: str, file_type: str = "file") -> dict:
        self.moved_files.append({"file_token": file_token, "folder_token": folder_token, "file_type": file_type})
        for source_folder, items in list(self.folder_items.items()):
            self.folder_items[source_folder] = [
                item for item in items if str(item.get("token") or item.get("file_token") or "") != file_token
            ]
        return {"code": 0, "data": {"file_token": file_token}}

    async def create_bitable_app(self, name: str, folder_token: str = "") -> dict:
        return {"code": 0, "data": {"app": {"app_token": "app_001", "url": "https://feishu.test/base/app_001"}}}

    async def create_table(self, app_token: str, table_name: str, fields: list[dict]) -> dict:
        assert any(field["field_name"] == "镜头运动" for field in fields)
        return {"code": 0, "data": {"table": {"table_id": "tbl_001"}}}

    async def search_records(self, app_token: str, table_id: str, payload: dict | None = None) -> dict:
        return {"code": 0, "data": {"items": self.records}}

    async def list_fields(self, app_token: str, table_id: str) -> dict:
        return {"code": 0, "data": {"items": list(self.fields)}}

    async def create_field(self, app_token: str, table_id: str, field: dict) -> dict:
        field_id = f"fld_{len(self.fields)}"
        self.fields.append({"field_name": field["field_name"], "field_id": field_id})
        return {"code": 0, "data": {"field": field}}

    async def delete_field(self, app_token: str, table_id: str, field_id: str) -> dict:
        self.deleted_fields.append(field_id)
        self.fields = [field for field in self.fields if field["field_id"] != field_id]
        return {"code": 0, "data": {"deleted": True, "field_id": field_id}}

    async def subscribe_file_events(self, file_token: str, file_type: str = "bitable") -> dict:
        return {"code": 0, "data": {}}

    async def batch_update_records(self, app_token: str, table_id: str, records: list[dict]) -> dict:
        self.updated_records.extend(records)
        for update in records:
            for record in self.records:
                if record["record_id"] == update["record_id"]:
                    record["fields"].update(update["fields"])
        return {"code": 0, "data": {"records": records}}

    async def batch_create_records(self, app_token: str, table_id: str, records: list[dict]) -> dict:
        created = []
        for record in records:
            item = {"record_id": f"rec_tpl_{len(self.records) + 1:03d}", "fields": dict(record["fields"])}
            self.records.append(item)
            created.append(item)
        return {"code": 0, "data": {"records": created}}

    async def upload_file(self, folder_token: str, name: str, content: bytes) -> dict:
        token = f"file_{len(self.uploaded_files) + 1}"
        self.uploaded_files.append({"folder_token": folder_token, "name": name, "content": content})
        return {"code": 0, "data": {"file_token": token}}

    async def upload_bitable_attachment(self, app_token: str, name: str, content: bytes) -> dict:
        token = f"bitable_file_{len(self.uploaded_files) + 1}"
        self.uploaded_files.append({"app_token": app_token, "name": name, "content": content})
        return {"code": 0, "data": {"file_token": token}}


def make_db():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_feishu_storyboard_real_user_flow(monkeypatch):
    import asyncio

    async def run_flow():
        monkeypatch.setattr(settings, "default_text_provider", "mock")
        monkeypatch.setattr(settings, "default_image_provider", "mock")
        monkeypatch.setattr(settings, "default_video_provider", "mock")
        db = make_db()
        fake = FakeFeishuClient()
        service = FeishuStoryboardService(db, feishu=fake)

        provisioned = await service.create_project_from_bot(project_name="咖啡广告 15 秒", chat_id="oc_test")
        provisioned.project.model_config = {
            "text": {"provider": "mock", "model_id": "mock-text-v1"},
            "image": {"provider": "mock", "model_id": "mock-image-v1"},
            "video": {"provider": "mock", "model_id": "mock-video-v1", "duration_seconds": 5},
        }
        db.commit()
        assert provisioned.table_url == "https://feishu.test/base/app_001?table=tbl_001"
        assert fake.sent_cards
        assert len(fake.records) == 4
        assert fake.records[1]["fields"]["生成批次"] == "batch_001"
        assert fake.records[1]["fields"]["审核状态"] == "草稿"
        assert fake.records[1]["fields"]["首帧同步设置"] == "否"
        assert fake.records[1]["fields"]["关键帧生成设置"] == "否"
        assert fake.records[1]["fields"]["图片生成状态"] == "未开始"
        assert fake.records[1]["fields"]["生成状态"] == "未开始"
        assert fake.records[1]["fields"]["重新生成状态"] == "未开始"
        assert fake.records[1]["fields"]["视频时长"] == 5.0

        optimized = await service.optimize_current_batch(project=provisioned.project, batch_no="batch_001")
        assert optimized[0].status == ShotStatus.PENDING_FRAMES.value
        assert fake.records[0]["fields"]["审核状态"] == "待生成帧"
        assert fake.records[0]["fields"]["视频 Prompt"]

        generated = await service.generate_current_batch(project=provisioned.project, batch_no="batch_001")
        assert generated[0].status == ShotStatus.PENDING_REVIEW.value
        assert fake.records[0]["fields"]["审核状态"] == "待审核"
        assert fake.records[0]["fields"]["图片生成状态"] == "生成完成"
        assert "关键帧图" not in fake.records[0]["fields"]
        assert "选中关键帧图" not in fake.records[0]["fields"]
        assert fake.records[0]["fields"]["首帧图"]
        assert fake.records[0]["fields"]["尾帧图"]

        fake.records[0]["fields"]["审核状态"] = "通过"
        shot = await service.process_record_status(project=provisioned.project, record=fake.records[0])
        assert shot.status == ShotStatus.APPROVED.value
        assert fake.records[0]["fields"]["审核状态"] == "通过"
        assert "视频链接" not in fake.records[0]["fields"]

        folders = provisioned.project.workflow_config["folders"]
        fake.folder_items[folders["videos"]] = [
            {"name": "001_v000_old.mp4", "token": "old_video_token", "type": "file"},
            {"name": "ARCHIVED", "token": folders["videos_archived"], "type": "folder"},
        ]
        fake.records[0]["fields"]["生成状态"] = "启动"
        shot = await service.process_record_status(project=provisioned.project, record=fake.records[0])
        assert shot.status == ShotStatus.PENDING_ACCEPTANCE.value
        assert fake.records[0]["fields"]["审核状态"] == "通过"
        assert fake.records[0]["fields"]["生成状态"] == "生成完成"
        assert fake.records[0]["fields"]["视频链接"]["link"].startswith("https://feishu.test/file/")
        assert fake.moved_files == [
            {"file_token": "old_video_token", "folder_token": folders["videos_archived"], "file_type": "file"}
        ]

        fake.records[0]["fields"]["满意度"] = "满意"
        shot = await service.process_record_status(project=provisioned.project, record=fake.records[0])
        assert shot.status == ShotStatus.ARCHIVED_SATISFIED.value
        assert fake.records[0]["fields"]["审核状态"] == "通过"
        assert fake.records[0]["fields"]["归档链接"]["link"].startswith("https://feishu.test/file/")

        project = ProjectService(db).get_project(provisioned.project.id)
        stats = service.progress_stats(project)
        assert stats["archived"] == 1

    asyncio.run(run_flow())


def test_create_project_from_bot_without_chat_id_does_not_fallback_to_default_chat(monkeypatch):
    import asyncio

    async def run_flow():
        monkeypatch.setattr(settings, "feishu_default_chat_id", "oc_default_group")
        db = make_db()
        fake = FakeFeishuClient()
        service = FeishuStoryboardService(db, feishu=fake)

        provisioned = await service.create_project_from_bot(project_name="无会话项目", chat_id=None)

        assert provisioned.project.workflow_config["chat_id"] is None
        assert fake.sent_cards == []

    asyncio.run(run_flow())


def test_send_progress_without_any_chat_id_does_not_fallback_to_default_chat(monkeypatch):
    import asyncio

    async def run_flow():
        monkeypatch.setattr(settings, "feishu_default_chat_id", "oc_default_group")
        db = make_db()
        fake = FakeFeishuClient()
        service = FeishuStoryboardService(db, feishu=fake)

        provisioned = await service.create_project_from_bot(project_name="进度无会话项目", chat_id="oc_test")
        provisioned.project.workflow_config = {**(provisioned.project.workflow_config or {}), "chat_id": None}
        db.commit()
        fake.sent_cards.clear()

        await service.send_progress(provisioned.project, chat_id=None)

        assert fake.sent_cards == []

    asyncio.run(run_flow())


def test_transition_alignment_requires_previous_tail_before_generating(monkeypatch):
    import asyncio

    async def run_flow():
        monkeypatch.setattr(settings, "default_text_provider", "mock")
        monkeypatch.setattr(settings, "default_image_provider", "mock")
        monkeypatch.setattr(settings, "default_video_provider", "mock")
        db = make_db()
        fake = FakeFeishuClient()
        fake.records = [
            {
                "record_id": "rec_001",
                "fields": {
                    "镜号": "001",
                    "场景描述": "第一镜，角色走进房间",
                    "生成批次": "batch_001",
                    "审核状态": "待优化",
                    "首帧同步设置": "否",
                    "图片生成状态": "未开始",
                    "生成状态": "未开始",
                    "重新生成状态": "未开始",
                    "Prompt 版本": 1,
                },
            },
            {
                "record_id": "rec_002",
                "fields": {
                    "镜号": "002",
                    "场景描述": "第二镜，角色停在窗边",
                    "生成批次": "batch_001",
                    "审核状态": "待优化",
                    "首帧同步设置": "是",
                    "图片生成状态": "启动",
                    "生成状态": "未开始",
                    "重新生成状态": "未开始",
                    "Prompt 版本": 1,
                },
            },
        ]
        service = FeishuStoryboardService(db, feishu=fake)

        provisioned = await service.create_project_from_bot(project_name="首帧同步异常项目", chat_id="oc_test")
        provisioned.project.model_config = {
            "text": {"provider": "mock", "model_id": "mock-text-v1"},
            "image": {"provider": "mock", "model_id": "mock-image-v1"},
            "video": {"provider": "mock", "model_id": "mock-video-v1", "duration_seconds": 5},
        }
        db.commit()
        await service.sync_from_feishu(provisioned.project)

        shot = await service.process_record_status(project=provisioned.project, record=fake.records[1])

        assert shot.status == ShotStatus.PENDING_FRAMES.value
        assert shot.error_code == "TRANSITION_SOURCE_MISSING"
        assert "上一镜 001 还没有可用尾帧" in shot.error_message
        assert fake.records[1]["fields"]["图片生成状态"] == "未开始"
        assert fake.records[1]["fields"]["错误信息"]

    asyncio.run(run_flow())


def test_regeneration_clears_rejection_and_ignores_stale_record(monkeypatch):
    import asyncio

    async def run_flow():
        monkeypatch.setattr(settings, "default_text_provider", "mock")
        monkeypatch.setattr(settings, "default_image_provider", "mock")
        monkeypatch.setattr(settings, "default_video_provider", "mock")
        db = make_db()
        fake = FakeFeishuClient()
        fake.records = [
            {
                "record_id": "rec_001",
                "fields": {
                    "镜号": "001",
                    "场景描述": "夜雨中的霓虹街口，角色回头",
                    "生成批次": "batch_001",
                    "审核状态": "驳回",
                    "生成状态": "未开始",
                    "图片生成状态": "未开始",
                    "重新生成状态": "启动",
                    "需要重新生成的选项": [{"text": "视频提示词"}, {"text": "视频重新生成"}],
                    "驳回原因": "镜头节奏不够紧凑",
                    "Prompt 版本": 1,
                },
            }
        ]
        service = FeishuStoryboardService(db, feishu=fake)

        provisioned = await service.create_project_from_bot(project_name="重生成回归项目", chat_id="oc_test")
        provisioned.project.model_config = {
            "text": {"provider": "mock", "model_id": "mock-text-v1"},
            "image": {"provider": "mock", "model_id": "mock-image-v1"},
            "video": {"provider": "mock", "model_id": "mock-video-v1", "duration_seconds": 5},
        }
        db.commit()
        await service.sync_from_feishu(provisioned.project)

        shot = await service.process_record_status(project=provisioned.project, record=fake.records[0])
        db.refresh(shot)

        assert shot.status == ShotStatus.PENDING_ACCEPTANCE.value
        assert shot.error_code is None
        assert shot.error_message is None
        assert shot.prompt_version == 2
        assert fake.records[0]["fields"]["审核状态"] == "待审核"
        assert fake.records[0]["fields"]["驳回原因"] == ""
        assert fake.records[0]["fields"]["重新生成状态"] == "生成完成"
        assert fake.records[0]["fields"]["生成状态"] == "生成完成"

        stale_record = {
            "record_id": "rec_001",
            "fields": {
                "镜号": "001",
                "场景描述": "夜雨中的霓虹街口，角色回头",
                "生成批次": "batch_001",
                "审核状态": "驳回",
                "生成状态": "正在生成",
                "图片生成状态": "未开始",
                "重新生成状态": "启动",
                "需要重新生成的选项": [{"text": "视频提示词"}, {"text": "视频重新生成"}],
                "驳回原因": "镜头节奏不够紧凑",
                "Prompt 版本": 1,
            },
        }
        stale = service.shots.upsert_from_feishu_record(project_id=provisioned.project.id, record=stale_record)
        db.refresh(stale)

        assert stale.status == ShotStatus.PENDING_ACCEPTANCE.value
        assert stale.error_code is None
        assert stale.error_message is None
        assert stale.prompt_version == 2
        assert stale.prompts["review_status"] == "待审核"
        assert stale.prompts["generation_status"] == "生成完成"
        assert stale.prompts["regeneration_status"] == "生成完成"

    asyncio.run(run_flow())


def test_custom_video_storage_folder_is_used_for_video_backfill(monkeypatch):
    import asyncio

    async def run_flow():
        monkeypatch.setattr(settings, "default_text_provider", "mock")
        monkeypatch.setattr(settings, "default_image_provider", "mock")
        monkeypatch.setattr(settings, "default_video_provider", "mock")
        db = make_db()
        fake = FakeFeishuClient()
        fake.records = [
            {
                "record_id": "rec_001",
                "fields": {
                    "镜号": "001",
                    "场景描述": "产品在台面上缓慢旋转，柔光掠过边缘",
                    "生成批次": "batch_001",
                    "审核状态": "通过",
                    "图片生成状态": "未开始",
                    "生成状态": "启动",
                    "重新生成状态": "未开始",
                    "视频存储位置": {
                        "text": "满意目录",
                        "link": "https://feishu.test/drive/folder/custom_folder_123",
                    },
                    "Prompt 版本": 1,
                },
            }
        ]
        service = FeishuStoryboardService(db, feishu=fake)

        provisioned = await service.create_project_from_bot(project_name="自定义视频目录项目", chat_id="oc_test")
        provisioned.project.model_config = {
            "text": {"provider": "mock", "model_id": "mock-text-v1"},
            "image": {"provider": "mock", "model_id": "mock-image-v1"},
            "video": {"provider": "mock", "model_id": "mock-video-v1", "duration_seconds": 5},
        }
        db.commit()
        await service.sync_from_feishu(provisioned.project)

        shot = await service.process_record_status(project=provisioned.project, record=fake.records[0])
        db.refresh(shot)
        video_asset = db.query(Asset).filter(Asset.shot_id == shot.id, Asset.asset_type == AssetType.VIDEO.value).one()

        assert shot.status == ShotStatus.PENDING_ACCEPTANCE.value
        assert video_asset.feishu_drive_folder_token == "custom_folder_123"
        assert fake.uploaded_files[-1]["folder_token"] == "custom_folder_123"
        assert fake.records[0]["fields"]["视频链接"]["link"].startswith("https://feishu.test/file/")

    asyncio.run(run_flow())


def test_sync_from_feishu_uses_actual_row_order_when_shot_no_field_missing(monkeypatch):
    import asyncio

    async def run_flow():
        monkeypatch.setattr(settings, "default_text_provider", "mock")
        monkeypatch.setattr(settings, "default_image_provider", "mock")
        monkeypatch.setattr(settings, "default_video_provider", "mock")
        db = make_db()
        fake = FakeFeishuClient()
        fake.records = [
            {
                "record_id": "rec_001",
                "fields": {
                    "场景描述": "第一镜",
                    "生成批次": "batch_001",
                    "审核状态": "草稿",
                    "首帧同步设置": "否",
                    "关键帧生成设置": "否",
                    "图片生成状态": "未开始",
                    "生成状态": "未开始",
                    "重新生成状态": "未开始",
                    "Prompt 版本": 1,
                },
            },
            {
                "record_id": "rec_002",
                "fields": {
                    "场景描述": "第二镜",
                    "生成批次": "batch_001",
                    "审核状态": "草稿",
                    "首帧同步设置": "否",
                    "关键帧生成设置": "否",
                    "图片生成状态": "未开始",
                    "生成状态": "未开始",
                    "重新生成状态": "未开始",
                    "Prompt 版本": 1,
                },
            },
        ]
        service = FeishuStoryboardService(db, feishu=fake)
        provisioned = await service.create_project_from_bot(project_name="按行数同步镜号项目", chat_id="oc_test")
        await service.sync_from_feishu(provisioned.project)

        shots = ProjectService(db).list_shots(provisioned.project.id)
        assert [shot.shot_no for shot in shots[:2]] == ["001", "002"]

        fake.records = [fake.records[1], fake.records[0]]
        await service.sync_from_feishu(provisioned.project)

        reordered = ProjectService(db).list_shots(provisioned.project.id)
        assert [(shot.feishu_record_id, shot.shot_no) for shot in reordered[:2]] == [
            ("rec_002", "001"),
            ("rec_001", "002"),
        ]

    asyncio.run(run_flow())


def test_ensure_table_fields_deletes_legacy_shot_no_columns(monkeypatch):
    import asyncio

    async def run_flow():
        monkeypatch.setattr(settings, "default_text_provider", "mock")
        monkeypatch.setattr(settings, "default_image_provider", "mock")
        monkeypatch.setattr(settings, "default_video_provider", "mock")
        db = make_db()
        fake = FakeFeishuClient()
        fake.fields.extend(
            [
                {"field_name": "镜号", "field_id": "legacy_1"},
                {"field_name": "镜号", "field_id": "legacy_2"},
            ]
        )
        service = FeishuStoryboardService(db, feishu=fake)
        provisioned = await service.create_project_from_bot(project_name="清理旧镜号字段项目", chat_id="oc_test")

        await service.ensure_table_fields(provisioned.project)

        assert fake.deleted_fields == ["legacy_1", "legacy_2"]
        assert all(field["field_name"] != "镜号" for field in fake.fields)

    asyncio.run(run_flow())


def test_ensure_table_fields_skips_undeletable_primary_shot_no_column(monkeypatch):
    import asyncio

    async def run_flow():
        monkeypatch.setattr(settings, "default_text_provider", "mock")
        monkeypatch.setattr(settings, "default_image_provider", "mock")
        monkeypatch.setattr(settings, "default_video_provider", "mock")
        db = make_db()
        fake = FakeFeishuClient()
        fake.fields.append({"field_name": "镜号", "field_id": "primary_legacy"})

        async def delete_field(app_token: str, table_id: str, field_id: str) -> dict:
            if field_id == "primary_legacy":
                raise FeishuApiError("Feishu API error: code=1254046, msg=The Primary Field cannot be deleted.", code=1254046)
            return await FakeFeishuClient.delete_field(fake, app_token, table_id, field_id)

        fake.delete_field = delete_field
        service = FeishuStoryboardService(db, feishu=fake)
        provisioned = await service.create_project_from_bot(project_name="主字段镜号兼容项目", chat_id="oc_test")

        created = await service.ensure_table_fields(provisioned.project)

        assert "关键帧时间点" in created or any(field["field_name"] == "关键帧时间点" for field in fake.fields)
        assert any(field["field_name"] == "镜号" for field in fake.fields)

    asyncio.run(run_flow())


def test_upload_drive_asset_falls_back_to_default_workspace_when_project_folder_is_missing(monkeypatch, tmp_path):
    import asyncio

    async def run_flow():
        monkeypatch.setattr(settings, "default_text_provider", "mock")
        monkeypatch.setattr(settings, "default_image_provider", "mock")
        monkeypatch.setattr(settings, "default_video_provider", "mock")
        monkeypatch.setattr(settings, "feishu_workspace_parent_url", "https://feishu.test/drive/folder/parent_folder")
        monkeypatch.setattr(settings, "feishu_workspace_folder_name", "AI生成")
        db = make_db()
        fake = FakeFeishuClient()

        async def upload_file(folder_token: str, name: str, content: bytes) -> dict:
            token = f"file_{len(fake.uploaded_files) + 1}"
            fake.uploaded_files.append({"folder_token": folder_token, "name": name, "content": content})
            if folder_token == "missing_frames":
                raise FeishuApiError("Feishu HTTP error: 400, msg=parent node not exist.")
            return {"code": 0, "data": {"file_token": token}}

        fake.upload_file = upload_file
        service = FeishuStoryboardService(db, feishu=fake)
        provisioned = await service.create_project_from_bot(project_name="目录回退项目", chat_id="oc_test")
        await service.sync_from_feishu(provisioned.project)
        shot = ProjectService(db).list_shots(provisioned.project.id)[0]
        frame_path = tmp_path / "frame.png"
        frame_path.write_bytes(b"frame")
        asset = Asset(
            project_id=provisioned.project.id,
            shot_id=shot.id,
            asset_type=AssetType.FIRST_FRAME.value,
            storage_uri=f"file://{frame_path}",
            public_url="https://storage.test/frame.png",
            provider="mock",
            model_id="mock-image-v1",
        )
        db.add(asset)
        db.commit()

        token = await service._upload_drive_asset(asset, "missing_frames")

        assert token == "file_2"
        assert asset.feishu_drive_folder_token == "new_root"
        assert [item["folder_token"] for item in fake.uploaded_files] == ["missing_frames", "new_root"]

    asyncio.run(run_flow())
