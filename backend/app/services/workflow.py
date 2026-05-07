import asyncio
import hashlib
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.config import settings
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
                "duration_seconds": self._video_duration(shot),
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
        result = await provider.optimize_prompt(
            {
                "scene_description": shot.scene_description,
                "project_style": "cinematic realistic",
                "model": job.model_id,
            }
        )
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
            result = await provider.generate_image(job.input_payload)
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
        self.jobs.mark_succeeded(job, {"asset_id": str(asset.id), "url": asset.public_url})

    async def _run_video_job(self, job: GenerationJob, shot: Shot) -> None:
        self.jobs.mark_running(job)
        provider = self.provider_router.video(job.provider)
        try:
            task = await provider.create_video_task(job.input_payload)
            job.provider_task_id = task.provider_task_id
            result = await provider.poll_video_task(task.provider_task_id)
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
        self.jobs.mark_succeeded(job, {"asset_id": str(asset.id), "url": asset.public_url})

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
                "nano_banana_2": settings.nano_banana_model,
                "openrouter": settings.openrouter_image_model,
            }.get(selected, settings.dashscope_image_model)
        return {
            "dashscope": settings.dashscope_video_model,
            "seedance_2_0": settings.seedance_model_id or settings.dashscope_video_model,
            "xyq_nest": settings.xyq_video_model,
        }.get(selected, settings.dashscope_video_model)

    def _infer_provider(self, *, kind: str, provider: str, model_id: str) -> str:
        normalized = model_id.lower().replace("-", "_").replace("/", "_")
        inferred = None
        if normalized in {"mock", "mock_text", "mock_image", "mock_video"}:
            inferred = "mock"
        elif normalized in {"nano_banana_2", "gemini_3.1_flash_image_preview"}:
            inferred = "nano_banana_2" if settings.google_api_key else "openrouter"
        elif normalized.startswith("deepseek_v4") or normalized.startswith("deepseek_chat") or normalized.startswith("deepseek_reasoner"):
            inferred = "deepseek"
        elif normalized in {"gpt_image_2", "gpt_image_1"}:
            inferred = "openai"
        elif normalized.startswith(("qwen", "wan", "z_image")):
            inferred = "dashscope"
        elif normalized.startswith("seedance"):
            inferred = "seedance_2_0"
        elif normalized.startswith(("xyq", "xiaoyunque", "xiao_yunque")):
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
        return prompts.get(key) or prompts.get("keyframe_prompt") or shot.scene_description

    def _image_size_for_shot(self, shot: Shot) -> str:
        project = self.projects.get_project(shot.project_id)
        aspect_ratio = ((project.workflow_config or {}).get("aspect_ratio") if project else None) or "16:9"
        return {"16:9": "1280*720", "9:16": "720*1280", "1:1": "1024*1024"}.get(aspect_ratio, settings.dashscope_image_size)

    def _video_duration(self, shot: Shot) -> int | None:
        project = self.projects.get_project(shot.project_id)
        if not project:
            return None
        video_config = (project.model_config or {}).get("video") or {}
        duration = video_config.get("duration_seconds") or (project.workflow_config or {}).get("duration_seconds")
        return int(duration) if duration else None

    def _video_input_assets(self, shot: Shot) -> dict:
        first = self.assets.latest_for_shot(shot.id, AssetType.FIRST_FRAME)
        last = self.assets.latest_for_shot(shot.id, AssetType.LAST_FRAME)
        keyframes = self.assets.list_for_shot(shot.id, AssetType.KEYFRAME)
        prompts = shot.prompts or {}
        video_prompt = prompts.get("video_prompt") or shot.scene_description
        if not video_prompt:
            raise WorkflowError("VIDEO_INPUT_INCOMPLETE", "缺少视频 Prompt 或场景描述")

        selected_tokens = prompts.get("selected_keyframe_tokens") or []
        selected = [asset for asset in keyframes if asset.feishu_file_token in selected_tokens] if selected_tokens else keyframes[:1]
        reference_urls = prompts.get("reference_image_urls") or []
        reference_image_url = reference_urls[0] if reference_urls else None
        return {
            "prompt": video_prompt,
            "first_frame_url": first.public_url if first else None,
            "last_frame_url": last.public_url if last else None,
            "reference_image_url": reference_image_url,
            "keyframe_urls": [asset.public_url for asset in selected],
            "first_frame_asset_id": str(first.id) if first else None,
            "last_frame_asset_id": str(last.id) if last else None,
            "keyframe_asset_ids": [str(asset.id) for asset in selected],
        }
