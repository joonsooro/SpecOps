from uuid import uuid4

from specops_workflow.enums import FindingCategory, ObservationKind, System
from specops_workflow.models import (
    JiraIdentity,
    RemoteObservation,
    SubmitRemoteSnapshotCommand,
    UUIDListQuery,
)

from public_api_factory import fresh_harness


def confirmed_binding_and_policy(tmp_path):
    harness = fresh_harness(tmp_path)
    harness.build_approved_package()
    harness.build_approved_jira_plan()
    harness.build_approved_status_policy()
    intent = next(item for item in harness.pending_intents() if item.system == System.JIRA)
    harness.apply_intent(intent, 1)
    return harness, intent, intent.request["generation_key"], harness.status_binding


def test_snapshot_status_changes_do_not_create_content_drift(tmp_path):
    harness, intent, generation, policy = confirmed_binding_and_policy(tmp_path)
    observation = RemoteObservation(
        observation_kind=ObservationKind.FOUND,
        system=System.JIRA,
        external_identity=JiraIdentity(key="OPS-1"),
        generation_key=generation,
        package_binding=intent.package_binding,
        plan_binding=intent.plan_binding,
        status_policy_binding=policy,
        remote_revision="r2",
        expected_previous_remote_revision="revision-1",
        native_status="To Do",
        owned_content=intent.request,
    )
    result = harness.service.submit_remote_snapshot(
        SubmitRemoteSnapshotCommand(
            command_id=uuid4(),
            case_id=harness.case_id,
            acting_actor_id="SYSTEM",
            expected_case_revision=harness.revision,
            observation=observation,
        )
    )
    assert result.active_finding_ids == []
    findings = harness.service.list_drift_findings(
        UUIDListQuery(case_id=harness.case_id, acting_actor_id="SYSTEM")
    )
    assert all(item.category != FindingCategory.CONTENT_DRIFT for item in findings.items)


def test_not_found_is_persisted_as_next_sequence_and_finding(tmp_path):
    harness, intent, generation, policy = confirmed_binding_and_policy(tmp_path)
    missing = RemoteObservation(
        observation_kind=ObservationKind.NOT_FOUND,
        system=System.JIRA,
        external_identity=JiraIdentity(key="OPS-1"),
        generation_key=generation,
        package_binding=intent.package_binding,
        plan_binding=intent.plan_binding,
        status_policy_binding=policy,
        expected_previous_remote_revision="revision-1",
    )
    result = harness.service.submit_remote_snapshot(
        SubmitRemoteSnapshotCommand(
            command_id=uuid4(),
            case_id=harness.case_id,
            acting_actor_id="SYSTEM",
            expected_case_revision=harness.revision,
            observation=missing,
        )
    )
    assert result.observation_sequence == 2
    assert len(result.active_finding_ids) == 1
