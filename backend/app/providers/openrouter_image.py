import base64
import mimetypes
from pathlib import Path

import httpx

from app.core.config import settings
from app.core.model_aliases import resolve_openrouter_image_model
from app.providers.base import ImageGenerationResult, ImageProvider


class OpenRouterImageProvider(ImageProvider):
    async def generate_image(self, payload: dict) -> ImageGenerationResult:
        if not settings.openrouter_api_key:
            raise RuntimeError("OPENROUTER_API_KEY is required")
        prompt = payload.get("prompt") or payload.get("final_prompt") or ""
        model = _resolve_model(payload.get("model") or settings.openrouter_image_model)
        body = {
            "model": model,
            "messages": [{"role": "user", "content": await _build_message_content(prompt=prompt, payload=payload)}],
            "modalities": ["image", "text"],
            "stream": False,
        }
        aspect_ratio = _aspect_ratio_from_size(payload.get("size"))
        if aspect_ratio:
            body["image_config"] = {"aspect_ratio": aspect_ratio}
        async with httpx.AsyncClient(timeout=180) as client:
            response = await client.post(
                f"{settings.openrouter_base_url.rstrip('/')}/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.openrouter_api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
        response.raise_for_status()
        message = response.json().get("choices", [{}])[0].get("message", {})
        images = message.get("images") or []
        for item in images:
            image_payload = item.get("image_url") or item.get("imageUrl") or {}
            url = image_payload.get("url")
            if url and url.startswith("data:"):
                mime_type, encoded = url.split(",", 1)
                detected = mime_type.split(";")[0].replace("data:", "") or "image/png"
                return ImageGenerationResult(
                    bytes_data=base64.b64decode(encoded),
                    mime_type=detected,
                )
        raise RuntimeError("OpenRouter image response did not include inline image data")


async def _build_message_content(*, prompt: str, payload: dict) -> list[dict]:
    content = [{"type": "text", "text": prompt}]
    for source in payload.get("reference_images") or payload.get("reference_image_urls") or []:
        content.append({"type": "image_url", "image_url": {"url": await _to_image_url(str(source))}})
    return content


async def _to_image_url(source: str) -> str:
    if source.startswith(("http://", "https://")):
        return source
    if source.startswith("file://"):
        path = Path(source.replace("file://", ""))
        if not path.exists():
            raise RuntimeError(f"reference image does not exist: {path}")
        mime_type = mimetypes.guess_type(path.name)[0] or "image/png"
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:{mime_type};base64,{encoded}"
    return source


def _resolve_model(model: str) -> str:
    return resolve_openrouter_image_model(
        model,
        default_openrouter_image_model=settings.openrouter_image_model,
        default_nano_banana_model=settings.openrouter_nano_banana_model,
    )


def _aspect_ratio_from_size(size: str | None) -> str | None:
    mapping = {
        "1024*1024": "1:1",
        "1280*720": "16:9",
        "720*1280": "9:16",
    }
    if not size:
        return None
    return mapping.get(size.replace("x", "*"))
