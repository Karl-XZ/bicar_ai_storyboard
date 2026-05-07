from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///./local_storage/feishu_e2e_acceptance.db")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.adapters.feishu import FeishuClient  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.db.session import SessionLocal, engine  # noqa: E402
from app.domain.enums import Satisfaction  # noqa: E402
from app.models import Base  # noqa: E402
from app.services.feishu_storyboard import FeishuStoryboardService  # noqa: E402


def emit(step: str, **data) -> None:
    safe = {"step": step, **data}
    print(json.dumps(safe, ensure_ascii=False), flush=True)


async def main() -> int:
    Base.metadata.create_all(bind=engine)
    project_name = f"验收_咖啡广告_1镜头_{datetime.now().strftime('%m%d_%H%M%S')}"
    db = SessionLocal()
    service = FeishuStoryboardService(db)
    feishu = FeishuClient()
    try:
        emit("start", project_name=project_name, root_folder_token=settings.feishu_root_folder_token)
        provisioned = await service.create_project_from_bot(project_name=project_name, chat_id=settings.feishu_default_chat_id)
        project = provisioned.project
        emit(
            "project_created",
            project_id=str(project.id),
            table_url=provisioned.table_url,
            folder_url=provisioned.folder_url,
        )

        create_response = await feishu.batch_create_records(
            project.feishu_app_token,
            project.feishu_table_id,
            [
                {
                    "fields": {
                        "镜号": "001",
                        "场景描述": "清晨厨房，一杯热咖啡放在木桌上，阳光从窗户照进来，画面温暖真实，有生活方式广告质感",
                        "生成批次": "batch_001",
                        "审核状态": "待优化",
                        "Prompt 版本": 1,
                    }
                }
            ],
        )
        created_items = (create_response.get("data") or {}).get("records") or []
        record_id = created_items[0]["record_id"] if created_items else ""
        emit("record_created", record_id=record_id)

        optimized = await service.optimize_current_batch(project=project, batch_no="batch_001")
        emit("prompt_optimized", shots=len(optimized), status=optimized[0].status if optimized else None)

        generated = await service.generate_current_batch(project=project, batch_no="batch_001")
        emit("frames_generated", shots=len(generated), status=generated[0].status if generated else None)

        records = await _records(feishu, project.feishu_app_token, project.feishu_table_id)
        record = _find_record(records, record_id)
        emit(
            "record_after_frames",
            record_id=record.get("record_id"),
            has_first_frame=bool((record.get("fields") or {}).get("首帧图")),
            keyframe_count=len((record.get("fields") or {}).get("关键帧图") or []),
            has_selected_keyframe=bool((record.get("fields") or {}).get("选中关键帧图")),
            status=_field_text((record.get("fields") or {}).get("审核状态")),
        )

        fields = record.get("fields") or {}
        fields["审核状态"] = "通过"
        await feishu.batch_update_records(project.feishu_app_token, project.feishu_table_id, [{"record_id": record_id, "fields": {"审核状态": "通过"}}])
        approved_record = {"record_id": record_id, "fields": fields}
        shot = await service.process_record_status(project=project, record=approved_record)
        emit("video_generated", shot_id=str(shot.id), status=shot.status)

        records = await _records(feishu, project.feishu_app_token, project.feishu_table_id)
        record = _find_record(records, record_id)
        fields = record.get("fields") or {}
        fields["满意度"] = "满意"
        await feishu.batch_update_records(project.feishu_app_token, project.feishu_table_id, [{"record_id": record_id, "fields": {"满意度": "满意"}}])
        accepted_record = {"record_id": record_id, "fields": fields}
        shot = await service.process_record_status(project=project, record=accepted_record)
        emit("archived", shot_id=str(shot.id), status=shot.status, satisfaction=Satisfaction.SATISFIED.value)

        records = await _records(feishu, project.feishu_app_token, project.feishu_table_id)
        record = _find_record(records, record_id)
        final_fields = record.get("fields") or {}
        emit(
            "done",
            project_id=str(project.id),
            table_url=provisioned.table_url,
            folder_url=provisioned.folder_url,
            final_status=_field_text(final_fields.get("审核状态")),
            has_video=bool(final_fields.get("视频链接")),
            has_archive=bool(final_fields.get("归档链接")),
        )
        return 0
    except Exception as exc:  # noqa: BLE001
        emit("failed", error=type(exc).__name__, message=str(exc))
        return 1
    finally:
        db.close()


async def _records(feishu: FeishuClient, app_token: str, table_id: str) -> list[dict]:
    response = await feishu.search_records(app_token, table_id, {})
    return (response.get("data") or {}).get("items") or []


def _find_record(records: list[dict], record_id: str) -> dict:
    for record in records:
        if record.get("record_id") == record_id:
            return record
    raise RuntimeError(f"record not found: {record_id}")


def _field_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("text") or value.get("name") or "")
    if isinstance(value, list):
        return "".join(_field_text(item) for item in value)
    return str(value)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
