from datetime import datetime, timezone
from uuid import uuid4

import pytest

from specops_workflow.canonical import sha256
from specops_workflow.enums import Action, ArtifactKind, ObservationKind, OperationStatus, ResultOutcome, System
from specops_workflow.errors import DomainError, ErrorCode
from specops_workflow.models import (
    ArtifactBinding, CreateCaseCommand, ExplicitFailure, JiraIdentity, NominalSuccess, OperationIntent,
    RecordOperationResultCommand, RemoteObservation, StartExternalOperationCommand,
)
from specops_workflow.ports import FrozenClock
from specops_workflow.service import WorkflowService


def operation_fixture():
    service = WorkflowService(clock=FrozenClock(datetime(2026, 8, 7, 12, tzinfo=timezone.utc)))
    case_id, pm, dev = uuid4(), uuid4(), uuid4()
    created = service.create_case(CreateCaseCommand(command_id=uuid4(), case_id=case_id, acting_actor_id=pm, pm_actor_id=pm, dev_lead_actor_id=dev))
    item_id, plan_id, package_id, policy_id = uuid4(), uuid4(), uuid4(), uuid4()
    package = ArtifactBinding(artifact_kind=ArtifactKind.SPEC_PACKAGE, artifact_id=package_id, version=1, semantic_hash="0" * 64)
    plan = ArtifactBinding(artifact_kind=ArtifactKind.PROJECTION_PLAN, artifact_id=plan_id, version=1, semantic_hash="1" * 64)
    policy = ArtifactBinding(artifact_kind=ArtifactKind.STATUS_POLICY, artifact_id=policy_id, version=1, semantic_hash="2" * 64)
    generation = f"specops:{case_id}:jira:{item_id}"
    request = {"generation_key": generation, "summary": "Exact work"}
    intent = OperationIntent(intent_id=uuid4(), system=System.JIRA, item_ref={"kind": "CURRENT", "item_id": item_id, "plan_id": plan_id, "plan_version": 1, "plan_hash": "1" * 64}, action=Action.CREATE, request=request, package_binding=package, plan_binding=plan, status_policy_binding=policy, request_owned_content_hash=sha256(request), fingerprint=sha256({"intent": str(item_id)}))
    service.get_case_state(case_id).metadata["intents"] = {intent.intent_id: intent}
    return service, case_id, created.receipt.revision, intent, generation


def test_success_requires_matching_readback_and_confirms_identity_once():
    service, case_id, revision, intent, generation = operation_fixture()
    operation_id = uuid4()
    started = service.start_external_operation(StartExternalOperationCommand(command_id=uuid4(), case_id=case_id, acting_actor_id="SYSTEM", expected_case_revision=revision, operation_id=operation_id, intent_id=intent.intent_id, idempotency_key="stable-key"))
    observation = RemoteObservation(observation_kind=ObservationKind.FOUND, system=System.JIRA, external_identity=JiraIdentity(key="OPS-1"), generation_key=generation, package_binding=intent.package_binding, plan_binding=intent.plan_binding, status_policy_binding=intent.status_policy_binding, remote_revision="r1", native_status="To Do", owned_content=intent.request)
    result = service.record_operation_result(RecordOperationResultCommand(command_id=uuid4(), case_id=case_id, acting_actor_id="SYSTEM", expected_case_revision=started.receipt.revision, operation_id=operation_id, attempt=1, outcome=NominalSuccess(outcome=ResultOutcome.NOMINAL_SUCCESS, read_back=observation)))
    assert result.status == OperationStatus.SUCCEEDED
    assert result.confirmation.external_identity.key == "OPS-1"
    assert len(service.get_case_state(case_id).bindings) == 1


def test_failed_retry_and_blind_retry_rules():
    service, case_id, revision, intent, _ = operation_fixture(); operation_id = uuid4()
    started = service.start_external_operation(StartExternalOperationCommand(command_id=uuid4(), case_id=case_id, acting_actor_id="SYSTEM", expected_case_revision=revision, operation_id=operation_id, intent_id=intent.intent_id, idempotency_key="stable-key"))
    with pytest.raises(DomainError) as caught:
        service.start_external_operation(StartExternalOperationCommand(command_id=uuid4(), case_id=case_id, acting_actor_id="SYSTEM", expected_case_revision=started.receipt.revision, operation_id=operation_id, intent_id=intent.intent_id, idempotency_key="stable-key"))
    assert caught.value.code == ErrorCode.OPERATION_RETRY_BLOCKED
    failed = service.record_operation_result(RecordOperationResultCommand(command_id=uuid4(), case_id=case_id, acting_actor_id="SYSTEM", expected_case_revision=started.receipt.revision, operation_id=operation_id, attempt=1, outcome=ExplicitFailure(outcome=ResultOutcome.EXPLICIT_FAILURE, failure_code="transport-failed")))
    retried = service.start_external_operation(StartExternalOperationCommand(command_id=uuid4(), case_id=case_id, acting_actor_id="SYSTEM", expected_case_revision=failed.receipt.revision, operation_id=operation_id, intent_id=intent.intent_id, idempotency_key="stable-key"))
    assert retried.status == OperationStatus.PENDING and retried.attempt == 2

