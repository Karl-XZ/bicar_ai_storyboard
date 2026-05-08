import base64

import httpx

from app.core.config import settings
from app.providers.base import ImageGenerationResult, ImageProvider


class OpenAIImageProvider(ImageProvider):
    async def generate_image(self, payload: dict) -> ImageGenerationResult:
        if not settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is required")
        prompt = payload.get("prompt") or payload.get("final_prompt") or ""
        size = str(payload.get("size") or "1024*1024").replace("*", "x")
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                f"{settings.openai_base_url.rstrip('/')}/v1/images/generations",
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                json={
                    "model": payload.get("model") or settings.openai_image_model,
                    "prompt": prompt,
                    "size": size,
                },
            )
        response.raise_for_status()
        item = response.json()["data"][0]
        image_b64 = item.get("b64_json")
        if not image_b64:
            raise RuntimeError("OpenAI image response did not include b64_json")
        return ImageGenerationResult(
            bytes_data=base64.b64decode(image_b64),
            mime_type="image/png",
            provider_asset_id=item.get("id"),
        )
