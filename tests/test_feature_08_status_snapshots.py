from uuid import uuid4

from specops_workflow.enums import (
    ApprovalScope, ConditionKind, JiraStatus, ObservationKind, PolicyState, Selector, System,
)
from specops_workflow.models import (
    ApproveStatusPolicyCommand, CreateStatusPolicyCommand, JiraIdentity, NativeStatusMapping, NominalSuccess,
    RecordOperationResultCommand, RemoteObservation, StartExternalOperationCommand, StatusCondition, StatusPolicyPayload,
    StatusRule, SubmitRemoteSnapshotCommand,
)
from specops_workflow.enums import ResultOutcome, FindingCategory

from test_feature_07_operations import operation_fixture


def confirmed_binding_and_policy():
    service, case_id, revision, intent, generation = operation_fixture(); state = service.get_case_state(case_id)
    started = service.start_external_operation(StartExternalOperationCommand(command_id=uuid4(), case_id=case_id, acting_actor_id="SYSTEM", expected_case_revision=revision, operation_id=uuid4(), intent_id=intent.intent_id, idempotency_key="status-fixture"))
    found = RemoteObservation(observation_kind=ObservationKind.FOUND, system=System.JIRA, external_identity=JiraIdentity(key="OPS-1"), generation_key=generation, package_binding=intent.package_binding, plan_binding=intent.plan_binding, status_policy_binding=intent.status_policy_binding, remote_revision="r1", native_status="To Do", owned_content=intent.request)
    confirmed = service.record_operation_result(RecordOperationResultCommand(command_id=uuid4(), case_id=case_id, acting_actor_id="SYSTEM", expected_case_revision=started.receipt.revision, operation_id=started.operation_id, attempt=1, outcome=NominalSuccess(outcome=ResultOutcome.NOMINAL_SUCCESS, read_back=found)))
    payload = StatusPolicyPayload(
        mappings=[NativeStatusMapping(mapping_id=uuid4(), system=System.JIRA, native_status="To Do", normalized_status=JiraStatus.TODO), NativeStatusMapping(mapping_id=uuid4(), system=System.JIRA, native_status="Cancelled", normalized_status=JiraStatus.CANCELLED)],
        rules=[StatusRule(rule_id=uuid4(), target_system=System.JIRA, selector=Selector.ALL_PROJECTED_ITEMS, target_normalized_status=JiraStatus.TODO, all_of=[StatusCondition(kind=ConditionKind.PACKAGE_CURRENT_APPROVED)])],
    )
    policy_id = uuid4()
    created = service.create_status_policy(CreateStatusPolicyCommand(command_id=uuid4(), case_id=case_id, acting_actor_id=state.pm_actor_id, expected_case_revision=confirmed.receipt.revision, policy_id=policy_id, content_schema_version=1, hash_schema_version=1, payload=payload))
    business = service.approve_status_policy(ApproveStatusPolicyCommand(command_id=uuid4(), case_id=case_id, acting_actor_id=state.pm_actor_id, expected_case_revision=created.receipt.revision, expected_artifact_id=policy_id, expected_artifact_version=1, expected_artifact_hash=created.binding.semantic_hash, scope=ApprovalScope.BUSINESS))
    technical = service.approve_status_policy(ApproveStatusPolicyCommand(command_id=uuid4(), case_id=case_id, acting_actor_id=state.dev_lead_actor_id, expected_case_revision=business.receipt.revision, expected_artifact_id=policy_id, expected_artifact_version=1, expected_artifact_hash=created.binding.semantic_hash, scope=ApprovalScope.TECHNICAL))
    assert service.get_case_state(case_id).policy.current.state == PolicyState.APPROVED
    return service, case_id, technical.receipt.revision, intent, generation, created.binding


def test_snapshot_status_changes_do_not_create_content_drift():
    service, case_id, revision, intent, generation, policy = confirmed_binding_and_policy()
    observation = RemoteObservation(observation_kind=ObservationKind.FOUND, system=System.JIRA, external_identity=JiraIdentity(key="OPS-1"), generation_key=generation, package_binding=intent.package_binding, plan_binding=intent.plan_binding, status_policy_binding=policy, remote_revision="r2", expected_previous_remote_revision="r1", native_status="To Do", owned_content=intent.request)
    result = service.submit_remote_snapshot(SubmitRemoteSnapshotCommand(command_id=uuid4(), case_id=case_id, acting_actor_id="SYSTEM", expected_case_revision=revision, observation=observation))
    assert result.active_finding_ids == []
    assert all(item["category"] != FindingCategory.CONTENT_DRIFT for item in service.get_case_state(case_id).metadata.get("findings", {}).values())


def test_not_found_is_persisted_as_next_sequence_and_finding():
    service, case_id, revision, intent, generation, policy = confirmed_binding_and_policy()
    missing = RemoteObservation(observation_kind=ObservationKind.NOT_FOUND, system=System.JIRA, external_identity=JiraIdentity(key="OPS-1"), generation_key=generation, package_binding=intent.package_binding, plan_binding=intent.plan_binding, status_policy_binding=policy, expected_previous_remote_revision="r1")
    result = service.submit_remote_snapshot(SubmitRemoteSnapshotCommand(command_id=uuid4(), case_id=case_id, acting_actor_id="SYSTEM", expected_case_revision=revision, observation=missing))
    assert result.observation_sequence == 2
    assert len(result.active_finding_ids) == 1

