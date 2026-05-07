import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from app.domain.enums import Satisfaction  # noqa: E402
from app.domain.schemas import CreateProjectRequest, ShotCreate  # noqa: E402
from app.models import Base  # noqa: E402
from app.services.projects import ProjectService  # noqa: E402
from app.services.workflow import WorkflowService  # noqa: E402


def main() -> int:
    engine = create_engine("sqlite+pysqlite:///./local_demo.db")
    SessionLocal = sessionmaker(bind=engine)
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        project = ProjectService(db).create_project(
            CreateProjectRequest(
                name="本地演示项目",
                initial_shots=[
                    ShotCreate(shot_no="001", scene_description="城市天际线在晨曦中渐渐亮起"),
                    ShotCreate(shot_no="002", scene_description="年轻女主站在地铁站台上等待列车"),
                ],
            )
        )
        workflow = WorkflowService(db)
        jobs = workflow.generate_batch_frames(project.id, "batch_001")
        first_shot = ProjectService(db).list_shots(project.id)[0]
        workflow.approve_shot(first_shot.id)
        video_job = workflow.generate_video(first_shot.id)
        archive = workflow.archive_shot(first_shot.id, Satisfaction.SATISFIED)
        print(
            {
                "project_id": str(project.id),
                "frame_jobs": len(jobs),
                "video_job_id": str(video_job.id),
                "archive_asset_id": str(archive.id),
            }
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
