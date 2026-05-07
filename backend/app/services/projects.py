from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.enums import ShotStatus
from app.domain.schemas import CreateProjectRequest
from app.models.project import Project
from app.models.shot import Shot


class ProjectService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create_project(self, payload: CreateProjectRequest) -> Project:
        project = Project(
            name=payload.name,
            model_config={
                "text": {"provider": payload.default_text_provider, "model_id": payload.default_text_model},
                "image": {"provider": payload.default_image_provider, "model_id": payload.default_image_model},
                "video": {
                    "provider": payload.default_video_provider,
                    "model_id": payload.default_video_model,
                    "duration_seconds": payload.duration_seconds,
                },
            },
            workflow_config={
                "aspect_ratio": payload.aspect_ratio,
                "duration_seconds": payload.duration_seconds,
                "transition_alignment_enabled": payload.transition_alignment_enabled,
                "keyframe_generation_enabled": payload.keyframe_generation_enabled,
            },
        )
        self.db.add(project)
        self.db.flush()

        for shot_input in payload.initial_shots:
            prompts = {
                "keyframe_prompt": shot_input.keyframe_prompt,
                "first_frame_prompt": shot_input.first_frame_prompt,
                "last_frame_prompt": shot_input.last_frame_prompt,
                "video_prompt": shot_input.video_prompt,
                "negative_prompt": shot_input.negative_prompt,
            }
            status = ShotStatus.PENDING_FRAMES.value if any(prompts.values()) else ShotStatus.PENDING_PROMPT.value
            self.db.add(
                Shot(
                    project_id=project.id,
                    feishu_record_id=f"local_{project.id}_{shot_input.shot_no}",
                    shot_no=shot_input.shot_no,
                    batch_no=shot_input.batch_no,
                    scene_description=shot_input.scene_description,
                    prompts=prompts,
                    status=status,
                    prompt_version=1,
                )
            )
        self.db.commit()
        self.db.refresh(project)
        return project

    def get_project(self, project_id: UUID) -> Project | None:
        return self.db.get(Project, project_id)

    def find_by_feishu_table(self, app_token: str, table_id: str | None = None) -> Project | None:
        statement = select(Project).where(Project.feishu_app_token == app_token)
        if table_id:
            statement = statement.where(Project.feishu_table_id == table_id)
        return self.db.scalar(statement)

    def latest_for_chat(self, chat_id: str | None = None) -> Project | None:
        statement = select(Project).where(Project.feishu_app_token.is_not(None)).order_by(Project.created_at.desc())
        projects = list(self.db.scalars(statement))
        if chat_id:
            for project in projects:
                if (project.workflow_config or {}).get("chat_id") == chat_id:
                    return project
            return None
        return projects[0] if projects else None

    def update_feishu_resources(
        self,
        project: Project,
        *,
        app_token: str | None = None,
        table_id: str | None = None,
        folder_token: str | None = None,
        workflow_config: dict | None = None,
    ) -> Project:
        if app_token is not None:
            project.feishu_app_token = app_token
        if table_id is not None:
            project.feishu_table_id = table_id
        if folder_token is not None:
            project.feishu_folder_token = folder_token
        if workflow_config is not None:
            project.workflow_config = {**(project.workflow_config or {}), **workflow_config}
        self.db.commit()
        self.db.refresh(project)
        return project

    def list_shots(self, project_id: UUID, batch_no: str | None = None) -> list[Shot]:
        statement = select(Shot).where(Shot.project_id == project_id)
        if batch_no:
            statement = statement.where(Shot.batch_no == batch_no)
        return list(self.db.scalars(statement.order_by(Shot.shot_no.asc())))
