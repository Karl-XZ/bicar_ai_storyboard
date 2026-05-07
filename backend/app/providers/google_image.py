import base64

import httpx

from app.core.config import settings
from app.providers.base import ImageGenerationResult, ImageProvider


class GoogleNanoBanana2Provider(ImageProvider):
    async def generate_image(self, payload: dict) -> ImageGenerationResult:
        if not settings.google_api_key:
            raise RuntimeError("GOOGLE_API_KEY is required")
        prompt = payload.get("prompt") or payload.get("final_prompt") or ""
        url = (
            f"{settings.google_base_url.rstrip('/')}/v1beta/models/"
            f"{settings.nano_banana_model}:generateContent?key={settings.google_api_key}"
        )
        body = {"contents": [{"parts": [{"text": prompt}]}]}
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(url, json=body)
        response.raise_for_status()
        data = response.json()
        for candidate in data.get("candidates", []):
            for part in candidate.get("content", {}).get("parts", []):
                inline = part.get("inlineData") or part.get("inline_data")
                if inline and inline.get("data"):
                    return ImageGenerationResult(
                        bytes_data=base64.b64decode(inline["data"]),
                        mime_type=inline.get("mimeType") or inline.get("mime_type") or "image/png",
                    )
        raise RuntimeError("Google image response did not include inline image data")

