from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.model_aliases import normalize_image_model, normalize_video_model
from app.domain.enums import ShotStatus
from app.models.shot import Shot

STATUS_FROM_FEISHU = {
    "草稿": ShotStatus.DRAFT.value,
    "待优化": ShotStatus.PENDING_PROMPT.value,
    "优化中": ShotStatus.PROMPT_OPTIMIZING.value,
    "待生成帧": ShotStatus.PENDING_FRAMES.value,
    "帧生成中": ShotStatus.FRAMES_GENERATING.value,
    "待审核": ShotStatus.PENDING_REVIEW.value,
    "通过": ShotStatus.APPROVED.value,
    "驳回": ShotStatus.REJECTED.value,
    "视频生成中": ShotStatus.VIDEO_GENERATING.value,
    "待验收": ShotStatus.PENDING_ACCEPTANCE.value,
    "已归档-满意": ShotStatus.ARCHIVED_SATISFIED.value,
    "已归档-不满意": ShotStatus.ARCHIVED_UNSATISFIED.value,
}

STATUS_TO_FEISHU = {value: key for key, value in STATUS_FROM_FEISHU.items()}
GENERATION_STATUS_NOT_STARTED = "未开始"
GENERATION_STATUS_STARTED = "启动"
GENERATION_STATUS_GENERATING = "正在生成"
GENERATION_STATUS_DONE = "生成完成"


class ShotService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def upsert_from_feishu_record(self, *, project_id: UUID, record: dict, fallback_shot_no: str | None = None) -> Shot | None:
        record_id = record.get("record_id")
        fields = record.get("fields") or {}
        description = self._plain(fields.get("场景描述"))
        if not record_id or not description:
            return None
        shot = self.db.scalar(select(Shot).where(Shot.project_id == project_id, Shot.feishu_record_id == record_id))
        explicit_shot_no = self._plain(fields.get("镜号"))
        fallback_value = str(fallback_shot_no or "").strip()
        shot_no = explicit_shot_no or fallback_value or (shot.shot_no if shot else "")
        if not shot_no:
            return None
        prompts = {
            "keyframe_prompt": self._plain(fields.get("关键帧提示词")),
            "first_frame_prompt": self._plain(fields.get("首帧提示词")),
            "last_frame_prompt": self._plain(fields.get("尾帧提示词")),
            "video_prompt": self._plain(fields.get("视频 Prompt")),
            "negative_prompt": self._plain(fields.get("负面 Prompt")),
            "camera_motion": self._plain(fields.get("镜头运动")),
            "consistency_notes": self._plain(fields.get("一致性说明")),
            "reference_image_notes": self._plain(fields.get("参考图批注")),
            "text_model": self._single_select(fields.get("文本模型")),
            "image_model": normalize_image_model(self._single_select(fields.get("图片模型"))),
            "video_model": normalize_video_model(self._single_select(fields.get("视频模型"))),
            "selected_keyframe_tokens": self._attachment_tokens(fields.get("选中关键帧图")),
            "selected_keyframe_urls": self._attachment_sources(fields.get("选中关键帧图")),
            "keyframe_time_seconds": self._number(fields.get("关键帧时间点")),
            "duration_seconds": self._number(fields.get("视频时长")),
            "reference_tokens": self._attachment_tokens(fields.get("参考图")),
            "reference_image_urls": self._attachment_sources(fields.get("参考图")),
            "transition_alignment": self._single_select(fields.get("首帧同步设置")) or "否",
            "keyframe_generation": self._single_select(fields.get("关键帧生成设置")) or "否",
            "regeneration_options": self._multi_select(fields.get("需要重新生成的选项")),
            "regeneration_status": self._single_select(fields.get("重新生成状态")) or GENERATION_STATUS_NOT_STARTED,
            "video_storage_url": self._url(fields.get("视频存储位置")),
        }
        status_text = self._single_select(fields.get("审核状态")) or "草稿"
        image_generation_status = self._single_select(fields.get("图片生成状态")) or GENERATION_STATUS_NOT_STARTED
        generation_status = self._single_select(fields.get("生成状态")) or GENERATION_STATUS_NOT_STARTED
        prompts["review_status"] = status_text
        prompts["image_generation_status"] = image_generation_status
        prompts["generation_status"] = generation_status
        prompt_version = int(self._number(fields.get("Prompt 版本")) or 1)
        rejection_reason = self._plain(fields.get("驳回原因"))
        if not shot:
            shot = Shot(
                project_id=project_id,
                feishu_record_id=record_id,
                shot_no=shot_no,
                batch_no=self._plain(fields.get("生成批次")) or "batch_001",
                scene_description=description,
                prompts=prompts,
                status=STATUS_FROM_FEISHU.get(status_text, ShotStatus.DRAFT.value),
                prompt_version=prompt_version,
                error_code="USER_REJECTED" if status_text == "驳回" and rejection_reason else None,
                error_message=rejection_reason or None,
            )
            self.db.add(shot)
        else:
            incoming_is_stale = prompt_version < shot.prompt_version
            shot.shot_no = shot_no
            shot.batch_no = self._plain(fields.get("生成批次")) or shot.batch_no
            shot.scene_description = description
            if not incoming_is_stale:
                shot.prompts = self._merge_prompts(shot.prompts or {}, prompts)
                shot.status = STATUS_FROM_FEISHU.get(status_text, shot.status)
            shot.prompt_version = max(shot.prompt_version, prompt_version)
            if not incoming_is_stale and status_text == "驳回" and rejection_reason:
                shot.error_code = "USER_REJECTED"
                shot.error_message = rejection_reason
        self.db.flush()
        return shot

    def _plain(self, value) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, list):
            return "".join(self._plain(item) for item in value).strip()
        if isinstance(value, dict):
            return str(value.get("text") or value.get("name") or value.get("link") or "").strip()
        return str(value).strip()

    def _url(self, value) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, dict):
            return str(value.get("link") or value.get("url") or value.get("text") or value.get("name") or "").strip()
        if isinstance(value, list):
            return "".join(self._url(item) for item in value).strip()
        return str(value).strip()

    def _single_select(self, value) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            return value.get("text") or value.get("name") or ""
        return ""

    def _multi_select(self, value) -> list[str]:
        if isinstance(value, str):
            return [value] if value else []
        if not isinstance(value, list):
            return []
        values = []
        for item in value:
            if isinstance(item, str):
                values.append(item)
            elif isinstance(item, dict):
                selected = item.get("text") or item.get("name")
                if selected:
                    values.append(str(selected))
        return values

    def _number(self, value) -> float | None:
        if value in (None, ""):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, dict):
            return self._number(value.get("value") or value.get("text") or value.get("name"))
        try:
            return float(str(value).strip())
        except ValueError:
            return None

    def _attachment_tokens(self, value) -> list[str]:
        if not isinstance(value, list):
            return []
        tokens = []
        for item in value:
            if isinstance(item, dict):
                token = item.get("file_token") or item.get("token") or item.get("tmp_url")
                if token:
                    tokens.append(str(token))
        return tokens

    def _attachment_sources(self, value) -> list[str]:
        if not isinstance(value, list):
            return []
        urls: list[str] = []
        for item in value:
            if isinstance(item, dict):
                token = item.get("file_token") or item.get("token")
                if token:
                    urls.append(f"feishu://{token}")
                    continue
                url = item.get("url") or item.get("tmp_url") or item.get("link")
                if url:
                    urls.append(str(url))
        return urls

    def _merge_prompts(self, current: dict, incoming: dict) -> dict:
        merged = dict(current)
        force_sync_keys = {
            "selected_keyframe_tokens",
            "selected_keyframe_urls",
            "keyframe_time_seconds",
            "duration_seconds",
            "reference_tokens",
            "reference_image_urls",
            "transition_alignment",
            "keyframe_generation",
            "regeneration_options",
            "regeneration_status",
            "video_storage_url",
            "review_status",
            "image_generation_status",
            "generation_status",
            "text_model",
            "image_model",
            "video_model",
        }
        for key, value in incoming.items():
            if not value and key not in force_sync_keys:
                continue
            if key in {"image_generation_status", "generation_status", "regeneration_status"}:
                value = self._prefer_status(current.get(key), value)
            merged[key] = value
        return merged

    def _prefer_status(self, current: str | None, incoming: str | None) -> str | None:
        order = {
            GENERATION_STATUS_NOT_STARTED: 0,
            GENERATION_STATUS_STARTED: 1,
            GENERATION_STATUS_GENERATING: 2,
            GENERATION_STATUS_DONE: 3,
        }
        if not incoming:
            return current
        if not current:
            return incoming
        return current if order.get(str(current), -1) > order.get(str(incoming), -1) else incoming
