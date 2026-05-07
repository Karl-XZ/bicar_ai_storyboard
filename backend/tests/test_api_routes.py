from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.session import get_db
from app.main import app
from app.models import Base
from app.core.config import settings
from app.services.projects import ProjectService


def test_api_create_project_and_generate_batch(monkeypatch):
    monkeypatch.setattr(settings, "default_text_provider", "mock")
    monkeypatch.setattr(settings, "default_image_provider", "mock")
    monkeypatch.setattr(settings, "default_video_provider", "mock")
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)

    def override_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)
    response = client.post(
        "/api/projects",
        json={
            "name": "API 测试项目",
            "default_text_provider": "mock",
            "default_text_model": "mock-text-v1",
            "default_image_provider": "mock",
            "default_image_model": "mock-image-v1",
            "default_video_provider": "mock",
            "default_video_model": "mock-video-v1",
            "initial_shots": [{"shot_no": "001", "scene_description": "清晨城市天际线"}],
        },
    )
    assert response.status_code == 202
    project_id = response.json()["data"]["project_id"]

    response = client.post(f"/api/projects/{project_id}/generate-current-batch", json={"batch_no": "batch_001"})
    assert response.status_code == 202
    assert len(response.json()["data"]["job_ids"]) == 5

    db = session_factory()
    try:
        shots = ProjectService(db).list_shots(project_id)
        assert shots[0].status == "pending_review"
    finally:
        db.close()
        app.dependency_overrides.clear()
