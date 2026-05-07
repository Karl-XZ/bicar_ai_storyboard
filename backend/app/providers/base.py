from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class PromptOptimizationResult:
    keyframe_prompt: str
    first_frame_prompt: str
    last_frame_prompt: str
    video_prompt: str
    negative_prompt: str
    camera_motion: str
    consistency_notes: str
    style_tags: list[str]


@dataclass(frozen=True)
class ImageGenerationResult:
    bytes_data: bytes
    mime_type: str
    provider_asset_id: str | None = None


@dataclass(frozen=True)
class VideoTaskResult:
    provider_task_id: str


class TextProvider(ABC):
    @abstractmethod
    async def optimize_prompt(self, payload: dict) -> PromptOptimizationResult:
        raise NotImplementedError


class ImageProvider(ABC):
    @abstractmethod
    async def generate_image(self, payload: dict) -> ImageGenerationResult:
        raise NotImplementedError


class VideoProvider(ABC):
    @abstractmethod
    async def create_video_task(self, payload: dict) -> VideoTaskResult:
        raise NotImplementedError

    @abstractmethod
    async def poll_video_task(self, provider_task_id: str) -> dict:
        raise NotImplementedError

