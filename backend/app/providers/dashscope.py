from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import re
from pathlib import Path
from typing import Any

import httpx

from app.core.config import settings
from app.providers.base import ImageGenerationResult, ImageProvider, PromptOptimizationResult, TextProvider, VideoProvider, VideoTaskResult


class DashScopeProviderError(RuntimeError):
    pass


class DashScopeTextProvider(TextProvider):
    async def optimize_prompt(self, payload: dict) -> PromptOptimizationResult:
        api_key = _require_api_key()
        model = payload.get("model") or payload.get("model_id") or settings.dashscope_text_model
        body = {
            "model": model,
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
        }
        async with httpx.AsyncClient(timeout=90) as client:
            response = await client.post(
                f"{settings.dashscope_compatible_base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=body,
            )
        data = _decode_response(response)
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


class DashScopeImageProvider(ImageProvider):
    async def generate_image(self, payload: dict) -> ImageGenerationResult:
        api_key = _require_api_key()
        prompt = payload.get("prompt") or payload.get("final_prompt") or ""
        if not prompt:
            raise DashScopeProviderError("DashScope image prompt is required")
        input_payload = {"prompt": prompt}
        negative_prompt = payload.get("negative_prompt")
        if negative_prompt:
            input_payload["negative_prompt"] = negative_prompt

        task = await _post_async_task(
            path="/services/aigc/text2image/image-synthesis",
            api_key=api_key,
            body={
                "model": payload.get("model") or payload.get("model_id") or settings.dashscope_image_model,
                "input": input_payload,
                "parameters": {
                    "size": payload.get("size") or settings.dashscope_image_size,
                    "n": int(payload.get("n") or 1),
                },
            },
        )
        result = await _wait_for_task(api_key, task.provider_task_id)
        output = result.get("output", {})
        image_url = next((item.get("url") for item in output.get("results", []) if item.get("url")), "")
        if not image_url:
            raise DashScopeProviderError("DashScope image task succeeded without result url")
        async with httpx.AsyncClient(timeout=120) as client:
            image_response = await client.get(image_url)
        image_response.raise_for_status()
        return ImageGenerationResult(
            bytes_data=image_response.content,
            mime_type=image_response.headers.get("content-type", "image/png").split(";")[0],
            provider_asset_id=task.provider_task_id,
        )


class DashScopeVideoProvider(VideoProvider):
    async def create_video_task(self, payload: dict) -> VideoTaskResult:
        api_key = _require_api_key()
        model = payload.get("model") or payload.get("model_id") or settings.dashscope_video_model
        first_frame_url = payload.get("first_frame_url")
        last_frame_url = payload.get("last_frame_url")
        reference_image_url = payload.get("reference_image_url")
        input_payload = {
            "prompt": payload.get("prompt") or "",
            "negative_prompt": payload.get("negative_prompt") or "",
        }
        endpoint = "/services/aigc/video-generation/video-synthesis"
        if first_frame_url and last_frame_url:
            endpoint = "/services/aigc/image2video/video-synthesis"
            if "kf2v" not in model:
                model = settings.dashscope_video_model
            input_payload["first_frame_url"] = _image_input(first_frame_url)
            input_payload["last_frame_url"] = _image_input(last_frame_url)
        elif first_frame_url or last_frame_url or reference_image_url:
            endpoint = "/services/aigc/image2video/video-synthesis"
            if "i2v" not in model:
                model = settings.dashscope_i2v_model
            input_payload["img_url"] = _image_input(first_frame_url or last_frame_url or reference_image_url)
        elif "t2v" not in model:
            model = settings.dashscope_t2v_model

        body = {
            "model": model,
            "input": {key: value for key, value in input_payload.items() if value},
            "parameters": {
                "resolution": payload.get("resolution") or settings.dashscope_video_resolution,
                "prompt_extend": bool(payload.get("prompt_extend", settings.dashscope_prompt_extend)),
                "watermark": bool(payload.get("watermark", False)),
            },
        }
        duration = payload.get("duration_seconds")
        if duration and "wanx2.1-i2v-turbo" in model:
            body["parameters"]["duration"] = int(duration)
        return await _post_async_task(path=endpoint, api_key=api_key, body=body)

    async def poll_video_task(self, provider_task_id: str) -> dict:
        api_key = _require_api_key()
        result = await _wait_for_task(api_key, provider_task_id)
        video_url = (result.get("output") or {}).get("video_url")
        if not video_url:
            raise DashScopeProviderError("DashScope video task succeeded without video_url")
        async with httpx.AsyncClient(timeout=180) as client:
            video_response = await client.get(video_url)
        video_response.raise_for_status()
        return {
            "status": "succeeded",
            "provider_task_id": provider_task_id,
            "video_bytes": video_response.content,
            "mime_type": video_response.headers.get("content-type", "video/mp4").split(";")[0],
            "provider_response": result,
        }


async def _post_async_task(*, path: str, api_key: str, body: dict) -> VideoTaskResult:
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            f"{settings.dashscope_base_url.rstrip('/')}{path}",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "X-DashScope-Async": "enable",
            },
            json=body,
        )
    data = _decode_response(response)
    output = data.get("output") or {}
    task_id = output.get("task_id")
    if not task_id:
        raise DashScopeProviderError("DashScope response did not include task_id")
    return VideoTaskResult(provider_task_id=task_id)


async def _wait_for_task(api_key: str, task_id: str) -> dict:
    attempts = max(int(settings.video_max_polling_attempts), 1)
    interval = max(int(settings.video_polling_interval_seconds), 1)
    async with httpx.AsyncClient(timeout=60) as client:
        for attempt in range(attempts):
            response = await client.get(
                f"{settings.dashscope_base_url.rstrip('/')}/tasks/{task_id}",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            data = _decode_response(response)
            output = data.get("output") or {}
            status = output.get("task_status")
            if status == "SUCCEEDED":
                return data
            if status in {"FAILED", "CANCELED", "UNKNOWN"}:
                raise DashScopeProviderError(
                    f"DashScope task failed: status={status}, code={output.get('code')}, message={output.get('message')}"
                )
            if attempt < attempts - 1:
                await asyncio.sleep(interval)
    raise DashScopeProviderError(f"DashScope task timed out: {task_id}")


def _decode_response(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError as exc:
        raise DashScopeProviderError(f"DashScope returned non-JSON response: {response.text[:300]}") from exc
    if response.status_code >= 400 or data.get("code"):
        raise DashScopeProviderError(
            f"DashScope API error: status={response.status_code}, code={data.get('code')}, message={data.get('message')}"
        )
    return data


def _parse_json_object(text: str) -> dict[str, Any]:
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


def _image_input(value: str | None) -> str:
    if not value:
        raise DashScopeProviderError("DashScope image-to-video requires an input image")
    if value.startswith("file://"):
        path = Path(value.replace("file://", ""))
        if not path.exists():
            raise DashScopeProviderError(f"image file does not exist: {path}")
        mime_type = mimetypes.guess_type(path.name)[0] or "image/png"
        return f"data:{mime_type};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"
    return value


def _require_api_key() -> str:
    if not settings.dashscope_api_key:
        raise DashScopeProviderError("DASHSCOPE_API_KEY is required")
    return settings.dashscope_api_key
