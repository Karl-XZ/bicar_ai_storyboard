from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.domain.enums import AssetType, FrameType, Satisfaction, ShotStatus
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


def test_reference_image_notes_are_appended_to_frame_and_video_prompts(monkeypatch):
    monkeypatch.setattr(settings, "default_text_provider", "mock")
    monkeypatch.setattr(settings, "default_image_provider", "mock")
    monkeypatch.setattr(settings, "default_video_provider", "mock")
    db = make_db()
    project = ProjectService(db).create_project(
        CreateProjectRequest(
            name="参考图批注项目",
            default_text_provider="mock",
            default_text_model="mock-text-v1",
            default_image_provider="mock",
            default_image_model="mock-image-v1",
            default_video_provider="mock",
            default_video_model="mock-video-v1",
            initial_shots=[ShotCreate(shot_no="001", scene_description="老爷车停在石板路上")],
        )
    )
    shot = ProjectService(db).list_shots(project.id)[0]
    shot.prompts = {
        "keyframe_prompt": "生成一张老爷车侧面构图",
        "video_prompt": "镜头从车头缓慢移动到车尾",
        "reference_image_urls": ["feishu://ref_tok_001"],
        "reference_image_notes": "只参考车身侧面线条和轮毂造型，不参考背景和色调",
    }
    db.commit()

    workflow = WorkflowService(db)
    frame_prompt = workflow._frame_prompt(shot, FrameType.KEYFRAME)
    video_inputs = workflow._video_input_assets(shot)

    assert "参考图使用说明" in frame_prompt
    assert "只参考车身侧面线条和轮毂造型，不参考背景和色调" in frame_prompt
    assert "不要机械复刻整张图" in frame_prompt
    assert "参考图使用说明" in video_inputs["prompt"]
    assert "只参考车身侧面线条和轮毂造型，不参考背景和色调" in video_inputs["prompt"]


def test_video_payload_includes_duration_and_manual_keyframe_time(monkeypatch):
    monkeypatch.setattr(settings, "default_text_provider", "mock")
    monkeypatch.setattr(settings, "default_image_provider", "mock")
    monkeypatch.setattr(settings, "default_video_provider", "mock")
    db = make_db()
    project = ProjectService(db).create_project(
        CreateProjectRequest(
            name="关键帧时间项目",
            default_text_provider="mock",
            default_text_model="mock-text-v1",
            default_video_provider="mock",
            default_video_model="mock-video-v1",
            initial_shots=[ShotCreate(shot_no="001", scene_description="车身侧面从暗处驶入")],
        )
    )
    shot = ProjectService(db).list_shots(project.id)[0]
    shot.prompts = {
        "video_prompt": "车身侧面从暗处驶入，镜头低机位跟拍",
        "duration_seconds": 3.5,
        "selected_keyframe_urls": ["feishu://manual_keyframe_token"],
        "keyframe_time_seconds": 2.5,
    }
    db.commit()

    workflow = WorkflowService(db)
    video_inputs = workflow._video_input_assets(shot)
    job = workflow.generate_video(shot.id)

    assert workflow._video_duration(shot) == 3.5
    assert video_inputs["keyframe_urls"] == ["feishu://manual_keyframe_token"]
    assert video_inputs["keyframe_time_seconds"] == 2.5
    assert job.input_payload["duration_seconds"] == 3.5
    assert job.input_payload["keyframe_time_seconds"] == 2.5
    assert job.input_payload["keyframe_urls"] == ["feishu://manual_keyframe_token"]
