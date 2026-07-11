from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routes import tools
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


def test_debug_paper_copy_form_and_submit(monkeypatch):
    calls = []

    def fake_start_job(title):
        calls.append(title)
        return "job_abc"

    monkeypatch.setattr(settings, "app_public_base_url", "http://testserver")
    monkeypatch.setattr(tools, "_start_debug_paper_job", fake_start_job)
    monkeypatch.setattr(tools, "_read_job_state", lambda job_id: {"status": "running", "title": "客户A调试纸"})
    client = TestClient(app)

    response = client.get("/tools/debug-paper-copy")
    assert response.status_code == 200
    assert "副本文件名" in response.text

    response = client.post("/tools/debug-paper-copy", data={"name": ""})
    assert response.status_code == 400
    assert "请先填写副本文件名" in response.text

    response = client.post("/tools/debug-paper-copy", data={"name": "客户A调试纸"})
    assert response.status_code == 200
    assert "正在创建副本" in response.text
    assert "/tools/debug-paper-copy/status/job_abc" in response.text
    assert calls == ["客户A调试纸"]

    monkeypatch.setattr(
        tools,
        "_read_job_state",
        lambda job_id: {
            "status": "done",
            "title": "客户A调试纸",
            "url": "https://feishu.test/file/file_abc",
            "folder_token": "folder_abc",
        },
    )
    response = client.get("/tools/debug-paper-copy/status/job_abc")
    assert response.status_code == 200
    assert "https://feishu.test/file/file_abc" in response.text


def test_debug_paper_copy_qr_svg(monkeypatch):
    monkeypatch.setattr(settings, "app_public_base_url", "http://testserver")
    client = TestClient(app)

    response = client.get("/tools/debug-paper-copy/qr.svg")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    assert b"<svg" in response.content
