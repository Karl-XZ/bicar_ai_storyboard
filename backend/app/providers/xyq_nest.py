from __future__ import annotations

import asyncio
import json
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx

from app.adapters.feishu import FeishuClient
from app.core.config import settings
from app.core.model_aliases import VIDEO_MODEL_XYQ, normalize_video_model
from app.providers.base import VideoProvider, VideoTaskResult

SUBMIT_RUN_PATH = "/api/biz/v1/skill/submit_run"
GET_THREAD_PATH = "/api/biz/v1/skill/get_thread"
UPLOAD_FILE_PATH = "/api/biz/v1/skill/upload_file"
VIDEO_SUFFIXES = (".mp4", ".mov", ".webm", ".m4v", ".avi", ".mkv")
_XYQ_SUBMIT_LOCK = asyncio.Lock()


class XYQNestProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class _UploadedAsset:
    label: str
    asset_id: str


@dataclass(frozen=True)
class _TaskRef:
    thread_id: str
    run_id: str
    web_thread_link: str | None = None


class XYQNestVideoProvider(VideoProvider):
    async def create_video_task(self, payload: dict) -> VideoTaskResult:
        access_key = _require_access_key()
        async with _XYQ_SUBMIT_LOCK:
            uploaded_assets = await _upload_assets_from_payload(payload, access_key)
            body = {
                "message": _build_video_message(payload, uploaded_assets),
            }
            if uploaded_assets:
                body["asset_ids"] = [asset.asset_id for asset in uploaded_assets]
            data = await _api_post(path=SUBMIT_RUN_PATH, access_key=access_key, body=body, timeout=60)
        run = data.get("run") or {}
        thread_id = str(run.get("thread_id") or "").strip()
        run_id = str(run.get("run_id") or "").strip()
        if not thread_id or not run_id:
            raise XYQNestProviderError("小云雀 submit_run 未返回 thread_id/run_id")
        return VideoTaskResult(
            provider_task_id=_encode_task_ref(
                _TaskRef(
                    thread_id=thread_id,
                    run_id=run_id,
                    web_thread_link=str(data.get("web_thread_link") or "").strip() or None,
                )
            )
        )

    async def poll_video_task(self, provider_task_id: str) -> dict:
        access_key = _require_access_key()
        task_ref = _decode_task_ref(provider_task_id)
        attempts = max(int(settings.video_max_polling_attempts), 1)
        interval = max(int(settings.video_polling_interval_seconds), 1)
        last_message = ""
        for attempt in range(attempts):
            data = await _api_post(
                path=GET_THREAD_PATH,
                access_key=access_key,
                body={"thread_id": task_ref.thread_id, "run_id": task_ref.run_id, "after_seq": 0},
                timeout=60,
            )
            thread = data.get("thread") or {}
            run_list = thread.get("run_list") or []
            if not run_list:
                raise XYQNestProviderError("小云雀 get_thread 未返回 run_list")
            run = run_list[0] or {}
            run_state = _normalize_run_state(run.get("state"))
            last_message = _extract_text_summary(run) or last_message

            if run_state == 3:
                result_url = _extract_result_url(run)
                if not result_url:
                    detail = f"：{last_message}" if last_message else ""
                    raise XYQNestProviderError(f"小云雀任务已完成，但没有可下载结果{detail}")
                async with httpx.AsyncClient(timeout=180, follow_redirects=True) as client:
                    response = await client.get(result_url, headers={"User-Agent": "biche-storyboard/1.0"})
                try:
                    response.raise_for_status()
                except httpx.HTTPError as exc:
                    raise XYQNestProviderError(
                        f"小云雀结果下载失败：status={response.status_code}, url={result_url}"
                    ) from exc
                mime_type = response.headers.get("content-type", "video/mp4").split(";")[0]
                if not mime_type.startswith("video/") and not _looks_like_video_url(result_url):
                    raise XYQNestProviderError(f"小云雀结果不是视频资产：{result_url}")
                return {
                    "status": "succeeded",
                    "provider_task_id": provider_task_id,
                    "video_bytes": response.content,
                    "mime_type": mime_type,
                    "provider_response": {
                        "run": run,
                        "result_url": result_url,
                        "web_thread_link": task_ref.web_thread_link,
                    },
                }

            if run_state == 4:
                raise XYQNestProviderError(_normalize_xyq_failure_reason(str(run.get("fail_reason") or "小云雀任务失败")))
            if run_state == 5:
                raise XYQNestProviderError("小云雀任务已取消")
            if attempt < attempts - 1:
                await asyncio.sleep(interval)

        detail = f"：{last_message}" if last_message else ""
        raise XYQNestProviderError(f"小云雀任务超时{detail}")


async def _upload_assets_from_payload(payload: dict, access_key: str) -> list[_UploadedAsset]:
    ordered_sources: list[tuple[str, str]] = []
    if payload.get("first_frame_url"):
        ordered_sources.append(("首帧参考", str(payload["first_frame_url"])))
    if payload.get("last_frame_url"):
        ordered_sources.append(("尾帧参考", str(payload["last_frame_url"])))
    if payload.get("reference_image_url"):
        ordered_sources.append(("参考图", str(payload["reference_image_url"])))
    for index, url in enumerate(payload.get("keyframe_urls") or [], start=1):
        if url:
            ordered_sources.append((f"关键帧参考{index}", str(url)))

    uploaded: list[_UploadedAsset] = []
    seen: set[str] = set()
    for label, source_url in ordered_sources:
        if source_url in seen:
            continue
        seen.add(source_url)
        filename, content, mime_type = await _materialize_file(source_url)
        asset_id = await _upload_file(
            access_key=access_key,
            filename=filename,
            content=content,
            mime_type=mime_type,
        )
        uploaded.append(_UploadedAsset(label=label, asset_id=asset_id))
    return uploaded


async def _materialize_file(source_url: str) -> tuple[str, bytes, str]:
    if source_url.startswith("file://"):
        path = Path(source_url.replace("file://", ""))
        if not path.exists():
            raise XYQNestProviderError(f"小云雀源文件不存在：{path}")
        mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return path.name, path.read_bytes(), mime_type
    if source_url.startswith("feishu://"):
        token = source_url.replace("feishu://", "", 1).strip().strip("/")
        if not token:
            raise XYQNestProviderError("小云雀飞书参考素材缺少文件 token")
        try:
            return await FeishuClient().download_drive_file(token)
        except Exception as exc:
            raise XYQNestProviderError(f"failed to fetch reference asset: {source_url}") from exc

    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        response = await client.get(source_url, headers={"User-Agent": "biche-storyboard/1.0"})
    try:
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise XYQNestProviderError(f"failed to fetch reference asset: {source_url}") from exc

    parsed = urlparse(source_url)
    filename = Path(parsed.path).name or "reference"
    mime_type = response.headers.get("content-type", "").split(";")[0] or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    if "." not in filename:
        suffix = mimetypes.guess_extension(mime_type) or ".bin"
        filename = f"{filename}{suffix}"
    return filename, response.content, mime_type


async def _upload_file(*, access_key: str, filename: str, content: bytes, mime_type: str) -> str:
    async with httpx.AsyncClient(timeout=180) as client:
        response = await client.post(
            f"{settings.xyq_base_url.rstrip('/')}{UPLOAD_FILE_PATH}",
            headers={"Authorization": f"Bearer {access_key}"},
            data={"accessKey": access_key},
            files={"file": (filename, content, mime_type)},
        )
    data = _decode_response(response)
    asset_id = str(data.get("pippit_asset_id") or data.get("asset_id") or "").strip()
    if not asset_id:
        raise XYQNestProviderError("小云雀 upload_file 未返回 asset_id")
    return asset_id


async def _api_post(*, path: str, access_key: str, body: dict, timeout: float) -> dict:
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            f"{settings.xyq_base_url.rstrip('/')}{path}",
            headers={
                "Authorization": f"Bearer {access_key}",
                "Content-Type": "application/json",
            },
            json=body,
        )
    return _decode_response(response)


def _decode_response(response: httpx.Response) -> dict:
    try:
        payload = response.json()
    except ValueError as exc:
        raise XYQNestProviderError(f"小云雀返回了非 JSON 响应：{response.text[:300]}") from exc
    try:
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise XYQNestProviderError(
            _normalize_xyq_failure_reason(
                f"小云雀 HTTP 错误：status={response.status_code}, body={json.dumps(payload, ensure_ascii=False)[:300]}"
            )
        ) from exc
    if str(payload.get("ret")) != "0":
        raise XYQNestProviderError(
            _normalize_xyq_failure_reason(
                f"小云雀 API 错误：ret={payload.get('ret')}, errmsg={payload.get('errmsg')}"
            )
        )
    return payload.get("data") or {}


def _build_video_message(payload: dict, uploaded_assets: list[_UploadedAsset]) -> str:
    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
        raise XYQNestProviderError("小云雀视频提示词不能为空")
    lines = [
        "请直接生成 1 条分镜视频，不要反问，不要要求我补充信息，也不要输出多套方案。",
        "如有附件，请将其视为我已经确认好的参考素材，并直接完成创作。",
        f"画面描述：{prompt}",
    ]
    camera_motion = str(payload.get("camera_motion") or "").strip()
    negative_prompt = str(payload.get("negative_prompt") or "").strip()
    duration_seconds = payload.get("duration_seconds")
    keyframe_time_seconds = payload.get("keyframe_time_seconds")
    model = normalize_video_model(payload.get("model") or payload.get("model_id") or settings.xyq_video_model)
    if camera_motion:
        lines.append(f"镜头运动：{camera_motion}")
    if negative_prompt:
        lines.append(f"避免出现：{negative_prompt}")
    if duration_seconds:
        lines.append(f"目标时长：{_format_seconds(duration_seconds)} 秒")
    if model:
        lines.append(f"任务标记：{model or VIDEO_MODEL_XYQ}")
    if uploaded_assets:
        lines.append("附件说明：")
        for index, asset in enumerate(uploaded_assets, start=1):
            lines.append(f"{index}. {asset.label}")
        lines.append("首尾帧强约束：")
        lines.append("- 第一帧必须尽可能贴近“首帧参考”的主体、构图、机位和动作起点。")
        lines.append("- 最后一帧必须尽可能贴近“尾帧参考”的主体、构图、机位和动作终点。")
        lines.append("- 不允许把尾帧内容提前到开头，也不允许把首帧内容拖到结尾。")
        lines.append("- 中间过程只能从首帧自然过渡到尾帧，不能交换起止语义。")
        lines.append("其他附件用于主体、风格、服装、场景和光线一致性参考。")
    if payload.get("keyframe_urls") and keyframe_time_seconds is not None:
        lines.append(
            f"关键帧时间约束：关键帧参考应出现在第 {_format_seconds(keyframe_time_seconds)} 秒附近，"
            "前后运动必须自然衔接，不能把关键帧当作首帧或尾帧。"
        )
    lines.append("输出要求：直接产出最终视频，保持角色、服装、光线和空间关系稳定。")
    return "\n".join(lines)


def _format_seconds(value) -> str:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return str(value)
    if seconds.is_integer():
        return str(int(seconds))
    return f"{seconds:.2f}".rstrip("0").rstrip(".")


def _encode_task_ref(task_ref: _TaskRef) -> str:
    compact = json.dumps(
        {
            "thread_id": task_ref.thread_id,
            "run_id": task_ref.run_id,
            **({"web_thread_link": task_ref.web_thread_link} if task_ref.web_thread_link else {}),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    if len(compact) <= 255:
        return compact
    return json.dumps({"thread_id": task_ref.thread_id, "run_id": task_ref.run_id}, separators=(",", ":"))


def _decode_task_ref(provider_task_id: str) -> _TaskRef:
    raw = str(provider_task_id or "").strip()
    if not raw:
        raise XYQNestProviderError("小云雀 provider_task_id 不能为空")
    if raw.startswith("{"):
        parsed = json.loads(raw)
        thread_id = str(parsed.get("thread_id") or "").strip()
        run_id = str(parsed.get("run_id") or "").strip()
        if not thread_id or not run_id:
            raise XYQNestProviderError("小云雀 provider_task_id 无效")
        return _TaskRef(
            thread_id=thread_id,
            run_id=run_id,
            web_thread_link=str(parsed.get("web_thread_link") or "").strip() or None,
        )
    if "|" in raw:
        thread_id, run_id = raw.split("|", 1)
        if thread_id and run_id:
            return _TaskRef(thread_id=thread_id, run_id=run_id)
    raise XYQNestProviderError("小云雀 provider_task_id 格式暂不支持")


def _extract_result_url(run: dict) -> str | None:
    candidates = _collect_media_urls(run)
    for url, is_video in candidates:
        if is_video:
            return url
    return candidates[0][0] if candidates else None


def _collect_media_urls(value, *, video_hint: bool = False) -> list[tuple[str, bool]]:
    candidates: list[tuple[str, bool]] = []
    parsed_json = _parse_nested_json(value)
    if parsed_json is not None:
        candidates.extend(_collect_media_urls(parsed_json, video_hint=video_hint))
    elif isinstance(value, dict):
        current_hint = video_hint or _mapping_looks_like_video(value)
        url = value.get("url")
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            candidates.append((url, current_hint or _looks_like_video_url(url)))
        for child in value.values():
            candidates.extend(_collect_media_urls(child, video_hint=current_hint))
    elif isinstance(value, list):
        for child in value:
            candidates.extend(_collect_media_urls(child, video_hint=video_hint))
    deduped: list[tuple[str, bool]] = []
    seen: dict[str, bool] = {}
    for url, is_video in candidates:
        seen[url] = seen.get(url, False) or is_video
    for url, is_video in seen.items():
        deduped.append((url, is_video))
    return deduped


def _mapping_looks_like_video(value: dict) -> bool:
    keys = ("type", "subtype", "mime_type", "content_type", "media_type", "file_type", "name")
    joined = " ".join(str(value.get(key) or "") for key in keys).lower()
    return "video" in joined or any(suffix in joined for suffix in VIDEO_SUFFIXES)


def _extract_text_summary(run: dict) -> str:
    parts = _collect_text_fragments(run)
    for part in parts:
        if "http" not in part:
            return part
    return ""


def _collect_text_fragments(value) -> list[str]:
    fragments: list[str] = []
    parsed_json = _parse_nested_json(value)
    if parsed_json is not None:
        fragments.extend(_collect_text_fragments(parsed_json))
    elif isinstance(value, str):
        stripped = value.strip()
        if stripped:
            fragments.append(stripped)
    elif isinstance(value, list):
        for child in value:
            fragments.extend(_collect_text_fragments(child))
    elif isinstance(value, dict):
        for key in ("text", "title", "content", "message", "description", "desc", "question", "prompt"):
            if key in value:
                fragments.extend(_collect_text_fragments(value.get(key)))
    return fragments


def _looks_like_video_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return path.endswith(VIDEO_SUFFIXES)


def _normalize_xyq_failure_reason(message: str) -> str:
    normalized = (message or "").strip()
    lower = normalized.lower()
    human_image_patterns = (
        "真人",
        "人物",
        "人脸",
        "人像",
        "portrait",
        "real person",
        "real-person",
        "human face",
        "human image",
        "face image",
        "facial",
    )
    policy_patterns = (
        "not allowed",
        "not support",
        "unsupported",
        "forbidden",
        "prohibit",
        "reject",
        "refuse",
        "审核",
        "风控",
        "违规",
        "敏感",
        "安全",
        "policy",
        "compliance",
        "moderation",
    )
    if any(pattern in lower for pattern in human_image_patterns) and any(pattern in lower for pattern in policy_patterns):
        return "小云雀 当前不支持上传真人参考图。请改用非真人参考图，或切换到其他视频模型后重试。"
    return normalized


def _parse_nested_json(value):
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped or stripped[0] not in "{[":
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None


def _normalize_run_state(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _require_access_key() -> str:
    if not settings.xyq_access_key:
        raise XYQNestProviderError("XYQ_ACCESS_KEY 未配置")
    return settings.xyq_access_key
