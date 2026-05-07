import httpx

from app.core.config import settings
from app.providers.base import VideoProvider, VideoTaskResult


class Seedance20VideoProvider(VideoProvider):
    async def create_video_task(self, payload: dict) -> VideoTaskResult:
        if not settings.seedance_api_key or not settings.seedance_base_url:
            raise RuntimeError("SEEDANCE_API_KEY and SEEDANCE_BASE_URL are required")
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                f"{settings.seedance_base_url.rstrip('/')}/videos/generations",
                headers={"Authorization": f"Bearer {settings.seedance_api_key}"},
                json={"model": payload.get("model") or settings.seedance_model_id, **payload},
            )
        response.raise_for_status()
        data = response.json()
        provider_task_id = data.get("id") or data.get("task_id") or data.get("provider_task_id")
        if not provider_task_id:
            raise RuntimeError("Seedance response did not include task id")
        return VideoTaskResult(provider_task_id=provider_task_id)

    async def poll_video_task(self, provider_task_id: str) -> dict:
        if not settings.seedance_api_key or not settings.seedance_base_url:
            raise RuntimeError("SEEDANCE_API_KEY and SEEDANCE_BASE_URL are required")
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.get(
                f"{settings.seedance_base_url.rstrip('/')}/videos/generations/{provider_task_id}",
                headers={"Authorization": f"Bearer {settings.seedance_api_key}"},
            )
        response.raise_for_status()
        return response.json()
