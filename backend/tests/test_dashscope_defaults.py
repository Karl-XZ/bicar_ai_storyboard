from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.domain.schemas import CreateProjectRequest, ShotCreate
from app.models import Base
from app.services.projects import ProjectService
from app.services.workflow import WorkflowService


def make_db():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_dashscope_is_default_model_selection(monkeypatch):
    monkeypatch.setattr(settings, "default_text_provider", "dashscope")
    monkeypatch.setattr(settings, "default_image_provider", "dashscope")
    monkeypatch.setattr(settings, "default_video_provider", "dashscope")
    monkeypatch.setattr(settings, "dashscope_text_model", "qwen-plus")
    monkeypatch.setattr(settings, "dashscope_image_model", "wanx2.1-t2i-turbo")
    monkeypatch.setattr(settings, "dashscope_video_model", "wan2.2-kf2v-flash")

    db = make_db()
    project = ProjectService(db).create_project(
        CreateProjectRequest(name="DashScope 默认项目", initial_shots=[ShotCreate(shot_no="001", scene_description="咖啡特写")])
    )
    shot = ProjectService(db).list_shots(project.id)[0]
    workflow = WorkflowService(db)

    assert project.model_config["text"] == {"provider": "dashscope", "model_id": "qwen-plus"}
    assert project.model_config["image"] == {"provider": "dashscope", "model_id": "wanx2.1-t2i-turbo"}
    assert project.model_config["video"]["provider"] == "dashscope"
    assert project.model_config["video"]["model_id"] == "wan2.2-kf2v-flash"
    assert workflow._provider_model(shot, "text") == ("dashscope", "qwen-plus")
    assert workflow._provider_model(shot, "image") == ("dashscope", "wanx2.1-t2i-turbo")
    assert workflow._provider_model(shot, "video") == ("dashscope", "wan2.2-kf2v-flash")
