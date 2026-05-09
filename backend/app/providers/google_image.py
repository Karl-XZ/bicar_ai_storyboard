import base64
import mimetypes
from pathlib import Path

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
        body = {"contents": [{"parts": await _build_parts(prompt=prompt, payload=payload)}]}
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


async def _build_parts(*, prompt: str, payload: dict) -> list[dict]:
    parts = [{"text": prompt}]
    for source in payload.get("reference_images") or payload.get("reference_image_urls") or []:
        mime_type, content = await _load_image_source(str(source))
        parts.append({"inlineData": {"mimeType": mime_type, "data": base64.b64encode(content).decode("ascii")}})
    return parts


async def _load_image_source(source: str) -> tuple[str, bytes]:
    if source.startswith("file://"):
        path = Path(source.replace("file://", ""))
        if not path.exists():
            raise RuntimeError(f"reference image does not exist: {path}")
        mime_type = mimetypes.guess_type(path.name)[0] or "image/png"
        return mime_type, path.read_bytes()
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        response = await client.get(source)
    response.raise_for_status()
    mime_type = response.headers.get("content-type", "image/png").split(";")[0]
    return mime_type, response.content
