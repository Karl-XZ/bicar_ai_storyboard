import json

import httpx

from app.core.config import settings
from app.providers.base import PromptOptimizationResult, TextProvider


class OpenAIProviderError(RuntimeError):
    pass


class OpenAITextProvider(TextProvider):
    async def optimize_prompt(self, payload: dict) -> PromptOptimizationResult:
        if not settings.openai_api_key:
            raise OpenAIProviderError("OPENAI_API_KEY is required")
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "keyframe_prompt": {"type": "string"},
                "first_frame_prompt": {"type": "string"},
                "last_frame_prompt": {"type": "string"},
                "video_prompt": {"type": "string"},
                "negative_prompt": {"type": "string"},
                "camera_motion": {"type": "string"},
                "consistency_notes": {"type": "string"},
                "style_tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": [
                "keyframe_prompt",
                "first_frame_prompt",
                "last_frame_prompt",
                "video_prompt",
                "negative_prompt",
                "camera_motion",
                "consistency_notes",
                "style_tags",
            ],
        }
        body = {
            "model": payload.get("model") or settings.openai_text_model,
            "input": [
                {
                    "role": "system",
                    "content": "你是影视分镜 Prompt 优化器。只输出符合 schema 的 JSON。",
                },
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False),
                },
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "storyboard_prompt",
                    "schema": schema,
                    "strict": True,
                }
            },
        }
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                f"{settings.openai_base_url.rstrip('/')}/v1/responses",
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                json=body,
            )
        response.raise_for_status()
        data = response.json()
        text = data.get("output_text")
        if not text:
            for item in data.get("output", []):
                for content in item.get("content", []):
                    if content.get("type") in {"output_text", "text"}:
                        text = content.get("text")
                        break
        if not text:
            raise OpenAIProviderError("OpenAI response did not include output_text")
        parsed = json.loads(text)
        return PromptOptimizationResult(**parsed)
