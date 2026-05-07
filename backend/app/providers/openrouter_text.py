import json
import re

import httpx

from app.core.config import settings
from app.providers.base import PromptOptimizationResult, TextProvider


class OpenRouterTextProvider(TextProvider):
    async def optimize_prompt(self, payload: dict) -> PromptOptimizationResult:
        if not settings.openrouter_api_key:
            raise RuntimeError("OPENROUTER_API_KEY is required")
        body = {
            "model": payload.get("model") or settings.openrouter_text_model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是影视广告分镜 Prompt 优化器。只输出一个 JSON 对象，不要输出 Markdown。"
                        "JSON 字段必须包含 keyframe_prompt、first_frame_prompt、last_frame_prompt、"
                        "video_prompt、negative_prompt、camera_motion、consistency_notes、style_tags。"
                    ),
                },
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            "temperature": 0.4,
            "response_format": {"type": "json_object"},
        }
        async with httpx.AsyncClient(timeout=90) as client:
            response = await client.post(
                f"{settings.openrouter_base_url.rstrip('/')}/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.openrouter_api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
        response.raise_for_status()
        data = response.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        parsed = _parse_json_object(content)
        return PromptOptimizationResult(
            keyframe_prompt=str(parsed["keyframe_prompt"]),
            first_frame_prompt=str(parsed["first_frame_prompt"]),
            last_frame_prompt=str(parsed["last_frame_prompt"]),
            video_prompt=str(parsed["video_prompt"]),
            negative_prompt=str(parsed["negative_prompt"]),
            camera_motion=str(parsed["camera_motion"]),
            consistency_notes=str(parsed["consistency_notes"]),
            style_tags=list(parsed.get("style_tags") or []),
        )


def _parse_json_object(text: str) -> dict:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?", "", stripped, flags=re.IGNORECASE).strip()
        stripped = re.sub(r"```$", "", stripped).strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", stripped, flags=re.DOTALL)
        if match:
            return json.loads(match.group(0))
        raise
