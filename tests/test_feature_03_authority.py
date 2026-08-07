from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from specops_workflow.enums import Domain
from specops_workflow.errors import DomainError, ErrorCode
from specops_workflow.models import AddParticipantCommand, CreateCaseCommand, GrantDelegationCommand, ReplayResult, RevokeDelegationCommand
from specops_workflow.service import WorkflowService


@dataclass
class MutableClock:
    value: datetime
    def now(self): return self.value


def created():
    clock = MutableClock(datetime(2026, 8, 7, 12, tzinfo=timezone.utc))
    service = WorkflowService(clock=clock)
    case_id, pm, dev = uuid4(), uuid4(), uuid4()
    result = service.create_case(CreateCaseCommand(command_id=uuid4(), case_id=case_id, acting_actor_id=pm, pm_actor_id=pm, dev_lead_actor_id=dev))
    return service, clock, case_id, pm, dev, result.receipt.revision


def test_authority_slots_and_system_boundary():
    service, _, case_id, pm, dev, revision = created()
    with pytest.raises(DomainError) as caught:
        service.add_participant(AddParticipantCommand(command_id=uuid4(), case_id=case_id, acting_actor_id="SYSTEM", expected_case_revision=revision, actor_id=uuid4()))
    assert caught.value.code == ErrorCode.SYSTEM_ACTION_FORBIDDEN
    with pytest.raises(DomainError) as caught:
        service.create_case(CreateCaseCommand(command_id=uuid4(), case_id=uuid4(), acting_actor_id=pm, pm_actor_id=pm, dev_lead_actor_id=pm))
    assert caught.value.code == ErrorCode.AUTHORITY_SLOT_OCCUPIED


def test_scoped_delegation_expiry_and_revocation():
    service, clock, case_id, pm, _, revision = created()
    delegate = uuid4()
    added = service.add_participant(AddParticipantCommand(command_id=uuid4(), case_id=case_id, acting_actor_id=pm, expected_case_revision=revision, actor_id=delegate))
    delegation_id = uuid4()
    granted = service.grant_delegation(GrantDelegationCommand(
        command_id=uuid4(), case_id=case_id, acting_actor_id=pm, expected_case_revision=added.receipt.revision,
        delegation_id=delegation_id, delegate_id=delegate, domain=Domain.BUSINESS,
        command_names=["record_ambiguity_finding"], valid_from=clock.value, valid_until=clock.value + timedelta(hours=1), later_review_required=True,
    ))
    assert granted.delegation_id == delegation_id
    revoked = service.revoke_delegation(RevokeDelegationCommand(command_id=uuid4(), case_id=case_id, acting_actor_id=pm, expected_case_revision=granted.receipt.revision, delegation_id=delegation_id))
    assert revoked.revoked_at == clock.value


def test_expected_revision_and_command_replay_are_deterministic():
    service, _, case_id, pm, _, revision = created()
    command = AddParticipantCommand(command_id=uuid4(), case_id=case_id, acting_actor_id=pm, expected_case_revision=revision, actor_id=uuid4())
    first = service.add_participant(command)
    replay = service.add_participant(command)
    assert isinstance(replay, ReplayResult)
    assert replay.result == first.model_dump(mode="json", exclude_none=False)
    changed = command.model_copy(update={"actor_id": uuid4()})
    with pytest.raises(DomainError) as caught: service.add_participant(changed)
    assert caught.value.code == ErrorCode.IDEMPOTENCY_CONFLICT
