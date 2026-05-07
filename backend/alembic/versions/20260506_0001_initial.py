"""initial schema

Revision ID: 20260506_0001
Revises:
Create Date: 2026-05-06
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260506_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("feishu_app_token", sa.String(length=255), nullable=True),
        sa.Column("feishu_table_id", sa.String(length=255), nullable=True),
        sa.Column("feishu_folder_token", sa.String(length=255), nullable=True),
        sa.Column("model_config", sa.JSON(), nullable=False),
        sa.Column("workflow_config", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_projects")),
    )
    op.create_table(
        "shots",
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("feishu_record_id", sa.String(length=255), nullable=False),
        sa.Column("shot_no", sa.String(length=64), nullable=False),
        sa.Column("batch_no", sa.String(length=64), nullable=False),
        sa.Column("scene_description", sa.Text(), nullable=False),
        sa.Column("prompts", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("prompt_version", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name=op.f("fk_shots_project_id_projects")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_shots")),
    )
    op.create_index(op.f("ix_shots_feishu_record_id"), "shots", ["feishu_record_id"], unique=False)
    op.create_index(op.f("ix_shots_project_id"), "shots", ["project_id"], unique=False)
    op.create_table(
        "generation_jobs",
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("shot_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("job_type", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=128), nullable=True),
        sa.Column("model_id", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("prompt_version", sa.Integer(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("input_payload", sa.JSON(), nullable=False),
        sa.Column("output_payload", sa.JSON(), nullable=False),
        sa.Column("provider_task_id", sa.String(length=255), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name=op.f("fk_generation_jobs_project_id_projects")),
        sa.ForeignKeyConstraint(["shot_id"], ["shots.id"], name=op.f("fk_generation_jobs_shot_id_shots")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_generation_jobs")),
        sa.UniqueConstraint("idempotency_key", name="uq_generation_jobs_idempotency_key"),
    )
    op.create_index(op.f("ix_generation_jobs_project_id"), "generation_jobs", ["project_id"], unique=False)
    op.create_index(op.f("ix_generation_jobs_provider_task_id"), "generation_jobs", ["provider_task_id"], unique=False)
    op.create_index(op.f("ix_generation_jobs_shot_id"), "generation_jobs", ["shot_id"], unique=False)
    op.create_table(
        "assets",
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("shot_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("asset_type", sa.String(length=64), nullable=False),
        sa.Column("storage_uri", sa.String(length=1024), nullable=False),
        sa.Column("public_url", sa.String(length=2048), nullable=True),
        sa.Column("feishu_file_token", sa.String(length=255), nullable=True),
        sa.Column("provider", sa.String(length=128), nullable=True),
        sa.Column("model_id", sa.String(length=255), nullable=True),
        sa.Column("prompt_hash", sa.String(length=128), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name=op.f("fk_assets_project_id_projects")),
        sa.ForeignKeyConstraint(["shot_id"], ["shots.id"], name=op.f("fk_assets_shot_id_shots")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_assets")),
    )
    op.create_index(op.f("ix_assets_project_id"), "assets", ["project_id"], unique=False)
    op.create_index(op.f("ix_assets_prompt_hash"), "assets", ["prompt_hash"], unique=False)
    op.create_index(op.f("ix_assets_shot_id"), "assets", ["shot_id"], unique=False)
    op.create_table(
        "audit_logs",
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("shot_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("actor_open_id", sa.String(length=255), nullable=True),
        sa.Column("action", sa.String(length=128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_logs")),
    )
    op.create_index(op.f("ix_audit_logs_project_id"), "audit_logs", ["project_id"], unique=False)
    op.create_index(op.f("ix_audit_logs_shot_id"), "audit_logs", ["shot_id"], unique=False)
    op.create_table(
        "cost_logs",
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("shot_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("provider", sa.String(length=128), nullable=False),
        sa.Column("model_id", sa.String(length=255), nullable=False),
        sa.Column("usage", sa.JSON(), nullable=False),
        sa.Column("estimated_cost", sa.JSON(), nullable=False),
        sa.Column("actual_cost", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cost_logs")),
    )
    op.create_index(op.f("ix_cost_logs_project_id"), "cost_logs", ["project_id"], unique=False)
    op.create_index(op.f("ix_cost_logs_shot_id"), "cost_logs", ["shot_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_cost_logs_shot_id"), table_name="cost_logs")
    op.drop_index(op.f("ix_cost_logs_project_id"), table_name="cost_logs")
    op.drop_table("cost_logs")
    op.drop_index(op.f("ix_audit_logs_shot_id"), table_name="audit_logs")
    op.drop_index(op.f("ix_audit_logs_project_id"), table_name="audit_logs")
    op.drop_table("audit_logs")
    op.drop_index(op.f("ix_assets_shot_id"), table_name="assets")
    op.drop_index(op.f("ix_assets_prompt_hash"), table_name="assets")
    op.drop_index(op.f("ix_assets_project_id"), table_name="assets")
    op.drop_table("assets")
    op.drop_index(op.f("ix_generation_jobs_shot_id"), table_name="generation_jobs")
    op.drop_index(op.f("ix_generation_jobs_provider_task_id"), table_name="generation_jobs")
    op.drop_index(op.f("ix_generation_jobs_project_id"), table_name="generation_jobs")
    op.drop_table("generation_jobs")
    op.drop_index(op.f("ix_shots_project_id"), table_name="shots")
    op.drop_index(op.f("ix_shots_feishu_record_id"), table_name="shots")
    op.drop_table("shots")
    op.drop_table("projects")

