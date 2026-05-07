import base64
from uuid import uuid4

from app.providers.base import ImageGenerationResult, ImageProvider, PromptOptimizationResult, TextProvider, VideoProvider, VideoTaskResult

PNG_1X1_AMBER = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)


class MockTextProvider(TextProvider):
    async def optimize_prompt(self, payload: dict) -> PromptOptimizationResult:
        description = payload.get("scene_description") or "未命名镜头"
        style = payload.get("project_style") or "cinematic realistic"
        return PromptOptimizationResult(
            keyframe_prompt=f"{description}，核心画面，{style}，电影级构图，细节稳定",
            first_frame_prompt=f"{description}，镜头起始帧，建立空间关系，光线自然，{style}",
            last_frame_prompt=f"{description}，镜头结束帧，动作和情绪收束，保持角色一致，{style}",
            video_prompt=f"连续镜头：{description}。保持首尾帧人物、服装、光线和空间一致，运动自然，{style}",
            negative_prompt="flicker, deformation, extra limbs, text artifacts, unstable face, inconsistent costume",
            camera_motion="slow push in",
            consistency_notes="保持角色服装、光线方向、空间布局和镜头连续性一致。",
            style_tags=["cinematic", "realistic"],
        )


class MockImageProvider(ImageProvider):
    async def generate_image(self, payload: dict) -> ImageGenerationResult:
        return ImageGenerationResult(
            bytes_data=PNG_1X1_AMBER,
            mime_type="image/png",
            provider_asset_id=f"mock_image_{uuid4().hex}",
        )


class MockVideoProvider(VideoProvider):
    async def create_video_task(self, payload: dict) -> VideoTaskResult:
        return VideoTaskResult(provider_task_id=f"mock_video_task_{uuid4().hex}")

    async def poll_video_task(self, provider_task_id: str) -> dict:
        return {
            "status": "succeeded",
            "provider_task_id": provider_task_id,
            "video_bytes": b"mock mp4 placeholder",
            "mime_type": "video/mp4",
        }
