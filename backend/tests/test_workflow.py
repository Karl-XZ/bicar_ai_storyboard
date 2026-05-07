from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.domain.enums import AssetType, Satisfaction, ShotStatus
from app.domain.schemas import CreateProjectRequest, ShotCreate
from app.models import Asset, Base
from app.core.config import settings
from app.services.projects import ProjectService
from app.services.workflow import WorkflowService


def make_db():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    return session_factory()


def test_full_mock_workflow(monkeypatch):
    monkeypatch.setattr(settings, "default_text_provider", "mock")
    monkeypatch.setattr(settings, "default_image_provider", "mock")
    monkeypatch.setattr(settings, "default_video_provider", "mock")
    db = make_db()
    project = ProjectService(db).create_project(
        CreateProjectRequest(
            name="测试项目",
            default_text_provider="mock",
            default_text_model="mock-text-v1",
            default_image_provider="mock",
            default_image_model="mock-image-v1",
            default_video_provider="mock",
            default_video_model="mock-video-v1",
            initial_shots=[ShotCreate(shot_no="001", scene_description="雨夜街道奔跑")],
        )
    )
    shot = ProjectService(db).list_shots(project.id)[0]
    workflow = WorkflowService(db)

    prompt_job = workflow.optimize_prompt(shot.id)
    db.refresh(shot)
    assert prompt_job.status == "succeeded"
    assert shot.status == ShotStatus.PENDING_FRAMES.value
    assert shot.prompts["video_prompt"]

    frame_jobs = workflow.generate_frames(shot.id)
    db.refresh(shot)
    assert len(frame_jobs) == 5
    assert shot.status == ShotStatus.PENDING_REVIEW.value

    workflow.approve_shot(shot.id)
    db.refresh(shot)
    assert shot.status == ShotStatus.APPROVED.value

    video_job = workflow.generate_video(shot.id)
    db.refresh(shot)
    assert video_job.status == "succeeded"
    assert shot.status == ShotStatus.PENDING_ACCEPTANCE.value

    archive = workflow.archive_shot(shot.id, Satisfaction.SATISFIED)
    db.refresh(shot)
    assert archive.asset_type == AssetType.ARCHIVE.value
    assert shot.status == ShotStatus.ARCHIVED_SATISFIED.value
    assert db.query(Asset).count() == 7


def test_video_generation_can_run_from_text_only_and_is_idempotent(monkeypatch):
    monkeypatch.setattr(settings, "default_text_provider", "mock")
    monkeypatch.setattr(settings, "default_image_provider", "mock")
    monkeypatch.setattr(settings, "default_video_provider", "mock")
    db = make_db()
    project = ProjectService(db).create_project(
        CreateProjectRequest(
            name="纯文字视频项目",
            default_text_provider="mock",
            default_text_model="mock-text-v1",
            default_video_provider="mock",
            default_video_model="mock-video-v1",
            initial_shots=[ShotCreate(shot_no="001", scene_description="咖啡杯特写，热气上升")],
        )
    )
    shot = ProjectService(db).list_shots(project.id)[0]
    workflow = WorkflowService(db)

    workflow.optimize_prompt(shot.id)
    first_job = workflow.generate_video(shot.id)
    second_job = workflow.generate_video(shot.id)
    db.refresh(shot)

    assert first_job.id == second_job.id
    assert first_job.status == "succeeded"
    assert shot.status == ShotStatus.PENDING_ACCEPTANCE.value
    assert db.query(Asset).filter(Asset.asset_type == AssetType.VIDEO.value).count() == 1
