from app.domain.enums import ShotStatus


ALLOWED_TRANSITIONS: dict[ShotStatus, set[ShotStatus]] = {
    ShotStatus.DRAFT: {ShotStatus.PENDING_PROMPT, ShotStatus.PENDING_FRAMES},
    ShotStatus.PENDING_PROMPT: {ShotStatus.PROMPT_OPTIMIZING},
    ShotStatus.PROMPT_OPTIMIZING: {ShotStatus.PENDING_FRAMES, ShotStatus.REJECTED},
    ShotStatus.PENDING_FRAMES: {ShotStatus.FRAMES_GENERATING},
    ShotStatus.FRAMES_GENERATING: {ShotStatus.PENDING_REVIEW, ShotStatus.FRAME_PARTIAL_FAILED},
    ShotStatus.FRAME_PARTIAL_FAILED: {ShotStatus.FRAMES_GENERATING, ShotStatus.REJECTED},
    ShotStatus.PENDING_REVIEW: {ShotStatus.APPROVED, ShotStatus.REJECTED},
    ShotStatus.REJECTED: {ShotStatus.PENDING_PROMPT, ShotStatus.PENDING_FRAMES},
    ShotStatus.APPROVED: {ShotStatus.VIDEO_QUEUED},
    ShotStatus.VIDEO_QUEUED: {ShotStatus.VIDEO_GENERATING},
    ShotStatus.VIDEO_GENERATING: {ShotStatus.PENDING_ACCEPTANCE, ShotStatus.VIDEO_FAILED},
    ShotStatus.VIDEO_FAILED: {ShotStatus.VIDEO_QUEUED, ShotStatus.REJECTED},
    ShotStatus.PENDING_ACCEPTANCE: {ShotStatus.ARCHIVED_SATISFIED, ShotStatus.ARCHIVED_UNSATISFIED},
    ShotStatus.ARCHIVED_SATISFIED: set(),
    ShotStatus.ARCHIVED_UNSATISFIED: set(),
}


class StateMachineService:
    def can_transition(self, current: ShotStatus, target: ShotStatus) -> bool:
        return target in ALLOWED_TRANSITIONS.get(current, set())

    def require_transition(self, current: ShotStatus, target: ShotStatus) -> None:
        if not self.can_transition(current, target):
            raise ValueError(f"invalid shot transition: {current} -> {target}")

