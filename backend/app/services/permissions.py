from dataclasses import dataclass


@dataclass(frozen=True)
class ProjectMember:
    open_id: str
    role: str


class PermissionService:
    def can_manage_project(self, member: ProjectMember) -> bool:
        return member.role in {"owner", "admin"}

    def can_edit_shots(self, member: ProjectMember) -> bool:
        return member.role in {"owner", "admin", "writer"}

    def can_review(self, member: ProjectMember) -> bool:
        return member.role in {"owner", "admin", "reviewer"}

    def can_archive(self, member: ProjectMember) -> bool:
        return member.role in {"owner", "admin", "producer"}

