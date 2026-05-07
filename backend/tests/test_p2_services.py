from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.domain.enums import JobStatus, JobType
from app.models import Base
from app.models.job import GenerationJob
from app.models.project import Project
from app.services.costs import CostService
from app.services.permissions import PermissionService, ProjectMember
from app.services.recovery import RecoveryService


def test_cost_permission_and_recovery_services():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    project = Project(name="P2", model_config={}, workflow_config={})
    db.add(project)
    db.flush()
    cost = CostService(db).record(
        project_id=project.id,
        shot_id=None,
        provider="mock",
        model_id="mock",
        usage={"images": 1},
    )
    assert cost.usage["images"] == 1

    permissions = PermissionService()
    assert permissions.can_review(ProjectMember(open_id="ou_x", role="reviewer"))
    assert not permissions.can_manage_project(ProjectMember(open_id="ou_x", role="writer"))

    job = GenerationJob(
        project_id=project.id,
        job_type=JobType.PROMPT_OPTIMIZE.value,
        status=JobStatus.QUEUED.value,
        idempotency_key=str(uuid4()),
        input_payload={},
        output_payload={},
    )
    db.add(job)
    db.commit()
    RecoveryService(db).move_to_retrying(job.id)
    db.refresh(job)
    assert job.status == JobStatus.RETRYING.value
    assert job.retry_count == 1

