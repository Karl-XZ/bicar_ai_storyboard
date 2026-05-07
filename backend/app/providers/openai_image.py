import base64

import httpx

from app.core.config import settings
from app.providers.base import ImageGenerationResult, ImageProvider


class OpenAIImageProvider(ImageProvider):
    async def generate_image(self, payload: dict) -> ImageGenerationResult:
        if not settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is required")
        prompt = payload.get("prompt") or payload.get("final_prompt") or ""
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                f"{settings.openai_base_url.rstrip('/')}/v1/images/generations",
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                json={
                    "model": payload.get("model") or settings.openai_image_model,
                    "prompt": prompt,
                    "size": "1024x1024",
                    "response_format": "b64_json",
                },
            )
        response.raise_for_status()
        item = response.json()["data"][0]
        return ImageGenerationResult(
            bytes_data=base64.b64decode(item["b64_json"]),
            mime_type="image/png",
            provider_asset_id=item.get("id"),
        )
