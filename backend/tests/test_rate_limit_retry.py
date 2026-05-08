from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.domain.enums import ShotStatus
from app.domain.schemas import CreateProjectRequest, ShotCreate
from app.models import Base
from app.providers.base import ImageGenerationResult, PromptOptimizationResult, VideoTaskResult
from app.services.projects import ProjectService
from app.services.workflow import WorkflowError, WorkflowService


def make_db():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


class RateLimitedTextProvider:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    async def optimize_prompt(self, payload: dict) -> PromptOptimizationResult:
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("429 Too Many Requests from deepseek")
        return PromptOptimizationResult(
            keyframe_prompt="关键帧",
            first_frame_prompt="首帧",
            last_frame_prompt="尾帧",
            video_prompt="视频",
            negative_prompt="无",
            camera_motion="推近",
            consistency_notes="保持一致",
            style_tags=["cinematic"],
        )


class RateLimitedImageProvider:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    async def generate_image(self, payload: dict) -> ImageGenerationResult:
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("429 Too Many Requests from image provider")
        return ImageGenerationResult(bytes_data=b"png-bytes", mime_type="image/png", provider_asset_id="img-1")


class RateLimitedVideoProvider:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.create_calls = 0

    async def create_video_task(self, payload: dict) -> VideoTaskResult:
        self.create_calls += 1
        if self.create_calls <= self.failures:
            raise RuntimeError("429 Too Many Requests from XYQ")
        return VideoTaskResult(provider_task_id="video-task-1")

    async def poll_video_task(self, provider_task_id: str) -> dict:
        return {
            "status": "succeeded",
            "provider_task_id": provider_task_id,
            "video_bytes": b"video-bytes",
            "mime_type": "video/mp4",
        }


def test_prompt_rate_limit_retries_then_fails(monkeypatch):
    db = make_db()
    project = ProjectService(db).create_project(
        CreateProjectRequest(
            name="文本限流项目",
            default_text_provider="deepseek",
            default_text_model="deepseek-v4-pro",
            initial_shots=[ShotCreate(shot_no="001", scene_description="海边清晨")],
        )
    )
    shot = ProjectService(db).list_shots(project.id)[0]
    workflow = WorkflowService(db)
    provider = RateLimitedTextProvider(failures=4)
    logs: list[str] = []
    sleeps: list[int] = []

    async def fake_sleep(seconds: int) -> None:
        sleeps.append(seconds)

    async def fake_sync(target_shot) -> None:
        logs.append(target_shot.error_message or "")
        db.commit()

    monkeypatch.setattr(workflow.provider_router, "text", lambda provider_name=None: provider)
    monkeypatch.setattr("app.services.workflow.asyncio.sleep", fake_sleep)
    monkeypatch.setattr(workflow, "_sync_shot_error_log", fake_sync)

    try:
        workflow.optimize_prompt(shot.id)
        assert False, "expected WorkflowError"
    except WorkflowError as exc:
        assert exc.code == "PROMPT_RATE_LIMIT_RETRY_EXHAUSTED"
        assert "3次回退全部失败" in exc.message

    db.refresh(shot)
    assert sleeps == [60, 300, 1200]
    assert len(logs) == 4
    assert "第1次回退重试" in logs[0]
    assert "1 分钟" in logs[0]
    assert "第2次回退重试" in logs[1]
    assert "5 分钟" in logs[1]
    assert "第3次回退重试" in logs[2]
    assert "20 分钟" in logs[2]
    assert "3次回退全部失败" in logs[3]
    assert shot.error_code == "PROMPT_RATE_LIMIT_RETRY_EXHAUSTED"


def test_image_rate_limit_retries_then_succeeds(monkeypatch):
    db = make_db()
    project = ProjectService(db).create_project(
        CreateProjectRequest(
            name="图片限流项目",
            default_text_provider="mock",
            default_text_model="mock-text-v1",
            default_image_provider="dashscope",
            default_image_model="wanx-v1",
            initial_shots=[ShotCreate(shot_no="001", scene_description="城市夜景")],
        )
    )
    shot = ProjectService(db).list_shots(project.id)[0]
    workflow = WorkflowService(db)
    workflow.optimize_prompt(shot.id)
    provider = RateLimitedImageProvider(failures=1)
    logs: list[str] = []
    sleeps: list[int] = []

    async def fake_sleep(seconds: int) -> None:
        sleeps.append(seconds)

    async def fake_sync(target_shot) -> None:
        logs.append(target_shot.error_message or "")
        db.commit()

    monkeypatch.setattr(workflow.provider_router, "image", lambda provider_name=None: provider)
    monkeypatch.setattr("app.services.workflow.asyncio.sleep", fake_sleep)
    monkeypatch.setattr(workflow, "_sync_shot_error_log", fake_sync)

    jobs = workflow.generate_frames(shot.id)

    db.refresh(shot)
    assert len(jobs) == 5
    assert sleeps == [60]
    assert logs and "第1次回退重试" in logs[0]
    assert shot.status == ShotStatus.PENDING_REVIEW.value
    assert shot.error_code is None
    assert shot.error_message is None


def test_video_rate_limit_retries_then_succeeds(monkeypatch):
    db = make_db()
    project = ProjectService(db).create_project(
        CreateProjectRequest(
            name="视频限流项目",
            default_text_provider="mock",
            default_text_model="mock-text-v1",
            default_video_provider="xyq_nest",
            default_video_model="xyq_nest_video",
            initial_shots=[ShotCreate(shot_no="001", scene_description="追车镜头")],
        )
    )
    shot = ProjectService(db).list_shots(project.id)[0]
    workflow = WorkflowService(db)
    workflow.optimize_prompt(shot.id)
    provider = RateLimitedVideoProvider(failures=1)
    logs: list[str] = []
    sleeps: list[int] = []

    async def fake_sleep(seconds: int) -> None:
        sleeps.append(seconds)

    async def fake_sync(target_shot) -> None:
        logs.append(target_shot.error_message or "")
        db.commit()

    monkeypatch.setattr(workflow.provider_router, "video", lambda provider_name=None: provider)
    monkeypatch.setattr("app.services.workflow.asyncio.sleep", fake_sleep)
    monkeypatch.setattr(workflow, "_sync_shot_error_log", fake_sync)

    job = workflow.generate_video(shot.id)

    db.refresh(shot)
    assert job.status == "succeeded"
    assert sleeps == [60]
    assert logs and "xyq_nest/xyq_nest_video" in logs[0]
    assert "第1次回退重试" in logs[0]
    assert shot.status == ShotStatus.PENDING_ACCEPTANCE.value
    assert shot.error_code is None
    assert shot.error_message is None
