import asyncio
import hashlib
import re
from datetime import datetime
from typing import Awaitable, Callable, TypeVar
from uuid import UUID

import httpx
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.model_aliases import IMAGE_MODEL_NANOBANANA, normalize_image_model, normalize_video_model, video_provider_display
from app.domain.enums import AssetType, FrameType, JobStatus, JobType, Satisfaction, ShotStatus
from app.models.asset import Asset
from app.models.job import GenerationJob
from app.models.shot import Shot
from app.providers.router import ProviderRouter
from app.services.assets import AssetService
from app.services.idempotency import make_idempotency_key
from app.services.jobs import JobService
from app.services.projects import ProjectService
from app.services.state_machine import StateMachineService

T = TypeVar("T")
RATE_LIMIT_BACKOFF_SECONDS = (60, 300, 1200)
XYQ_RATE_LIMIT_BACKOFF_SECONDS = (300, 600, 1200, 1800, 3600)
RATE_LIMIT_PATTERNS = (
    r"\b429\b",
    r"too many requests",
    r"rate limit",
    r"rate[- ]limited",
    r"resource[_ ]?exhausted",
    r"throttl",
    r"insufficient_quota",
    r"quota exceeded",
    r"非vip用户",
    r"无法使用该功能",
)


class WorkflowError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class WorkflowService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.assets = AssetService(db)
        self.jobs = JobService(db)
        self.projects = ProjectService(db)
        self.state_machine = StateMachineService()
        self.provider_router = ProviderRouter()

    def optimize_prompt(self, shot_id: UUID) -> GenerationJob:
        return asyncio.run(self.optimize_prompt_async(shot_id))

    async def optimize_prompt_async(self, shot_id: UUID) -> GenerationJob:
        shot = self._require_shot(shot_id)
        provider, model_id = self._provider_model(shot, "text")
        key = make_idempotency_key("prompt", shot.project_id, shot.id, shot.prompt_version, provider, model_id)
        job = self.jobs.get_or_create(
            project_id=shot.project_id,
            shot_id=shot.id,
            job_type=JobType.PROMPT_OPTIMIZE,
            idempotency_key=key,
            input_payload={
                "scene_description": shot.scene_description,
                "prompt_version": shot.prompt_version,
                "model": model_id,
            },
            provider=provider,
            model_id=model_id,
            prompt_version=shot.prompt_version,
        )
        if settings.workflow_inline_execution:
            await self._run_prompt_job(job, shot)
        self.db.commit()
        return job

    async def revise_prompts_async(self, shot_id: UUID, *, prompt_keys: list[str], rejection_reason: str) -> Shot:
        shot = self._require_shot(shot_id)
        if not prompt_keys:
            return shot
        provider, model_id = self._provider_model(shot, "text")
        provider_instance = self.provider_router.text(provider)
        result = await provider_instance.optimize_prompt(
            {
                "scene_description": shot.scene_description,
                "existing_prompts": shot.prompts or {},
                "revision_instruction": rejection_reason,
                "selected_prompt_fields": prompt_keys,
                "project_style": "cinematic realistic",
                "model": model_id,
            }
        )
        generated = {
            "keyframe_prompt": result.keyframe_prompt,
            "first_frame_prompt": result.first_frame_prompt,
            "last_frame_prompt": result.last_frame_prompt,
            "video_prompt": result.video_prompt,
            "negative_prompt": result.negative_prompt,
            "camera_motion": result.camera_motion,
            "consistency_notes": result.consistency_notes,
            "style_tags": result.style_tags,
        }
        current = shot.prompts or {}
        selected = {key: generated[key] for key in prompt_keys if key in generated}
        shot.prompts = {**current, **selected}
        shot.prompt_version += 1
        shot.status = ShotStatus.PENDING_FRAMES.value
        self.db.commit()
        return shot

    def generate_frames(
        self,
        shot_id: UUID,
        *,
        include_first_frame: bool = True,
        include_last_frame: bool = True,
        include_keyframes: bool = True,
    ) -> list[GenerationJob]:
        return asyncio.run(
            self.generate_frames_async(
                shot_id,
                include_first_frame=include_first_frame,
                include_last_frame=include_last_frame,
                include_keyframes=include_keyframes,
            )
        )

    async def generate_frames_async(
        self,
        shot_id: UUID,
        *,
        include_first_frame: bool = True,
        include_last_frame: bool = True,
        include_keyframes: bool = True,
        force: bool = False,
    ) -> list[GenerationJob]:
        shot = self._require_shot(shot_id)
        if not shot.prompts:
            raise WorkflowError("MISSING_PROMPT", "镜头缺少 Prompt，无法生成帧图")

        jobs: list[GenerationJob] = []
        frame_specs: list[tuple[FrameType, int]] = []
        if include_first_frame:
            frame_specs.append((FrameType.FIRST_FRAME, 1))
        if include_last_frame:
            frame_specs.append((FrameType.LAST_FRAME, 1))
        if include_keyframes:
            frame_specs.extend((FrameType.KEYFRAME, index) for index in range(1, settings.workflow_keyframe_variants + 1))

        prompt_hash = self._prompt_hash(shot.prompts)
        provider, model_id = self._provider_model(shot, "image")
        size = self._image_size_for_shot(shot)
        for frame_type, variant in frame_specs:
            frame_prompt = self._frame_prompt(shot, frame_type)
            key = make_idempotency_key("image", shot.project_id, shot.id, frame_type.value, variant, prompt_hash, provider, model_id)
            job = self.jobs.get_or_create(
                project_id=shot.project_id,
                shot_id=shot.id,
                job_type=JobType.IMAGE_GENERATE,
                idempotency_key=key,
                input_payload={
                    "frame_type": frame_type.value,
                    "variant_index": variant,
                    "prompt_hash": prompt_hash,
                    "prompt": frame_prompt,
                    "negative_prompt": (shot.prompts or {}).get("negative_prompt"),
                    "reference_image_urls": (shot.prompts or {}).get("reference_image_urls") or [],
                    "model": model_id,
                    "size": size,
                },
                provider=provider,
                model_id=model_id,
                prompt_version=shot.prompt_version,
            )
            jobs.append(job)
            if settings.workflow_inline_execution:
                if not force and job.status == JobStatus.SUCCEEDED.value and (job.output_payload or {}).get("asset_id"):
                    continue
                await self._run_image_job(job, shot, frame_type, variant, prompt_hash)

        shot.status = ShotStatus.PENDING_REVIEW.value
        self.db.commit()
        return jobs

    def generate_batch_frames(
        self,
        project_id: UUID,
        batch_no: str,
        *,
        include_keyframes: bool = True,
    ) -> list[GenerationJob]:
        return asyncio.run(self.generate_batch_frames_async(project_id, batch_no, include_keyframes=include_keyframes))

    async def generate_batch_frames_async(self, project_id: UUID, batch_no: str, *, include_keyframes: bool = True) -> list[GenerationJob]:
        shots = self.projects.list_shots(project_id, batch_no=batch_no)
        jobs: list[GenerationJob] = []
        for shot in shots:
            if not shot.prompts or not any(shot.prompts.values()):
                await self.optimize_prompt_async(shot.id)
                self.db.refresh(shot)
            jobs.extend(await self.generate_frames_async(shot.id, include_keyframes=include_keyframes))
        return jobs

    def generate_video(self, shot_id: UUID) -> GenerationJob:
        return asyncio.run(self.generate_video_async(shot_id))

    async def generate_video_async(self, shot_id: UUID, *, force: bool = False) -> GenerationJob:
        shot = self._require_shot(shot_id)
        if shot.status == ShotStatus.PENDING_REVIEW.value:
            shot.status = ShotStatus.APPROVED.value
        video_inputs = self._video_input_assets(shot)
        provider, model_id = self._provider_model(shot, "video")
        duration_seconds = self._video_duration(shot)

        key = make_idempotency_key(
            "video",
            shot.project_id,
            shot.id,
            shot.prompt_version,
            video_inputs.get("first_frame_asset_id"),
            video_inputs.get("last_frame_asset_id"),
            video_inputs.get("reference_image_url"),
            video_inputs.get("keyframe_asset_ids"),
            (shot.prompts or {}).get("video_run_id"),
            duration_seconds,
            video_inputs.get("keyframe_time_seconds"),
            provider,
            model_id,
        )
        job = self.jobs.get_or_create(
            project_id=shot.project_id,
            shot_id=shot.id,
            job_type=JobType.VIDEO_GENERATE,
            idempotency_key=key,
            input_payload={
                "first_frame_url": video_inputs.get("first_frame_url"),
                "last_frame_url": video_inputs.get("last_frame_url"),
                "reference_image_url": video_inputs.get("reference_image_url"),
                "keyframe_urls": video_inputs.get("keyframe_urls") or [],
                "prompt": video_inputs.get("prompt"),
                "negative_prompt": (shot.prompts or {}).get("negative_prompt"),
                "camera_motion": (shot.prompts or {}).get("camera_motion"),
                "model": model_id,
                "duration_seconds": duration_seconds,
                "keyframe_time_seconds": video_inputs.get("keyframe_time_seconds"),
            },
            provider=provider,
            model_id=model_id,
            prompt_version=shot.prompt_version,
        )
        if settings.workflow_inline_execution:
            if not force and job.status == JobStatus.SUCCEEDED.value and (job.output_payload or {}).get("asset_id"):
                shot.status = ShotStatus.PENDING_ACCEPTANCE.value
                self.db.commit()
                return job
            await self._run_video_job(job, shot)
        self.db.commit()
        return job

    def approve_shot(self, shot_id: UUID) -> Shot:
        shot = self._require_shot(shot_id)
        self._video_input_assets(shot)
        shot.status = ShotStatus.APPROVED.value
        self.db.commit()
        return shot

    def archive_shot(self, shot_id: UUID, satisfaction: Satisfaction) -> Asset:
        shot = self._require_shot(shot_id)
        video = self.assets.latest_for_shot(shot.id, AssetType.VIDEO)
        if not video:
            raise WorkflowError("MISSING_VIDEO", "镜头缺少视频资产，无法归档")
        archive = self.assets.archive_video(video_asset=video, satisfaction=satisfaction)
        shot.status = (
            ShotStatus.ARCHIVED_SATISFIED.value
            if satisfaction == Satisfaction.SATISFIED
            else ShotStatus.ARCHIVED_UNSATISFIED.value
        )
        self.db.commit()
        return archive

    def reject_shot(self, shot_id: UUID, reason: str) -> Shot:
        shot = self._require_shot(shot_id)
        shot.status = ShotStatus.REJECTED.value
        shot.error_code = "USER_REJECTED"
        shot.error_message = reason
        self.db.commit()
        return shot

    async def _run_prompt_job(self, job: GenerationJob, shot: Shot) -> None:
        self.jobs.mark_running(job)
        provider = self.provider_router.text(job.provider)
        try:
            result = await self._run_with_rate_limit_backoff(
                shot=shot,
                job=job,
                operation_name="Prompt 优化",
                failure_code="PROMPT_RATE_LIMIT_RETRY_EXHAUSTED",
                runner=lambda: provider.optimize_prompt(
                    {
                        "scene_description": shot.scene_description,
                        "project_style": "cinematic realistic",
                        "model": job.model_id,
                    }
                ),
            )
        except WorkflowError as exc:
            self.jobs.mark_failed(job, exc.code, exc.message)
            raise
        except Exception as exc:
            self.jobs.mark_failed(job, type(exc).__name__, str(exc))
            raise WorkflowError("PROMPT_FAILED", str(exc)) from exc
        existing_prompts = shot.prompts or {}
        shot.prompts = {
            **existing_prompts,
            "keyframe_prompt": result.keyframe_prompt,
            "first_frame_prompt": result.first_frame_prompt,
            "last_frame_prompt": result.last_frame_prompt,
            "video_prompt": result.video_prompt,
            "negative_prompt": result.negative_prompt,
            "camera_motion": result.camera_motion,
            "consistency_notes": result.consistency_notes,
            "style_tags": result.style_tags,
        }
        shot.prompt_version += 1
        shot.status = ShotStatus.PENDING_FRAMES.value
        shot.error_code = None
        shot.error_message = None
        self.jobs.mark_succeeded(job, output_payload=shot.prompts)

    async def _run_image_job(
        self,
        job: GenerationJob,
        shot: Shot,
        frame_type: FrameType,
        variant_index: int,
        prompt_hash: str,
    ) -> None:
        self.jobs.mark_running(job)
        provider = self.provider_router.image(job.provider)
        try:
            result = await self._run_with_rate_limit_backoff(
                shot=shot,
                job=job,
                operation_name=f"{frame_type.value} 图片生成",
                failure_code="IMAGE_RATE_LIMIT_RETRY_EXHAUSTED",
                runner=lambda: provider.generate_image(job.input_payload),
            )
        except WorkflowError as exc:
            self.jobs.mark_failed(job, exc.code, exc.message)
            raise
        except Exception as exc:
            self.jobs.mark_failed(job, type(exc).__name__, str(exc))
            raise WorkflowError("IMAGE_FAILED", str(exc)) from exc
        asset_type = {
            FrameType.FIRST_FRAME: AssetType.FIRST_FRAME,
            FrameType.LAST_FRAME: AssetType.LAST_FRAME,
            FrameType.KEYFRAME: AssetType.KEYFRAME,
        }[frame_type]
        filename = f"frames/{shot.shot_no}/v{shot.prompt_version:03d}_{frame_type.value}_{variant_index}.png"
        asset = self.assets.put_bytes(
            project_id=shot.project_id,
            shot_id=shot.id,
            asset_type=asset_type,
            content=result.bytes_data,
            filename=filename,
            provider=job.provider,
            model_id=job.model_id,
            prompt_hash=prompt_hash,
            version=shot.prompt_version,
        )
        shot.error_code = None
        shot.error_message = None
        self.jobs.mark_succeeded(job, {"asset_id": str(asset.id), "url": asset.public_url})

    async def _run_video_job(self, job: GenerationJob, shot: Shot) -> None:
        self.jobs.mark_running(job)
        provider = self.provider_router.video(job.provider)
        try:
            task = await self._run_with_rate_limit_backoff(
                shot=shot,
                job=job,
                operation_name="视频任务创建",
                failure_code="VIDEO_RATE_LIMIT_RETRY_EXHAUSTED",
                runner=lambda: provider.create_video_task(job.input_payload),
            )
            job.provider_task_id = task.provider_task_id
            result = await self._run_with_rate_limit_backoff(
                shot=shot,
                job=job,
                operation_name="视频结果轮询",
                failure_code="VIDEO_RATE_LIMIT_RETRY_EXHAUSTED",
                runner=lambda: provider.poll_video_task(task.provider_task_id),
            )
        except WorkflowError as exc:
            self.jobs.mark_failed(job, exc.code, exc.message)
            shot.status = ShotStatus.VIDEO_FAILED.value
            raise
        except Exception as exc:
            self.jobs.mark_failed(job, type(exc).__name__, str(exc))
            shot.status = ShotStatus.VIDEO_FAILED.value
            raise WorkflowError("VIDEO_FAILED", str(exc)) from exc
        if result["status"] != "succeeded":
            self.jobs.mark_failed(job, "VIDEO_FAILED", "视频 Provider 返回失败状态")
            shot.status = ShotStatus.VIDEO_FAILED.value
            raise WorkflowError("VIDEO_FAILED", "视频 Provider 返回失败状态")
        content = result["video_bytes"]
        prompt_hash = self._prompt_hash(shot.prompts)
        asset = self.assets.put_bytes(
            project_id=shot.project_id,
            shot_id=shot.id,
            asset_type=AssetType.VIDEO,
            content=content,
            filename=f"videos/{shot.shot_no}_v{shot.prompt_version:03d}.mp4",
            provider=job.provider,
            model_id=job.model_id,
            prompt_hash=prompt_hash,
            version=shot.prompt_version,
        )
        shot.status = ShotStatus.PENDING_ACCEPTANCE.value
        shot.error_code = None
        shot.error_message = None
        self.jobs.mark_succeeded(job, {"asset_id": str(asset.id), "url": asset.public_url})

    async def _run_with_rate_limit_backoff(
        self,
        *,
        shot: Shot,
        job: GenerationJob,
        operation_name: str,
        failure_code: str,
        runner: Callable[[], Awaitable[T]],
    ) -> T:
        delays = self._rate_limit_delays(job)
        for attempt_index in range(len(delays) + 1):
            if attempt_index > 0:
                self.jobs.mark_running(job)
            try:
                return await runner()
            except Exception as exc:
                if not self._is_rate_limit_error(exc):
                    raise
                error_text = self._compact_error_message(exc)
                if attempt_index >= len(delays):
                    final_message = self._rate_limit_final_message(
                        operation_name=operation_name,
                        job=job,
                        error_text=error_text,
                    )
                    shot.error_code = failure_code
                    shot.error_message = self._append_error_log(shot.error_message, final_message)
                    await self._sync_shot_error_log(shot)
                    raise WorkflowError(failure_code, final_message) from exc

                retry_number = attempt_index + 1
                delay_seconds = delays[attempt_index]
                retry_message = self._rate_limit_retry_message(
                    operation_name=operation_name,
                    job=job,
                    retry_number=retry_number,
                    delay_seconds=delay_seconds,
                    error_text=error_text,
                )
                self.jobs.mark_retrying(job, "RATE_LIMITED", retry_message)
                shot.error_code = "RATE_LIMITED"
                shot.error_message = self._append_error_log(shot.error_message, retry_message)
                await self._sync_shot_error_log(shot)
                await asyncio.sleep(delay_seconds)

        raise WorkflowError(failure_code, f"{operation_name} 回退重试异常结束")

    def _is_rate_limit_error(self, exc: Exception) -> bool:
        if isinstance(exc, httpx.HTTPStatusError):
            response = exc.response
            if response is not None and response.status_code == 429:
                return True
        response = getattr(exc, "response", None)
        if response is not None and getattr(response, "status_code", None) == 429:
            return True
        text = self._compact_error_message(exc).lower()
        return any(re.search(pattern, text) for pattern in RATE_LIMIT_PATTERNS)

    def _compact_error_message(self, exc: Exception) -> str:
        text = str(exc).strip()
        if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
            details = exc.response.text.strip()
            if details:
                text = f"{text} | body={details}"
        text = re.sub(r"\s+", " ", text)
        return text[:500]

    def _append_error_log(self, current: str | None, line: str) -> str:
        existing_lines = [item for item in (current or "").splitlines() if item.strip()]
        if line in existing_lines:
            return "\n".join(existing_lines)
        existing_lines.append(line)
        return "\n".join(existing_lines[-12:])

    def _rate_limit_retry_message(
        self,
        *,
        operation_name: str,
        job: GenerationJob,
        retry_number: int,
        delay_seconds: int,
        error_text: str,
    ) -> str:
        return (
            f"{self._now_label()} {operation_name}（{self._job_target_label(job)}）遇到限流类错误，"
            f"将在 {self._delay_label(delay_seconds)} 后进行第{retry_number}次回退重试。错误：{error_text}"
        )

    def _rate_limit_final_message(self, *, operation_name: str, job: GenerationJob, error_text: str) -> str:
        retry_count = len(self._rate_limit_delays(job))
        return (
            f"{self._now_label()} {operation_name}（{self._job_target_label(job)}）第{retry_count}次回退重试后仍遇到限流类错误，"
            f"{retry_count}次回退全部失败。最后错误：{error_text}"
        )

    def _job_target_label(self, job: GenerationJob) -> str:
        provider = job.provider or "unknown"
        model = job.model_id or "unknown"
        if provider == "xyq_nest":
            provider = video_provider_display(provider, model)
        return f"{provider}/{model}"

    def _rate_limit_delays(self, job: GenerationJob) -> tuple[int, ...]:
        if (job.provider or "") == "xyq_nest" or normalize_video_model(job.model_id) == "小云雀":
            return XYQ_RATE_LIMIT_BACKOFF_SECONDS
        return RATE_LIMIT_BACKOFF_SECONDS

    def _delay_label(self, delay_seconds: int) -> str:
        mapping = {60: "1 分钟", 300: "5 分钟", 600: "10 分钟", 1200: "20 分钟", 1800: "30 分钟", 3600: "1 小时"}
        return mapping.get(delay_seconds, f"{delay_seconds} 秒")

    def _now_label(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    async def _sync_shot_error_log(self, shot: Shot) -> None:
        project = self.projects.get_project(shot.project_id)
        if not project or not project.feishu_app_token or not project.feishu_table_id or not shot.feishu_record_id:
            self.db.commit()
            return
        try:
            from app.services.feishu_storyboard import FeishuStoryboardService

            await FeishuStoryboardService(self.db).backfill_shots(project, [shot])
        except Exception:
            self.db.commit()

    def _require_shot(self, shot_id: UUID) -> Shot:
        shot = self.db.get(Shot, shot_id)
        if not shot:
            raise WorkflowError("SHOT_NOT_FOUND", "镜头不存在")
        return shot

    def _prompt_hash(self, prompts: dict) -> str:
        return hashlib.sha256(str(sorted((prompts or {}).items())).encode("utf-8")).hexdigest()

    def _provider_model(self, shot: Shot, kind: str) -> tuple[str, str]:
        project = self.projects.get_project(shot.project_id)
        config = ((project.model_config or {}).get(kind) or {}) if project else {}
        prompts = shot.prompts or {}
        configured_provider = str(config.get("provider") or self._default_provider(kind))
        configured_model = prompts.get(f"{kind}_model") or config.get("model_id")
        provider = configured_provider
        model_id = str(configured_model or self._default_model(kind, configured_provider))
        provider = self._infer_provider(kind=kind, provider=provider, model_id=model_id)
        return provider, model_id

    def _default_provider(self, kind: str) -> str:
        return {
            "text": settings.default_text_provider,
            "image": settings.default_image_provider,
            "video": settings.default_video_provider,
        }[kind]

    def _default_model(self, kind: str, provider: str | None = None) -> str:
        selected = provider or self._default_provider(kind)
        if kind == "text":
            return {
                "dashscope": settings.dashscope_text_model,
                "openai": settings.openai_text_model,
                "deepseek": settings.deepseek_text_model,
                "openrouter": settings.openrouter_text_model,
            }.get(selected, settings.dashscope_text_model)
        if kind == "image":
            return {
                "dashscope": settings.dashscope_image_model,
                "openai": settings.openai_image_model,
                "nano_banana_2": IMAGE_MODEL_NANOBANANA,
                "openrouter": IMAGE_MODEL_NANOBANANA,
            }.get(selected, settings.dashscope_image_model)
        return {
            "dashscope": settings.dashscope_video_model,
            "seedance_2_0": settings.seedance_model_id or settings.dashscope_video_model,
            "xyq_nest": settings.xyq_video_model,
        }.get(selected, settings.dashscope_video_model)

    def _infer_provider(self, *, kind: str, provider: str, model_id: str) -> str:
        if kind == "image":
            model_id = normalize_image_model(model_id)
        elif kind == "video":
            model_id = normalize_video_model(model_id)
        normalized = model_id.lower().replace("-", "_").replace("/", "_")
        inferred = None
        if normalized in {"mock", "mock_text", "mock_image", "mock_video"}:
            inferred = "mock"
        elif normalized in {"nanobanana", "neobunana", "nano_banana_2", "gemini_3.1_flash_image_preview"}:
            if settings.openrouter_api_key:
                inferred = "openrouter"
            elif settings.google_api_key:
                inferred = "nano_banana_2"
        elif normalized.startswith("deepseek_v4") or normalized.startswith("deepseek_chat") or normalized.startswith("deepseek_reasoner"):
            inferred = "deepseek"
        elif normalized in {"gpt2", "openai_gpt_5.4_image_2"}:
            inferred = "openrouter"
        elif normalized in {"gpt_image_2", "gpt_image_1"}:
            inferred = "openrouter" if settings.openrouter_api_key else "openai"
        elif normalized.startswith(("qwen", "wan", "z_image")):
            inferred = "dashscope"
        elif normalized.startswith("seedance"):
            inferred = "seedance_2_0"
        elif normalized == "小云雀" or normalized.startswith(("xyq", "xiaoyunque", "xiao_yunque")):
            inferred = "xyq_nest"
        elif normalized.startswith(("google_gemini", "openai_gpt", "anthropic_", "x_ai_", "meta_llama", "mistralai_", "moonshotai_")):
            inferred = "openrouter"
        elif normalized.startswith("gpt"):
            inferred = "openai"

        if normalized in {"mock", "mock_text", "mock_image", "mock_video"}:
            return "mock"
        if provider and provider != "auto":
            if kind == "image" and provider == "gpt_image_2":
                return "openai"
            if kind == "video" and provider == "seedance":
                return "seedance_2_0"
            if kind == "video" and provider in {"xyq", "xiao_yunque"}:
                return "xyq_nest"
            if inferred and inferred != provider:
                return inferred
            return provider
        if inferred:
            return inferred
        return provider or self._default_provider(kind)

    def _frame_prompt(self, shot: Shot, frame_type: FrameType) -> str:
        prompts = shot.prompts or {}
        key = {
            FrameType.FIRST_FRAME: "first_frame_prompt",
            FrameType.LAST_FRAME: "last_frame_prompt",
            FrameType.KEYFRAME: "keyframe_prompt",
        }[frame_type]
        base_prompt = prompts.get(key) or prompts.get("keyframe_prompt") or shot.scene_description
        return self._apply_reference_image_notes(base_prompt, shot)

    def _image_size_for_shot(self, shot: Shot) -> str:
        project = self.projects.get_project(shot.project_id)
        aspect_ratio = ((project.workflow_config or {}).get("aspect_ratio") if project else None) or "16:9"
        return {"16:9": "1280*720", "9:16": "720*1280", "1:1": "1024*1024"}.get(aspect_ratio, settings.dashscope_image_size)

    def _video_duration(self, shot: Shot) -> float | None:
        prompts = shot.prompts or {}
        prompt_duration = prompts.get("duration_seconds")
        if prompt_duration:
            return self._normalize_duration(prompt_duration)
        project = self.projects.get_project(shot.project_id)
        if not project:
            return None
        video_config = (project.model_config or {}).get("video") or {}
        duration = video_config.get("duration_seconds") or (project.workflow_config or {}).get("duration_seconds")
        return self._normalize_duration(duration) if duration else None

    def _normalize_duration(self, duration) -> float | None:
        try:
            value = float(duration)
        except (TypeError, ValueError):
            return None
        if value <= 0:
            return None
        return min(value, 60.0)

    def _video_input_assets(self, shot: Shot) -> dict:
        first = self.assets.latest_for_shot(shot.id, AssetType.FIRST_FRAME)
        last = self.assets.latest_for_shot(shot.id, AssetType.LAST_FRAME)
        keyframes = self.assets.list_for_shot(shot.id, AssetType.KEYFRAME)
        prompts = shot.prompts or {}
        video_prompt = self._apply_reference_image_notes(prompts.get("video_prompt") or shot.scene_description, shot)
        if not video_prompt:
            raise WorkflowError("VIDEO_INPUT_INCOMPLETE", "缺少视频 Prompt 或场景描述")

        selected_tokens = prompts.get("selected_keyframe_tokens") or []
        selected = [asset for asset in keyframes if asset.feishu_file_token in selected_tokens] if selected_tokens else keyframes[:1]
        selected_keyframe_urls = [asset.public_url for asset in selected]
        if not selected_keyframe_urls:
            selected_keyframe_urls = [str(url) for url in (prompts.get("selected_keyframe_urls") or []) if url]
        reference_urls = prompts.get("reference_image_urls") or []
        reference_image_url = reference_urls[0] if reference_urls else None
        return {
            "prompt": video_prompt,
            "first_frame_url": first.public_url if first else None,
            "last_frame_url": last.public_url if last else None,
            "reference_image_url": reference_image_url,
            "keyframe_urls": selected_keyframe_urls,
            "first_frame_asset_id": str(first.id) if first else None,
            "last_frame_asset_id": str(last.id) if last else None,
            "keyframe_asset_ids": [str(asset.id) for asset in selected],
            "keyframe_time_seconds": self._normalize_keyframe_time(prompts.get("keyframe_time_seconds")),
        }

    def _normalize_keyframe_time(self, value) -> float | None:
        try:
            time_seconds = float(value)
        except (TypeError, ValueError):
            return None
        if time_seconds < 0:
            return None
        return time_seconds

    def _apply_reference_image_notes(self, base_prompt: str | None, shot: Shot) -> str:
        prompt = (base_prompt or "").strip()
        notes = self._reference_image_notes(shot)
        if not notes:
            return prompt
        guidance = (
            f"参考图使用说明：这张参考图仅用于镜头中的以下部分参考：{notes}。"
            "不要机械复刻整张图，只吸收与该说明相关的局部元素。"
        )
        if not prompt:
            return guidance
        return f"{prompt}\n\n{guidance}"

    def _reference_image_notes(self, shot: Shot) -> str:
        prompts = shot.prompts or {}
        notes = str(prompts.get("reference_image_notes") or "").strip()
        reference_urls = prompts.get("reference_image_urls") or []
        reference_tokens = prompts.get("reference_tokens") or []
        if not notes:
            return ""
        if not reference_urls and not reference_tokens:
            return ""
        return notes
