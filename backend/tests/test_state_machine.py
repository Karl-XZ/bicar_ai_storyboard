from app.domain.enums import ShotStatus
from app.services.state_machine import StateMachineService


def test_state_machine_rejects_archived_mutation():
    service = StateMachineService()
    assert service.can_transition(ShotStatus.PENDING_REVIEW, ShotStatus.APPROVED)
    assert not service.can_transition(ShotStatus.ARCHIVED_SATISFIED, ShotStatus.VIDEO_QUEUED)

