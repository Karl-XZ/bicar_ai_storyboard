from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.model_aliases import IMAGE_MODEL_NANOBANANA
from app.domain.enums import Satisfaction


def _default_text_model() -> str:
    return {
        "dashscope": settings.dashscope_text_model,
        "openai": settings.openai_text_model,
        "deepseek": settings.deepseek_text_model,
        "openrouter": settings.openrouter_text_model,
    }.get(settings.default_text_provider, settings.dashscope_text_model)


def _default_image_model() -> str:
    return {
        "dashscope": settings.dashscope_image_model,
        "openai": settings.openai_image_model,
        "nano_banana_2": IMAGE_MODEL_NANOBANANA,
        "openrouter": IMAGE_MODEL_NANOBANANA,
    }.get(settings.default_image_provider, settings.dashscope_image_model)


def _default_video_model() -> str:
    return {
        "dashscope": settings.dashscope_video_model,
        "seedance_2_0": settings.seedance_model_id or settings.dashscope_video_model,
        "xyq_nest": settings.xyq_video_model,
    }.get(settings.default_video_provider, settings.dashscope_video_model)


class ApiResponse(BaseModel):
    success: bool = True
    request_id: str
    message: str
    data: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    success: bool = False
    request_id: str
    error_code: str
    message: str


class ShotCreate(BaseModel):
    shot_no: str
    scene_description: str
    batch_no: str = "batch_001"
    keyframe_prompt: str = ""
    first_frame_prompt: str = ""
    last_frame_prompt: str = ""
    video_prompt: str = ""
    negative_prompt: str = ""


class CreateProjectRequest(BaseModel):
    name: str
    aspect_ratio: str = "16:9"
    duration_seconds: int = 5
    default_text_provider: str = Field(default_factory=lambda: settings.default_text_provider)
    default_text_model: str = Field(default_factory=_default_text_model)
    default_image_provider: str = Field(default_factory=lambda: settings.default_image_provider)
    default_image_model: str = Field(default_factory=_default_image_model)
    default_video_provider: str = Field(default_factory=lambda: settings.default_video_provider)
    default_video_model: str = Field(default_factory=_default_video_model)
    transition_alignment_enabled: bool = False
    keyframe_generation_enabled: bool = False
    initial_shots: list[ShotCreate] = Field(default_factory=list)


class GenerateBatchRequest(BaseModel):
    batch_no: str


class RejectShotRequest(BaseModel):
    reason: str = Field(min_length=1)


class ArchiveShotRequest(BaseModel):
    satisfaction: Satisfaction


class JobCreated(BaseModel):
    job_id: UUID
