from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import parse_qs, urlparse

import httpx
from sqlalchemy.orm import Session

from app.adapters.feishu import FeishuClient
from app.core.config import settings
from app.models.project import Project
from app.services.feishu_storyboard import FeishuStoryboardService, ProvisionedProject


ProgressNotifier = Callable[[str], Awaitable[None]]


class VideoStoryboardError(RuntimeError):
    pass


@dataclass(frozen=True)
class VideoStoryboardShot:
    scene_description: str
    first_frame_prompt: str = ""
    last_frame_prompt: str = ""
    video_prompt: str = ""
    keyframe_prompt: str = ""
    camera_motion: str = ""
    consistency_notes: str = ""
    negative_prompt: str = ""


@dataclass(frozen=True)
class VideoStoryboardResult:
    project: Project
    table_url: str | None
    folder_url: str | None
    source_filename: str
    frame_count: int
    shot_count: int
    vision_model: str


class VideoStoryboardService:
    def __init__(self, db: Session, feishu: FeishuClient | None = None) -> None:
        self.db = db
        self.feishu = feishu or FeishuClient()
        self.storyboard = FeishuStoryboardService(db, feishu=self.feishu)

    async def create_project_from_video(
        self,
        *,
        video_reference: str,
        project_name: str | None = None,
        chat_id: str | None = None,
        parent_folder_url: str | None = None,
        sample_count: int = 10,
        target_shots: int | None = None,
        vision_model: str | None = None,
        progress_notifier: ProgressNotifier | None = None,
    ) -> VideoStoryboardResult:
        reference = str(video_reference or "").strip()
        if not reference:
            raise VideoStoryboardError("缺少视频链接或飞书文件地址。")
        sample_count = max(3, min(int(sample_count or 10), 20))
        target_shots = max(3, min(int(target_shots or sample_count), 30))
        model = (vision_model or settings.video_storyboard_vision_model or settings.openrouter_text_model).strip()

        await self._notify(progress_notifier, "已开始视频拆分镜：正在下载视频。")
        source_filename, video_bytes, mime_type = await self._download_video(reference)
        if not video_bytes:
            raise VideoStoryboardError("视频下载结果为空。")
        resolved_project_name = (project_name or Path(source_filename).stem or "视频拆分镜项目").strip()

        with tempfile.TemporaryDirectory(prefix="biche-video-storyboard-") as tmp:
            tmp_dir = Path(tmp)
            video_path = tmp_dir / self._safe_filename(source_filename, mime_type=mime_type)
            video_path.write_bytes(video_bytes)
            await self._notify(progress_notifier, f"视频已下载：{source_filename}，正在抽取 {sample_count} 张关键画面。")
            frames = await self._extract_frames(video_path, tmp_dir / "frames", sample_count=sample_count)
            if not frames:
                raise VideoStoryboardError("没有成功抽取到视频帧，请确认文件是可解码的视频。")
            await self._notify(progress_notifier, f"已抽取 {len(frames)} 张画面，正在调用视觉模型生成分镜结构。")
            shots = await self._analyze_frames(
                frames,
                source_filename=source_filename,
                target_shots=target_shots,
                model=model,
            )

        if not shots:
            raise VideoStoryboardError("视觉模型没有返回可写入的分镜结果。")

        await self._notify(progress_notifier, f"视觉拆解完成，共 {len(shots)} 条分镜，正在创建飞书分镜项目。")
        provisioned = await self.storyboard.create_project_from_bot(
            project_name=resolved_project_name,
            chat_id=chat_id,
            parent_folder_url=parent_folder_url,
        )
        await self.populate_project_table(provisioned, shots)
        await self.storyboard.sync_from_feishu(provisioned.project)
        await self._notify(progress_notifier, "视频拆分镜项目已创建并写入表格。")
        return VideoStoryboardResult(
            project=provisioned.project,
            table_url=provisioned.table_url,
            folder_url=provisioned.folder_url,
            source_filename=source_filename,
            frame_count=len(frames),
            shot_count=len(shots),
            vision_model=model,
        )

    async def populate_project_table(self, provisioned: ProvisionedProject, shots: list[VideoStoryboardShot]) -> None:
        project = provisioned.project
        if not project.feishu_app_token or not project.feishu_table_id:
            raise VideoStoryboardError("分镜项目缺少飞书表格配置，无法写入。")
        await self.storyboard.ensure_table_fields(project)
        response = await self.feishu.search_records(project.feishu_app_token, project.feishu_table_id, {})
        records = response.get("data", {}).get("items", [])
        defaults = self.storyboard._default_record_fields(project)
        updates: list[dict] = []
        creates: list[dict] = []
        for index, shot in enumerate(shots):
            fields = {
                **defaults,
                "场景描述": shot.scene_description,
                "关键帧提示词": shot.keyframe_prompt or shot.first_frame_prompt or shot.video_prompt,
                "首帧提示词": shot.first_frame_prompt,
                "尾帧提示词": shot.last_frame_prompt,
                "视频 Prompt": shot.video_prompt or shot.scene_description,
                "负面 Prompt": shot.negative_prompt,
                "镜头运动": shot.camera_motion,
                "一致性说明": shot.consistency_notes,
                "审核状态": "草稿",
                "图片生成状态": "未开始",
                "生成状态": "未开始",
                "重新生成状态": "未开始",
                "Prompt 版本": 1,
            }
            if index < len(records) and records[index].get("record_id"):
                updates.append({"record_id": records[index]["record_id"], "fields": fields})
            else:
                creates.append({"fields": fields})
        if updates:
            await self.feishu.batch_update_records(project.feishu_app_token, project.feishu_table_id, updates)
        if creates:
            await self.feishu.batch_create_records(project.feishu_app_token, project.feishu_table_id, creates)

    async def _download_video(self, reference: str) -> tuple[str, bytes, str]:
        token = self._extract_feishu_file_token(reference)
        if token:
            return await self.feishu.download_drive_file(token)
        parsed = urlparse(reference)
        if parsed.scheme not in {"http", "https"}:
            raise VideoStoryboardError("视频地址不是飞书文件链接，也不是可下载的 http/https 链接。")
        async with httpx.AsyncClient(timeout=180) as client:
            response = await client.get(reference, follow_redirects=True)
            response.raise_for_status()
        filename = Path(parsed.path).name or "source_video.mp4"
        mime_type = response.headers.get("content-type") or mimetypes.guess_type(filename)[0] or "video/mp4"
        return filename, response.content, mime_type

    async def _extract_frames(self, video_path: Path, output_dir: Path, *, sample_count: int) -> list[Path]:
        output_dir.mkdir(parents=True, exist_ok=True)
        duration = await self._probe_duration(video_path)
        if duration and duration > 0:
            timestamps = self._sample_timestamps(duration, sample_count)
            frames: list[Path] = []
            for index, timestamp in enumerate(timestamps, start=1):
                frame_path = output_dir / f"frame_{index:03d}.jpg"
                cmd = [
                    "ffmpeg",
                    "-y",
                    "-ss",
                    f"{timestamp:.3f}",
                    "-i",
                    str(video_path),
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale='min(768,iw)':-2",
                    "-q:v",
                    "3",
                    "-update",
                    "1",
                    str(frame_path),
                ]
                try:
                    await self._run_process(cmd)
                except VideoStoryboardError:
                    if not frame_path.exists() or frame_path.stat().st_size <= 0:
                        continue
                if frame_path.exists() and frame_path.stat().st_size > 0:
                    frames.append(frame_path)
            return frames
        pattern = output_dir / "frame_%03d.jpg"
        await self._run_process(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(video_path),
                "-vf",
                f"fps=1,scale='min(768,iw)':-2",
                "-frames:v",
                str(sample_count),
                "-q:v",
                "3",
                str(pattern),
            ]
        )
        return sorted(output_dir.glob("frame_*.jpg"))[:sample_count]

    async def _probe_duration(self, video_path: Path) -> float | None:
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(video_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _stderr = await proc.communicate()
            if proc.returncode != 0:
                return None
            return float(stdout.decode("utf-8", errors="ignore").strip())
        except (OSError, ValueError):
            return None

    async def _run_process(self, cmd: list[str]) -> None:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise VideoStoryboardError("本机缺少 ffmpeg/ffprobe，无法抽帧。") from exc
        _stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            detail = stderr.decode("utf-8", errors="ignore").strip()[:500]
            raise VideoStoryboardError(f"视频抽帧失败：{detail or 'ffmpeg exited with error'}")

    def _sample_timestamps(self, duration: float, sample_count: int) -> list[float]:
        if sample_count <= 1:
            return [max(duration / 2, 0.0)]
        if duration <= 0.4:
            return [max(duration / 2, 0.0) for _ in range(sample_count)]
        margin = min(1.0, max(duration * 0.12, 0.2))
        start = min(margin, max(duration / 2, 0.0))
        end = max(duration - margin, start)
        if end <= start:
            return [start for _ in range(sample_count)]
        step = (end - start) / max(sample_count - 1, 1)
        return [start + step * index for index in range(sample_count)]

    async def _analyze_frames(
        self,
        frames: list[Path],
        *,
        source_filename: str,
        target_shots: int,
        model: str,
    ) -> list[VideoStoryboardShot]:
        api_key = settings.openrouter_api_key.strip()
        if not api_key:
            raise VideoStoryboardError("缺少 OpenRouter API Key，无法调用视觉模型拆解视频。")
        content: list[dict] = [{"type": "text", "text": self._analysis_prompt(source_filename, target_shots)}]
        for index, frame in enumerate(frames, start=1):
            encoded = base64.b64encode(frame.read_bytes()).decode("ascii")
            content.append({"type": "text", "text": f"抽样画面 {index:02d}"})
            content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}})
        async with httpx.AsyncClient(timeout=300) as client:
            response = await client.post(
                f"{settings.openrouter_base_url.rstrip('/')}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://ocnwptzvwvt6.feishu.cn",
                    "X-Title": "Bicar AI Video Storyboard",
                },
                json={"model": model, "messages": [{"role": "user", "content": content}]},
            )
        if response.status_code >= 400:
            raise VideoStoryboardError(f"视觉模型调用失败：HTTP {response.status_code} {response.text[:500]}")
        payload = response.json()
        text = str((payload.get("choices") or [{}])[0].get("message", {}).get("content") or "").strip()
        data = self._parse_json_object(text)
        raw_shots = data.get("shots") if isinstance(data, dict) else None
        if not isinstance(raw_shots, list):
            raise VideoStoryboardError("视觉模型返回内容缺少 shots 数组。")
        shots = [self._shot_from_payload(item) for item in raw_shots if isinstance(item, dict)]
        return [shot for shot in shots if shot.scene_description]

    def _analysis_prompt(self, source_filename: str, target_shots: int) -> str:
        return (
            "你是专业广告片分镜导演。请根据用户视频的抽样画面，推断视频叙事结构，并生成可直接写入 AI 分镜表的 JSON。\n"
            f"视频文件名：{source_filename}\n"
            f"目标分镜数量：约 {target_shots} 条。允许根据画面节奏略微增减，但不要少于 3 条。\n"
            "要求：\n"
            "- 每条分镜要适合后续图片/视频生成，不要只写抽象评价。\n"
            "- 画面、动作、光线、主体、镜头运动要具体。\n"
            "- 如果只能从抽帧推断，请在 consistency_notes 里标注“根据抽样画面推断”。\n"
            "- 不要输出 Markdown，不要解释，只返回 JSON object。\n"
            "JSON 格式：\n"
            "{\n"
            '  "shots": [\n'
            "    {\n"
            '      "scene_description": "中文，镜头发生了什么",\n'
            '      "first_frame_prompt": "中文或英文，首帧画面提示词",\n'
            '      "last_frame_prompt": "中文或英文，尾帧画面提示词",\n'
            '      "video_prompt": "连续镜头视频生成提示词",\n'
            '      "keyframe_prompt": "关键画面提示词",\n'
            '      "camera_motion": "推/拉/摇/移/跟拍/固定等",\n'
            '      "consistency_notes": "主体、风格、光线、道具的一致性说明",\n'
            '      "negative_prompt": "避免的问题"\n'
            "    }\n"
            "  ]\n"
            "}"
        )

    def _parse_json_object(self, text: str) -> dict:
        stripped = text.strip()
        fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, flags=re.DOTALL | re.IGNORECASE)
        if fenced:
            stripped = fenced.group(1)
        if not stripped.startswith("{"):
            start = stripped.find("{")
            end = stripped.rfind("}")
            if start >= 0 and end > start:
                stripped = stripped[start : end + 1]
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise VideoStoryboardError(f"视觉模型返回不是有效 JSON：{stripped[:300]}") from exc
        if not isinstance(data, dict):
            raise VideoStoryboardError("视觉模型返回 JSON 不是对象。")
        return data

    def _shot_from_payload(self, payload: dict) -> VideoStoryboardShot:
        def text(*keys: str) -> str:
            for key in keys:
                value = payload.get(key)
                if value is not None:
                    return str(value).strip()
            return ""

        description = text("scene_description", "description", "场景描述", "画面内容")
        return VideoStoryboardShot(
            scene_description=description,
            first_frame_prompt=text("first_frame_prompt", "首帧提示词", "first_frame"),
            last_frame_prompt=text("last_frame_prompt", "尾帧提示词", "last_frame"),
            video_prompt=text("video_prompt", "视频 Prompt", "video"),
            keyframe_prompt=text("keyframe_prompt", "关键帧提示词", "keyframe"),
            camera_motion=text("camera_motion", "镜头运动", "movement"),
            consistency_notes=text("consistency_notes", "一致性说明", "notes"),
            negative_prompt=text("negative_prompt", "负面 Prompt", "negative"),
        )

    def _extract_feishu_file_token(self, value: str) -> str | None:
        raw = str(value or "").strip()
        if raw.startswith("feishu://"):
            return raw.replace("feishu://", "", 1).strip().strip("/")
        parsed = urlparse(raw)
        query = parse_qs(parsed.query)
        for key in ("file_token", "token"):
            if query.get(key):
                return str(query[key][0]).strip()
        parts = [part for part in parsed.path.split("/") if part]
        if "file" in parts:
            index = parts.index("file")
            if len(parts) > index + 1:
                return parts[index + 1]
        return None

    def _safe_filename(self, filename: str, *, mime_type: str = "") -> str:
        name = Path(str(filename or "source_video")).name.strip() or "source_video"
        name = re.sub(r"[\\/:*?\"<>|]+", "_", name)
        if Path(name).suffix:
            return name
        suffix = mimetypes.guess_extension(mime_type or "") or ".mp4"
        return f"{name}{suffix}"

    async def _notify(self, progress_notifier: ProgressNotifier | None, message: str) -> None:
        if progress_notifier:
            await progress_notifier(message)
