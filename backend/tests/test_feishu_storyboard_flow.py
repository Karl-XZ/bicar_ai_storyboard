from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.domain.enums import ShotStatus
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
                    "镜号": "001",
                    "场景描述": "清晨厨房，一杯咖啡在木桌上",
                "生成批次": "batch_001",
                "审核状态": "待优化",
                "图片生成状态": "未开始",
                "生成状态": "未开始",
                "Prompt 版本": 1,
            },
        }
        ]
        self.sent_cards = []
        self.updated_records = []
        self.uploaded_files = []

    async def send_card(self, receive_id: str, card: dict, receive_id_type: str = "chat_id") -> dict:
        self.sent_cards.append({"receive_id": receive_id, "card": card, "receive_id_type": receive_id_type})
        return {"code": 0, "data": {"message_id": "msg_001"}}

    async def create_folder(self, parent_token: str, name: str) -> dict:
        return {"code": 0, "data": {"token": f"fld_{len(name)}_{len(parent_token)}", "url": f"https://feishu.test/{name}"}}

    async def create_bitable_app(self, name: str, folder_token: str = "") -> dict:
        return {"code": 0, "data": {"app": {"app_token": "app_001", "url": "https://feishu.test/base/app_001"}}}

    async def create_table(self, app_token: str, table_name: str, fields: list[dict]) -> dict:
        assert any(field["field_name"] == "镜头运动" for field in fields)
        return {"code": 0, "data": {"table": {"table_id": "tbl_001"}}}

    async def search_records(self, app_token: str, table_id: str, payload: dict | None = None) -> dict:
        return {"code": 0, "data": {"items": self.records}}

    async def list_fields(self, app_token: str, table_id: str) -> dict:
        names = [
            "镜号",
            "场景描述",
            "生成批次",
            "审核状态",
            "图片生成状态",
            "生成状态",
            "Prompt 版本",
        ]
        return {"code": 0, "data": {"items": [{"field_name": name, "field_id": f"fld_{index}"} for index, name in enumerate(names)]}}

    async def create_field(self, app_token: str, table_id: str, field: dict) -> dict:
        return {"code": 0, "data": {"field": field}}

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
        assert fake.records[1]["fields"]["镜号"] == "001"
        assert fake.records[1]["fields"]["生成批次"] == "batch_001"
        assert fake.records[1]["fields"]["审核状态"] == "草稿"
        assert fake.records[1]["fields"]["图片生成状态"] == "未开始"
        assert fake.records[1]["fields"]["生成状态"] == "未开始"

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

        fake.records[0]["fields"]["生成状态"] = "启动"
        shot = await service.process_record_status(project=provisioned.project, record=fake.records[0])
        assert shot.status == ShotStatus.PENDING_ACCEPTANCE.value
        assert fake.records[0]["fields"]["审核状态"] == "通过"
        assert fake.records[0]["fields"]["生成状态"] == "生成完成"
        assert fake.records[0]["fields"]["视频链接"]["link"].startswith("https://feishu.test/drive/file/")

        fake.records[0]["fields"]["满意度"] = "满意"
        shot = await service.process_record_status(project=provisioned.project, record=fake.records[0])
        assert shot.status == ShotStatus.ARCHIVED_SATISFIED.value
        assert fake.records[0]["fields"]["审核状态"] == "通过"
        assert fake.records[0]["fields"]["归档链接"]["link"].startswith("https://feishu.test/drive/file/")

        project = ProjectService(db).get_project(provisioned.project.id)
        stats = service.progress_stats(project)
        assert stats["archived"] == 1

    asyncio.run(run_flow())
