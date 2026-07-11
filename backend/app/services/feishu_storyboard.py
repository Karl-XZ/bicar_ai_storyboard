from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from app.adapters.feishu import FeishuApiError, FeishuClient
from app.adapters.feishu_cards import batch_done_card, progress_card, project_created_card
from app.adapters.feishu_fields import bitable_field_definitions, build_field_map
from app.core.config import settings
from app.core.model_aliases import IMAGE_MODEL_NANOBANANA, VIDEO_MODEL_XYQ, normalize_image_model, normalize_video_model, video_provider_display
from app.domain.enums import AssetType, Satisfaction, ShotStatus
from app.domain.schemas import CreateProjectRequest
from app.models.asset import Asset
from app.models.project import Project
from app.models.shot import Shot
from app.services.assets import AssetService
from app.services.feishu_workspace import FeishuWorkspaceService
from app.services.projects import ProjectService
from app.services.shots import (
    GENERATION_STATUS_DONE,
    GENERATION_STATUS_GENERATING,
    GENERATION_STATUS_NOT_STARTED,
    GENERATION_STATUS_STARTED,
    STATUS_TO_FEISHU,
    ShotService,
)
from app.services.workflow import WorkflowError, WorkflowService

REGENERATE_OPTION_TO_PROMPT = {
    "关键帧提示词": "keyframe_prompt",
    "首帧提示词": "first_frame_prompt",
    "尾帧提示词": "last_frame_prompt",
    "视频提示词": "video_prompt",
}
REGENERATE_IMAGE_OPTIONS = {"关键帧重新生成", "首帧重新生成", "尾帧重新生成"}
DEPRECATED_TABLE_FIELDS = {"镜号"}


@dataclass(frozen=True)
class ProvisionedProject:
    project: Project
    table_url: str | None
    folder_url: str | None


class FeishuStoryboardService:
    def __init__(self, db: Session, feishu: FeishuClient | None = None) -> None:
        self.db = db
        self.feishu = feishu or FeishuClient()
        self.projects = ProjectService(db)
        self.shots = ShotService(db)

    async def create_project_from_bot(
        self,
        *,
        project_name: str,
        chat_id: str | None = None,
        parent_folder_url: str | None = None,
    ) -> ProvisionedProject:
        project = self.projects.create_project(CreateProjectRequest(name=project_name))
        resources = await self._provision_feishu_resources(project, parent_folder_url=parent_folder_url)
        project = self.projects.update_feishu_resources(
            project,
            app_token=resources["app_token"],
            table_id=resources["table_id"],
            folder_token=resources["project_folder_token"],
            workflow_config={
                "table_url": resources.get("table_url"),
                "folder_url": resources.get("folder_url"),
                "folders": resources["folders"],
                "chat_id": chat_id,
            },
        )
        target_chat = self._notification_chat_id(project=project, explicit_chat_id=chat_id)
        if target_chat:
            await self.feishu.send_card(
                target_chat,
                project_created_card(
                    project_id=str(project.id),
                    project_name=project.name,
                    table_url=resources.get("table_url"),
                    folder_url=resources.get("folder_url"),
                    transition_alignment_state="已启动" if (project.workflow_config or {}).get("transition_alignment_enabled") else "未启动",
                    keyframe_generation_state="已启动" if (project.workflow_config or {}).get("keyframe_generation_enabled") else "未启动",
                ),
            )
        return ProvisionedProject(project=project, table_url=resources.get("table_url"), folder_url=resources.get("folder_url"))

    async def sync_from_feishu(self, project: Project) -> list[Shot]:
        if not project.feishu_app_token or not project.feishu_table_id:
            return []
        await self.ensure_table_fields(project)
        response = await self.feishu.search_records(project.feishu_app_token, project.feishu_table_id, {})
        records = response.get("data", {}).get("items", [])
        if not records:
            await self.ensure_starter_records(project)
            return []
        await self._apply_record_defaults(project, records)
        synced: list[Shot] = []
        for index, record in enumerate(records, start=1):
            shot = self.shots.upsert_from_feishu_record(project_id=project.id, record=record, fallback_shot_no=f"{index:03d}")
            if shot:
                synced.append(shot)
        self.db.commit()
        return synced

    async def optimize_current_batch(self, *, project: Project, batch_no: str = "batch_001") -> list[Shot]:
        await self.sync_from_feishu(project)
        workflow = WorkflowService(self.db)
        shots = self.projects.list_shots(project.id, batch_no=batch_no)
        for shot in shots:
            await workflow.optimize_prompt_async(shot.id)
            self.db.refresh(shot)
            self._set_prompt_value(shot, "review_status", "待生成帧")
            await self.backfill_shots(project, [shot])
        return shots

    async def generate_current_batch(self, *, project: Project, batch_no: str = "batch_001") -> list[Shot]:
        await self.sync_from_feishu(project)
        shots = self.projects.list_shots(project.id, batch_no=batch_no)
        generated = await self._generate_images_for_shots(project, shots)
        target_chat = self._notification_chat_id(project=project)
        if target_chat:
            await self.feishu.send_card(
                target_chat,
                batch_done_card(
                    project_id=str(project.id),
                    batch_no=batch_no,
                    rows=[(shot.shot_no, "成功") for shot in generated],
                ),
            )
        return generated

    async def generate_all_images(self, project: Project) -> list[Shot]:
        await self.sync_from_feishu(project)
        shots = self.projects.list_shots(project.id)
        return await self._generate_images_for_shots(project, shots)

    async def generate_all_videos(self, project: Project, *, only_started: bool = False) -> list[Shot]:
        await self.sync_from_feishu(project)
        shots = self.projects.list_shots(project.id)
        generated: list[Shot] = []
        for shot in shots:
            generation_status = (shot.prompts or {}).get("generation_status") or GENERATION_STATUS_NOT_STARTED
            if only_started and generation_status != GENERATION_STATUS_STARTED:
                continue
            if not only_started and generation_status == GENERATION_STATUS_DONE:
                continue
            if not self._video_ready(shot):
                continue
            if await self.generate_shot_video(project, shot, force=only_started):
                generated.append(shot)
        return generated

    async def generate_all_images_and_videos(self, project: Project) -> dict[str, int]:
        images = await self.generate_all_images(project)
        videos = await self.generate_all_videos(project)
        return {"images": len(images), "videos": len(videos)}

    async def set_keyframe_generation(self, project: Project, enabled: bool) -> None:
        project.workflow_config = {**(project.workflow_config or {}), "keyframe_generation_enabled": enabled}
        self.db.commit()
        await self.sync_from_feishu(project)
        shots = self.projects.list_shots(project.id)
        for shot in shots:
            self._set_prompt_value(shot, "keyframe_generation", "是" if enabled else "否")
        self.db.commit()
        if shots:
            await self.backfill_shots(project, shots)

    async def set_transition_alignment(self, project: Project, enabled: bool) -> int:
        config = {**(project.workflow_config or {}), "transition_alignment_enabled": enabled}
        project.workflow_config = config
        self.db.commit()
        await self.sync_from_feishu(project)
        shots = self.projects.list_shots(project.id)
        for shot in shots:
            self._set_prompt_value(shot, "transition_alignment", "是" if enabled else "否")
        self.db.commit()
        if shots:
            await self.backfill_shots(project, shots)
        if enabled:
            return await self.sync_tail_to_next_first_frame(project)
        return 0

    async def sync_tail_to_next_first_frame(self, project: Project) -> int:
        await self.sync_from_feishu(project)
        shots = self.projects.list_shots(project.id)
        synced: list[Shot] = []
        for previous, current in zip(shots, shots[1:]):
            if await self._sync_tail_to_first_frame(project, previous, current):
                synced.append(current)
        self.db.commit()
        if synced:
            await self.backfill_shots(project, synced)
        return len(synced)

    async def _sync_tail_to_first_frame(self, project: Project, previous: Shot, current: Shot) -> bool:
        asset_service = AssetService(self.db)
        tail = asset_service.latest_for_shot(previous.id, AssetType.LAST_FRAME)
        if not tail:
            return False
        latest_first = asset_service.latest_for_shot(current.id, AssetType.FIRST_FRAME)
        if (
            latest_first
            and latest_first.provider == "transition_alignment"
            and latest_first.prompt_hash == tail.prompt_hash
            and (current.prompts or {}).get("transition_source_shot_no") == previous.shot_no
        ):
            return True
        path = Path(tail.storage_uri.replace("file://", ""))
        if not path.exists():
            return False
        asset_service.put_bytes(
            project_id=current.project_id,
            shot_id=current.id,
            asset_type=AssetType.FIRST_FRAME,
            content=path.read_bytes(),
            filename=f"frames/{current.shot_no}/v{current.prompt_version:03d}_first_frame_synced_from_{previous.shot_no}.png",
            provider="transition_alignment",
            model_id=tail.model_id,
            prompt_hash=tail.prompt_hash,
            version=current.prompt_version,
        )
        self._set_prompt_value(current, "transition_source_shot_no", previous.shot_no)
        self.db.flush()
        return True

    async def generate_shot_video(self, project: Project, shot: Shot, *, force: bool = False) -> bool:
        workflow = WorkflowService(self.db)
        if not force and (shot.prompts or {}).get("generation_status") == GENERATION_STATUS_DONE:
            if AssetService(self.db).latest_for_shot(shot.id, AssetType.VIDEO):
                await self.backfill_shots(project, [shot])
                return False
        if force:
            self._set_prompt_value(shot, "video_run_id", int((shot.prompts or {}).get("video_run_id") or 0) + 1)
        self._set_prompt_value(shot, "generation_status", GENERATION_STATUS_GENERATING)
        shot.status = ShotStatus.VIDEO_GENERATING.value
        shot.error_code = None
        shot.error_message = None
        self.db.commit()
        await self.backfill_shots(project, [shot])
        try:
            await workflow.generate_video_async(shot.id, force=force)
            self.db.refresh(shot)
            self._set_prompt_value(shot, "generation_status", GENERATION_STATUS_DONE)
            shot.error_code = None
            shot.error_message = None
            self.db.commit()
            await self.backfill_shots(project, [shot])
            return True
        except WorkflowError as exc:
            self._set_prompt_value(shot, "generation_status", GENERATION_STATUS_NOT_STARTED)
            shot.error_code = exc.code
            shot.error_message = self._merge_error_message(shot.error_message, exc.message)
            self.db.commit()
            await self.backfill_shots(project, [shot])
            return False
        except Exception as exc:
            self._set_prompt_value(shot, "generation_status", GENERATION_STATUS_NOT_STARTED)
            shot.error_code = type(exc).__name__
            shot.error_message = str(exc)
            self.db.commit()
            await self.backfill_shots(project, [shot])
            return False

    async def _generate_images_for_shots(
        self,
        project: Project,
        shots: list[Shot],
        *,
        force: bool = False,
        only_started: bool = False,
        include_first_frame: bool | None = None,
        include_last_frame: bool | None = None,
        include_keyframes: bool | None = None,
    ) -> list[Shot]:
        workflow = WorkflowService(self.db)
        generated: list[Shot] = []
        ordered_shots = self.projects.list_shots(project.id)
        previous_by_id = {current.id: previous for previous, current in zip(ordered_shots, ordered_shots[1:])}
        for index, shot in enumerate(shots):
            image_status = (shot.prompts or {}).get("image_generation_status") or GENERATION_STATUS_NOT_STARTED
            if only_started and image_status != GENERATION_STATUS_STARTED:
                continue
            if not force and image_status == GENERATION_STATUS_DONE:
                continue
            if force:
                self._set_prompt_value(shot, "image_run_id", int((shot.prompts or {}).get("image_run_id") or 0) + 1)
            self._set_prompt_value(shot, "image_generation_status", GENERATION_STATUS_GENERATING)
            shot.status = ShotStatus.FRAMES_GENERATING.value
            shot.error_code = None
            shot.error_message = None
            self.db.commit()
            await self.backfill_shots(project, [shot])
            try:
                if not shot.prompts or not any(value for key, value in (shot.prompts or {}).items() if key.endswith("_prompt")):
                    await workflow.optimize_prompt_async(shot.id)
                    self.db.refresh(shot)
                    self._set_prompt_value(shot, "review_status", "待生成帧")
                    await self.backfill_shots(project, [shot])
                include_first = True if include_first_frame is None else include_first_frame
                include_last = True if include_last_frame is None else include_last_frame
                previous = previous_by_id.get(shot.id)
                if include_first and self._transition_sync_enabled(shot) and previous:
                    synced = await self._sync_tail_to_first_frame(project, previous, shot)
                    if not synced:
                        raise WorkflowError(
                            "TRANSITION_SOURCE_MISSING",
                            f"镜头 {shot.shot_no} 已开启首帧同步，但上一镜 {previous.shot_no} 还没有可用尾帧。请先生成上一镜尾帧后再重试。",
                        )
                    include_first = False
                keyframes_enabled = (
                    self._keyframe_generation_enabled(shot, project)
                    if include_keyframes is None
                    else include_keyframes
                )
                await workflow.generate_frames_async(
                    shot.id,
                    include_first_frame=include_first,
                    include_last_frame=include_last,
                    include_keyframes=keyframes_enabled,
                    force=force,
                )
                self.db.refresh(shot)
                self._set_prompt_value(shot, "review_status", "待审核")
                self._set_prompt_value(shot, "image_generation_status", GENERATION_STATUS_DONE)
                shot.error_code = None
                shot.error_message = None
                generated.append(shot)
                self.db.commit()
                await self.backfill_shots(project, [shot])
            except WorkflowError as exc:
                self._set_prompt_value(shot, "image_generation_status", GENERATION_STATUS_NOT_STARTED)
                shot.status = ShotStatus.PENDING_FRAMES.value
                shot.error_code = exc.code
                shot.error_message = self._merge_error_message(shot.error_message, exc.message)
                self.db.commit()
                await self.backfill_shots(project, [shot])
            except Exception as exc:
                self._set_prompt_value(shot, "image_generation_status", GENERATION_STATUS_NOT_STARTED)
                shot.status = ShotStatus.PENDING_FRAMES.value
                shot.error_code = type(exc).__name__
                shot.error_message = str(exc)
                self.db.commit()
                await self.backfill_shots(project, [shot])
        return generated

    async def backfill_shots(self, project: Project, shots: list[Shot]) -> None:
        if not project.feishu_app_token or not project.feishu_table_id:
            return
        asset_service = AssetService(self.db)
        folders = (project.workflow_config or {}).get("folders", {})
        records = []
        for shot in shots:
            fields = {
                "关键帧提示词": (shot.prompts or {}).get("keyframe_prompt", ""),
                "首帧提示词": (shot.prompts or {}).get("first_frame_prompt", ""),
                "尾帧提示词": (shot.prompts or {}).get("last_frame_prompt", ""),
                "视频 Prompt": (shot.prompts or {}).get("video_prompt", ""),
                "负面 Prompt": (shot.prompts or {}).get("negative_prompt", ""),
                "镜头运动": (shot.prompts or {}).get("camera_motion", ""),
                "一致性说明": (shot.prompts or {}).get("consistency_notes", ""),
                "文本模型": (shot.prompts or {}).get("text_model") or self._project_model(project, "text"),
                "图片模型": (shot.prompts or {}).get("image_model") or self._project_model(project, "image"),
                "视频模型": (shot.prompts or {}).get("video_model") or self._project_model(project, "video"),
                "视频时长": (shot.prompts or {}).get("duration_seconds") or self._default_video_duration(project),
                "审核状态": self._review_status_for_shot(shot),
                "首帧同步设置": self._transition_alignment_for_shot(shot),
                "关键帧生成设置": self._keyframe_generation_for_shot(shot, project),
                "图片生成状态": self._image_generation_status_for_shot(shot),
                "生成状态": self._generation_status_for_shot(shot),
                "重新生成状态": self._regeneration_status_for_shot(shot),
                "Prompt 版本": shot.prompt_version,
                "错误信息": shot.error_message or "",
                "驳回原因": (
                    shot.error_message or ""
                    if shot.error_code == "USER_REJECTED" and self._review_status_for_shot(shot) == "驳回"
                    else ""
                ),
            }
            keyframe_time = (shot.prompts or {}).get("keyframe_time_seconds")
            if keyframe_time is not None:
                fields["关键帧时间点"] = keyframe_time
            await self._attach_assets(project, shot, fields, asset_service, folders)
            if shot.feishu_record_id:
                records.append({"record_id": shot.feishu_record_id, "fields": fields})
        if records:
            await self.feishu.batch_update_records(project.feishu_app_token, project.feishu_table_id, records)
        await self._archive_unselected_default_videos(project, asset_service, folders)
        self.db.commit()

    async def send_progress(self, project: Project, chat_id: str | None = None) -> dict:
        await self.sync_from_feishu(project)
        stats = self.progress_stats(project)
        table_url = self._project_table_url(project)
        folder_url = self._project_folder_url(project)
        card = progress_card(
            project_name=project.name,
            stats=stats,
            table_url=table_url,
            folder_url=folder_url,
            project_id=str(project.id),
        )
        target = self._notification_chat_id(project=project, explicit_chat_id=chat_id)
        if target:
            await self.feishu.send_card(target, card)
        return stats

    def _notification_chat_id(self, *, project: Project | None = None, explicit_chat_id: str | None = None) -> str | None:
        resolved = str(explicit_chat_id or "").strip()
        if resolved:
            return resolved
        config = (project.workflow_config or {}) if project else {}
        project_chat = str(config.get("chat_id") or "").strip()
        if project_chat:
            return project_chat
        return None

    async def ensure_starter_records(self, project: Project, count: int = 3) -> int:
        if not project.feishu_app_token or not project.feishu_table_id:
            return 0
        await self.ensure_table_fields(project)
        response = await self.feishu.search_records(project.feishu_app_token, project.feishu_table_id, {})
        records = response.get("data", {}).get("items", [])
        if records:
            await self._apply_record_defaults(project, records)
            return 0
        used_shot_numbers = set()
        starter_records = []
        next_index = 1
        while len(records) + len(starter_records) < count:
            while f"{next_index:03d}" in used_shot_numbers:
                next_index += 1
            shot_no = f"{next_index:03d}"
            used_shot_numbers.add(shot_no)
            starter_records.append({"fields": self._default_record_fields(project, shot_no=shot_no)})
        if not starter_records:
            return 0
        await self.feishu.batch_create_records(project.feishu_app_token, project.feishu_table_id, starter_records)
        return len(starter_records)

    async def ensure_table_fields(self, project: Project) -> list[str]:
        if not project.feishu_app_token or not project.feishu_table_id:
            return []
        fields_response = await self.feishu.list_fields(project.feishu_app_token, project.feishu_table_id)
        await self._delete_deprecated_fields(project, fields_response)
        fields_response = await self.feishu.list_fields(project.feishu_app_token, project.feishu_table_id)
        field_map = build_field_map(fields_response)
        created: list[str] = []
        for definition in bitable_field_definitions():
            name = definition.get("field_name")
            if name and name not in field_map:
                await self.feishu.create_field(project.feishu_app_token, project.feishu_table_id, definition)
                created.append(name)
        return created

    async def _delete_deprecated_fields(self, project: Project, fields_response: dict) -> None:
        items = (fields_response.get("data") or {}).get("items") or []
        for item in items:
            field_name = item.get("field_name")
            field_id = item.get("field_id")
            if field_name in DEPRECATED_TABLE_FIELDS and field_id:
                try:
                    await self.feishu.delete_field(project.feishu_app_token, project.feishu_table_id, str(field_id))
                except FeishuApiError as exc:
                    if exc.code == 1254046 or "Primary Field cannot be deleted" in str(exc):
                        continue
                    raise

    def progress_stats(self, project: Project) -> dict:
        shots = self.projects.list_shots(project.id)
        asset_service = AssetService(self.db)
        prompt_optimized = sum(1 for shot in shots if (shot.prompts or {}).get("video_prompt"))
        first_frames = sum(1 for shot in shots if asset_service.latest_for_shot(shot.id, AssetType.FIRST_FRAME))
        last_frames = sum(1 for shot in shots if asset_service.latest_for_shot(shot.id, AssetType.LAST_FRAME))
        keyframe_shots = sum(1 for shot in shots if asset_service.list_for_shot(shot.id, AssetType.KEYFRAME))
        videos = sum(1 for shot in shots if asset_service.latest_for_shot(shot.id, AssetType.VIDEO))
        image_generating = sum(
            1
            for shot in shots
            if (shot.prompts or {}).get("image_generation_status") in {GENERATION_STATUS_STARTED, GENERATION_STATUS_GENERATING}
        )
        image_done = sum(
            1
            for shot in shots
            if (shot.prompts or {}).get("image_generation_status") == GENERATION_STATUS_DONE
            or asset_service.latest_for_shot(shot.id, AssetType.FIRST_FRAME)
            or asset_service.latest_for_shot(shot.id, AssetType.LAST_FRAME)
            or asset_service.list_for_shot(shot.id, AssetType.KEYFRAME)
        )
        video_generating = sum(
            1
            for shot in shots
            if (shot.prompts or {}).get("generation_status") in {GENERATION_STATUS_STARTED, GENERATION_STATUS_GENERATING}
        )
        video_done = sum(
            1
            for shot in shots
            if (shot.prompts or {}).get("generation_status") == GENERATION_STATUS_DONE
            or asset_service.latest_for_shot(shot.id, AssetType.VIDEO)
        )
        error_items = [
            {"shot_no": shot.shot_no, "code": shot.error_code, "message": shot.error_message}
            for shot in shots
            if shot.error_code or shot.error_message
        ]
        transition_state = self._transition_alignment_state(shots)
        return {
            "total": len(shots),
            "prompt_optimized": prompt_optimized,
            "prompt_pending": max(len(shots) - prompt_optimized, 0),
            "first_frames": first_frames,
            "last_frames": last_frames,
            "keyframe_shots": keyframe_shots,
            "videos": videos,
            "image_generating": image_generating,
            "image_done": image_done,
            "video_done": video_done,
            "errors": len(error_items),
            "error_items": error_items[:5],
            "log_paths": self._log_paths(),
            "transition_alignment_enabled": transition_state == "已启动",
            "transition_alignment_state": transition_state,
            "keyframe_generation_enabled": self._keyframe_generation_state(shots, project) == "已启动",
            "keyframe_generation_state": self._keyframe_generation_state(shots, project),
            "pending_frames": sum(1 for shot in shots if shot.status == ShotStatus.PENDING_FRAMES.value),
            "frames_generating": image_generating,
            "pending_review": sum(1 for shot in shots if shot.status == ShotStatus.PENDING_REVIEW.value),
            "video_generating": video_generating,
            "pending_acceptance": sum(1 for shot in shots if shot.status == ShotStatus.PENDING_ACCEPTANCE.value),
            "archived": sum(
                1
                for shot in shots
                if shot.status in {ShotStatus.ARCHIVED_SATISFIED.value, ShotStatus.ARCHIVED_UNSATISFIED.value}
            ),
        }

    async def process_record_status(self, *, project: Project, record: dict) -> Shot | None:
        shot = self.shots.upsert_from_feishu_record(project_id=project.id, record=record)
        if not shot:
            return None

        workflow = WorkflowService(self.db)
        should_backfill = False
        image_generation_status = self._field_text((record.get("fields") or {}).get("图片生成状态"))
        if image_generation_status == GENERATION_STATUS_STARTED:
            await self._generate_images_for_shots(project, [shot], force=True, only_started=True)
            self.db.refresh(shot)

        generation_status = self._field_text((record.get("fields") or {}).get("生成状态"))
        if generation_status == GENERATION_STATUS_STARTED:
            await self.generate_shot_video(project, shot, force=True)
            self.db.refresh(shot)

        regeneration_status = self._field_text((record.get("fields") or {}).get("重新生成状态"))
        if regeneration_status == GENERATION_STATUS_STARTED:
            await self.regenerate_shot_from_rejection(project, shot)
            self.db.refresh(shot)

        satisfaction_text = self._field_text((record.get("fields") or {}).get("满意度"))
        if satisfaction_text == "满意":
            workflow.archive_shot(shot.id, Satisfaction.SATISFIED)
            should_backfill = True
        elif satisfaction_text == "不满意":
            workflow.archive_shot(shot.id, Satisfaction.UNSATISFIED)
            should_backfill = True

        self.db.refresh(shot)
        if should_backfill:
            await self.backfill_shots(project, [shot])
        else:
            self.db.commit()
        return shot

    async def regenerate_shot_from_rejection(self, project: Project, shot: Shot) -> bool:
        workflow = WorkflowService(self.db)
        options = set((shot.prompts or {}).get("regeneration_options") or [])
        rejection_reason = shot.error_message or (shot.prompts or {}).get("rejection_reason") or ""
        if not options:
            self._set_prompt_value(shot, "regeneration_status", GENERATION_STATUS_NOT_STARTED)
            shot.error_code = "REGENERATION_OPTIONS_REQUIRED"
            shot.error_message = "请先在「需要重新生成的选项」里选择要重做的内容"
            self.db.commit()
            await self.backfill_shots(project, [shot])
            return False
        self._set_prompt_value(shot, "regeneration_status", GENERATION_STATUS_GENERATING)
        shot.error_code = None
        shot.error_message = None
        self.db.commit()
        await self.backfill_shots(project, [shot])
        try:
            prompt_keys = [key for option, key in REGENERATE_OPTION_TO_PROMPT.items() if option in options]
            if prompt_keys:
                await workflow.revise_prompts_async(shot.id, prompt_keys=prompt_keys, rejection_reason=rejection_reason)
                self.db.refresh(shot)

            image_options = options & REGENERATE_IMAGE_OPTIONS
            if image_options:
                await self._generate_images_for_shots(
                    project,
                    [shot],
                    force=True,
                    include_first_frame="首帧重新生成" in image_options,
                    include_last_frame="尾帧重新生成" in image_options,
                    include_keyframes="关键帧重新生成" in image_options,
                )
                self.db.refresh(shot)

            if "视频重新生成" in options:
                await self.generate_shot_video(project, shot, force=True)
                self.db.refresh(shot)

            self._set_prompt_value(shot, "review_status", "待审核")
            self._set_prompt_value(shot, "regeneration_status", GENERATION_STATUS_DONE)
            shot.status = (
                ShotStatus.PENDING_ACCEPTANCE.value
                if (shot.prompts or {}).get("generation_status") == GENERATION_STATUS_DONE
                else ShotStatus.PENDING_REVIEW.value
            )
            shot.error_code = None
            shot.error_message = None
            self.db.commit()
            await self.backfill_shots(project, [shot])
            return True
        except WorkflowError as exc:
            self._set_prompt_value(shot, "regeneration_status", GENERATION_STATUS_NOT_STARTED)
            shot.error_code = exc.code
            shot.error_message = self._merge_error_message(shot.error_message, exc.message)
            self.db.commit()
            await self.backfill_shots(project, [shot])
            return False
        except Exception as exc:
            self._set_prompt_value(shot, "regeneration_status", GENERATION_STATUS_NOT_STARTED)
            shot.error_code = type(exc).__name__
            shot.error_message = str(exc)
            self.db.commit()
            await self.backfill_shots(project, [shot])
            return False

    async def _provision_feishu_resources(self, project: Project, *, parent_folder_url: str | None = None) -> dict:
        workspace = FeishuWorkspaceService(feishu=self.feishu)
        parent_folder_token = self._folder_token_from_url(parent_folder_url)
        if not parent_folder_token:
            ensured_parent = await workspace.ensure_storyboard_workspace_folder()
            parent_folder_token = str(ensured_parent.get("folder_token") or settings.feishu_root_folder_token or "root")
        project_folder, resolved_parent = await workspace.create_folder_with_fallback(
            parent_token=parent_folder_token,
            name=project.name,
        )
        project_folder_token = self._extract_token(project_folder)
        folders = {}
        for key, name in {
            "references": "01_参考图",
            "frames": "02_帧图",
            "videos": "03_视频",
            "satisfied": "05_通过_满意",
            "unsatisfied": "06_需重做_不满意",
        }.items():
            folder = await self.feishu.create_folder(project_folder_token, name)
            folders[key] = self._extract_token(folder)
        if folders.get("videos"):
            archive_folder = await self.feishu.create_folder(folders["videos"], "ARCHIVED")
            folders["videos_archived"] = self._extract_token(archive_folder)
        bitable, resolved_bitable_folder = await workspace.create_bitable_with_fallback(
            name=f"{project.name}_分镜表",
            folder_token=project_folder_token,
        )
        if resolved_bitable_folder != project_folder_token:
            project_folder_token = resolved_bitable_folder
        app_token = self._extract_app_token(bitable)
        table = await self.feishu.create_table(app_token, "分镜表", bitable_field_definitions())
        table_id = self._extract_table_id(table)
        await self.feishu.subscribe_file_events(app_token, "bitable")
        await self.feishu.batch_create_records(
            app_token,
            table_id,
            [
                {"fields": self._default_record_fields(project, shot_no=f"{index:03d}")}
                for index in range(1, 4)
            ],
        )
        app_url = self._extract_url(bitable)
        return {
            "project_folder_token": project_folder_token,
            "folders": folders,
            "app_token": app_token,
            "table_id": table_id,
            "table_url": self._bitable_table_url(app_url, app_token, table_id),
            "folder_url": self._extract_url(project_folder) or self._drive_url(project_folder_token),
            "resolved_parent_folder_token": resolved_parent,
        }

    async def _attach_assets(
        self,
        project: Project,
        shot: Shot,
        fields: dict,
        asset_service: AssetService,
        folders: dict,
    ) -> None:
        mapping = {
            "首帧图": (AssetType.FIRST_FRAME, folders.get("frames")),
            "尾帧图": (AssetType.LAST_FRAME, folders.get("frames")),
            "关键帧图": (AssetType.KEYFRAME, folders.get("frames")),
            "视频链接": (AssetType.VIDEO, self._video_folder_for_shot(shot, folders)),
            "归档链接": (AssetType.ARCHIVE, self._archive_folder_for_status(shot, folders)),
        }
        for field_name, (asset_type, folder_token) in mapping.items():
            assets = asset_service.list_for_shot(shot.id, asset_type)
            if not assets:
                continue
            if asset_type in {AssetType.VIDEO, AssetType.ARCHIVE}:
                asset = assets[-1]
                link = asset.public_url
                if folder_token:
                    token = await self._upload_drive_asset(asset, folder_token)
                    if token:
                        link = self._drive_file_url(project, token)
                fields[field_name] = {"link": link, "text": Path(asset.storage_uri).name}
                continue
            display_assets = assets[-1:] if asset_type in {AssetType.FIRST_FRAME, AssetType.LAST_FRAME} else assets
            if not folder_token:
                fields[field_name] = "\n".join(asset.public_url or asset.storage_uri for asset in display_assets)
                continue
            file_tokens = []
            for asset in display_assets:
                await self._upload_drive_asset(asset, folder_token)
                token = await self._upload_bitable_asset(project, asset)
                if token:
                    file_tokens.append({"file_token": token})
            if file_tokens:
                fields[field_name] = file_tokens
                if asset_type == AssetType.KEYFRAME and "选中关键帧图" not in fields:
                    fields["选中关键帧图"] = [file_tokens[0]]

    async def _upload_drive_asset(self, asset: Asset, folder_token: str) -> str | None:
        if asset.feishu_drive_token and asset.feishu_drive_folder_token == folder_token:
            return asset.feishu_drive_token
        path = Path(asset.storage_uri.replace("file://", ""))
        if not path.exists():
            return None
        workspace = FeishuWorkspaceService(feishu=self.feishu)
        response, resolved_folder = await workspace.upload_file_with_fallback(
            target_folder=folder_token,
            name=path.name,
            content=path.read_bytes(),
        )
        token = self._extract_file_token(response)
        if token:
            asset.feishu_drive_token = token
            asset.feishu_drive_folder_token = resolved_folder
            self.db.flush()
        return token

    async def _archive_unselected_default_videos(
        self,
        project: Project,
        asset_service: AssetService,
        folders: dict,
    ) -> int:
        video_folder = str(folders.get("videos") or "").strip()
        if not video_folder:
            return 0
        archive_folder = await self._ensure_video_archive_folder(project, video_folder, folders)
        if not archive_folder:
            return 0

        keep_tokens: set[str] = set()
        for shot in self.projects.list_shots(project.id):
            latest = asset_service.latest_for_shot(shot.id, AssetType.VIDEO)
            if latest and latest.feishu_drive_token and latest.feishu_drive_folder_token == video_folder:
                keep_tokens.add(latest.feishu_drive_token)

        moved = 0
        page_token: str | None = None
        while True:
            response = await self.feishu.list_folder_items(video_folder, page_size=200, page_token=page_token)
            data = response.get("data", {})
            items = data.get("files") or data.get("items") or []
            for item in items:
                token = self._item_token(item)
                if not token or token in keep_tokens:
                    continue
                file_type = self._item_type(item)
                if file_type == "folder":
                    continue
                try:
                    await self.feishu.move_file(token, folder_token=archive_folder, file_type=file_type)
                    moved += 1
                except FeishuApiError:
                    continue
            page_token = data.get("next_page_token") or data.get("page_token")
            if not page_token or not data.get("has_more"):
                break
        return moved

    async def _ensure_video_archive_folder(self, project: Project, video_folder: str, folders: dict) -> str | None:
        existing = str(folders.get("videos_archived") or "").strip()
        if existing:
            return existing
        page_token: str | None = None
        while True:
            response = await self.feishu.list_folder_items(video_folder, page_size=200, page_token=page_token)
            data = response.get("data", {})
            items = data.get("files") or data.get("items") or []
            for item in items:
                if self._item_type(item) == "folder" and str(item.get("name") or "").strip().upper() == "ARCHIVED":
                    token = self._item_token(item)
                    if token:
                        self._remember_video_archive_folder(project, folders, token)
                        return token
            page_token = data.get("next_page_token") or data.get("page_token")
            if not page_token or not data.get("has_more"):
                break
        try:
            folder = await self.feishu.create_folder(video_folder, "ARCHIVED")
        except FeishuApiError:
            return None
        token = self._extract_token(folder)
        if token:
            self._remember_video_archive_folder(project, folders, token)
        return token or None

    def _remember_video_archive_folder(self, project: Project, folders: dict, token: str) -> None:
        folders["videos_archived"] = token
        project.workflow_config = {
            **(project.workflow_config or {}),
            "folders": {**((project.workflow_config or {}).get("folders") or {}), "videos_archived": token},
        }
        self.db.flush()

    def _item_token(self, item: dict) -> str:
        return str(item.get("token") or item.get("file_token") or item.get("node_token") or "")

    def _item_type(self, item: dict) -> str:
        raw = str(item.get("type") or item.get("mime_type") or item.get("file_type") or "").lower()
        if raw in {"folder", "explorer"}:
            return "folder"
        if raw in {"doc", "docx", "sheet", "mindnote", "bitable"}:
            return raw
        return "file"

    async def _upload_bitable_asset(self, project: Project, asset: Asset) -> str | None:
        if asset.feishu_file_token:
            return asset.feishu_file_token
        if not project.feishu_app_token:
            return None
        path = Path(asset.storage_uri.replace("file://", ""))
        if not path.exists():
            return None
        response = await self.feishu.upload_bitable_attachment(project.feishu_app_token, path.name, path.read_bytes())
        token = self._extract_file_token(response)
        if token:
            asset.feishu_file_token = token
            self.db.flush()
        return token

    def _archive_folder_for_status(self, shot: Shot, folders: dict) -> str | None:
        if shot.status == ShotStatus.ARCHIVED_UNSATISFIED.value:
            return folders.get("unsatisfied")
        return folders.get("satisfied")

    def _merge_error_message(self, current: str | None, incoming: str) -> str:
        current_text = (current or "").strip()
        incoming_text = (incoming or "").strip()
        if not current_text:
            return incoming_text
        if not incoming_text or incoming_text in current_text:
            return current_text
        return f"{current_text}\n{incoming_text}"

    async def _apply_record_defaults(self, project: Project, records: list[dict]) -> None:
        if not project.feishu_app_token or not project.feishu_table_id:
            return
        updates = []
        for record in records:
            fields = record.setdefault("fields", {})
            defaults = {}
            for key, value in self._default_record_fields(project).items():
                if not self._field_text(fields.get(key)):
                    defaults[key] = value
            if defaults and record.get("record_id"):
                fields.update(defaults)
                updates.append({"record_id": record["record_id"], "fields": defaults})
        if updates:
            await self.feishu.batch_update_records(project.feishu_app_token, project.feishu_table_id, updates)

    def _default_record_fields(self, project: Project, shot_no: str | None = None) -> dict:
        fields = {
            "生成批次": "batch_001",
            "审核状态": "草稿",
            "首帧同步设置": "否",
            "关键帧生成设置": "否",
            "图片生成状态": GENERATION_STATUS_NOT_STARTED,
            "生成状态": GENERATION_STATUS_NOT_STARTED,
            "重新生成状态": GENERATION_STATUS_NOT_STARTED,
            "Prompt 版本": 1,
            "文本模型": self._project_model(project, "text"),
            "图片模型": self._project_model(project, "image"),
            "视频模型": self._project_model(project, "video"),
            "视频时长": self._default_video_duration(project),
        }
        return fields

    def _review_status_for_shot(self, shot: Shot) -> str:
        review_status = (shot.prompts or {}).get("review_status")
        if review_status:
            return str(review_status)
        if shot.status in {
            ShotStatus.PENDING_REVIEW.value,
            ShotStatus.APPROVED.value,
            ShotStatus.VIDEO_GENERATING.value,
            ShotStatus.PENDING_ACCEPTANCE.value,
            ShotStatus.ARCHIVED_SATISFIED.value,
            ShotStatus.ARCHIVED_UNSATISFIED.value,
        }:
            return "待审核"
        return STATUS_TO_FEISHU.get(shot.status, "草稿")

    def _generation_status_for_shot(self, shot: Shot) -> str:
        generation_status = (shot.prompts or {}).get("generation_status")
        if generation_status in {GENERATION_STATUS_STARTED, GENERATION_STATUS_GENERATING, GENERATION_STATUS_DONE}:
            return str(generation_status)
        if shot.status == ShotStatus.VIDEO_GENERATING.value:
            return GENERATION_STATUS_GENERATING
        if shot.status in {
            ShotStatus.PENDING_ACCEPTANCE.value,
            ShotStatus.ARCHIVED_SATISFIED.value,
            ShotStatus.ARCHIVED_UNSATISFIED.value,
        } or AssetService(self.db).latest_for_shot(shot.id, AssetType.VIDEO):
            return GENERATION_STATUS_DONE
        if generation_status:
            return str(generation_status)
        return GENERATION_STATUS_NOT_STARTED

    def _image_generation_status_for_shot(self, shot: Shot) -> str:
        image_generation_status = (shot.prompts or {}).get("image_generation_status")
        if image_generation_status in {GENERATION_STATUS_STARTED, GENERATION_STATUS_GENERATING, GENERATION_STATUS_DONE}:
            return str(image_generation_status)
        if shot.status == ShotStatus.FRAMES_GENERATING.value:
            return GENERATION_STATUS_GENERATING
        asset_service = AssetService(self.db)
        if (
            shot.status == ShotStatus.PENDING_REVIEW.value
            or asset_service.latest_for_shot(shot.id, AssetType.FIRST_FRAME)
            or asset_service.latest_for_shot(shot.id, AssetType.LAST_FRAME)
            or asset_service.list_for_shot(shot.id, AssetType.KEYFRAME)
        ):
            return GENERATION_STATUS_DONE
        if image_generation_status:
            return str(image_generation_status)
        return GENERATION_STATUS_NOT_STARTED

    def _regeneration_status_for_shot(self, shot: Shot) -> str:
        return str((shot.prompts or {}).get("regeneration_status") or GENERATION_STATUS_NOT_STARTED)

    def _transition_alignment_for_shot(self, shot: Shot) -> str:
        value = (shot.prompts or {}).get("transition_alignment")
        return "是" if value == "是" else "否"

    def _transition_sync_enabled(self, shot: Shot) -> bool:
        return self._transition_alignment_for_shot(shot) == "是"

    def _keyframe_generation_for_shot(self, shot: Shot, project: Project | None = None) -> str:
        value = (shot.prompts or {}).get("keyframe_generation")
        if value in {"是", "否"}:
            return str(value)
        if project and (project.workflow_config or {}).get("keyframe_generation_enabled"):
            return "是"
        return "否"

    def _keyframe_generation_enabled(self, shot: Shot, project: Project | None = None) -> bool:
        return self._keyframe_generation_for_shot(shot, project) == "是"

    def _set_prompt_value(self, shot: Shot, key: str, value) -> None:
        shot.prompts = {**(shot.prompts or {}), key: value}
        self.db.flush()

    def _video_ready(self, shot: Shot) -> bool:
        return bool((shot.prompts or {}).get("video_prompt") or shot.scene_description)

    def _extract_token(self, response: dict) -> str:
        data = response.get("data", {})
        return data.get("token") or data.get("file_token") or data.get("folder_token") or data.get("node", {}).get("token") or ""

    def _extract_file_token(self, response: dict) -> str | None:
        data = response.get("data", {})
        return data.get("file_token") or data.get("file", {}).get("file_token")

    def _extract_app_token(self, response: dict) -> str:
        data = response.get("data", {})
        return data.get("app_token") or data.get("app", {}).get("app_token") or data.get("token") or ""

    def _extract_table_id(self, response: dict) -> str:
        data = response.get("data", {})
        return data.get("table_id") or data.get("table", {}).get("table_id") or ""

    def _extract_url(self, response: dict) -> str | None:
        data = response.get("data", {})
        return data.get("url") or data.get("app", {}).get("url")

    def _drive_url(self, token: str) -> str:
        domain = settings.feishu_base_url.replace("https://open.", "").replace("http://open.", "")
        return f"https://{domain}/drive/folder/{token}"

    def _drive_file_url(self, project: Project, token: str) -> str:
        return f"{self._project_site_url(project)}/file/{token}"

    def _project_site_url(self, project: Project) -> str:
        for key in ("table_url", "folder_url"):
            url = (project.workflow_config or {}).get(key)
            if not url:
                continue
            parsed = urlparse(str(url))
            if parsed.netloc:
                return f"{parsed.scheme or 'https'}://{parsed.netloc}"
        return self._feishu_site_url()

    def _project_table_url(self, project: Project) -> str | None:
        table_url = str((project.workflow_config or {}).get("table_url") or "").strip()
        if table_url:
            return table_url
        if project.feishu_app_token and project.feishu_table_id:
            return self._bitable_table_url(None, project.feishu_app_token, project.feishu_table_id)
        return None

    def _project_folder_url(self, project: Project) -> str | None:
        folder_url = str((project.workflow_config or {}).get("folder_url") or "").strip()
        if folder_url:
            return folder_url
        if project.feishu_folder_token:
            return self._drive_url(project.feishu_folder_token)
        return None

    def _bitable_table_url(self, app_url: str | None, app_token: str, table_id: str) -> str:
        base_url = app_url or f"{self._feishu_site_url()}/base/{app_token}"
        separator = "&" if "?" in base_url else "?"
        return f"{base_url}{separator}table={table_id}"

    def _feishu_site_url(self) -> str:
        domain = settings.feishu_base_url.replace("https://open.", "").replace("http://open.", "")
        return f"https://{domain}"

    def _field_text(self, value) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, dict):
            return str(value.get("text") or value.get("name") or value.get("link") or "").strip()
        if isinstance(value, list):
            return "".join(self._field_text(item) for item in value).strip()
        return str(value).strip()

    def _project_model(self, project: Project, kind: str) -> str:
        model = str(((project.model_config or {}).get(kind) or {}).get("model_id") or "")
        if kind == "image":
            return normalize_image_model(model)
        if kind == "video":
            return normalize_video_model(model)
        return model

    def _default_video_duration(self, project: Project) -> float:
        video_config = (project.model_config or {}).get("video") or {}
        duration = video_config.get("duration_seconds") or (project.workflow_config or {}).get("duration_seconds") or 5
        try:
            return float(duration)
        except (TypeError, ValueError):
            return 5.0

    def _transition_alignment_state(self, shots: list[Shot]) -> str:
        if not shots:
            return "未启动"
        values = {self._transition_alignment_for_shot(shot) for shot in shots}
        if values == {"是"}:
            return "已启动"
        if values == {"否"}:
            return "未启动"
        return "自定义"

    def _keyframe_generation_state(self, shots: list[Shot], project: Project) -> str:
        if not shots:
            return "未启动"
        values = {self._keyframe_generation_for_shot(shot, project) for shot in shots}
        if values == {"是"}:
            return "已启动"
        if values == {"否"}:
            return "未启动"
        return "自定义"

    def _video_folder_for_shot(self, shot: Shot, folders: dict) -> str | None:
        custom_url = (shot.prompts or {}).get("video_storage_url")
        token = self._folder_token_from_url(custom_url)
        return token or folders.get("videos")

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

    def _log_paths(self) -> list[str]:
        backend_root = Path(__file__).resolve().parents[2]
        return [
            str((backend_root / "backend-api.err").resolve()),
            str((backend_root / "feishu-ws.err").resolve()),
        ]
