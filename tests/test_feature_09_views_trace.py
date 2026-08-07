from uuid import uuid4

from specops_workflow.enums import FindingCategory, ObservationKind, SetupStage, System
from specops_workflow.models import JiraIdentity, QueryOne, RemoteObservation, SubmitRemoteSnapshotCommand, UUIDListQuery

from test_feature_05_jira_plan import approved_case
from test_feature_08_status_snapshots import confirmed_binding_and_policy


def test_workflow_stage_and_trace_ids_are_deterministic():
    service, case_id, pm, _, _, _, _ = approved_case()
    view = service.get_workflow_view(QueryOne(case_id=case_id, acting_actor_id=pm))
    assert view.setup_stage == SetupStage.JIRA_REVIEW
    first = service.get_traceability_map(QueryOne(case_id=case_id, acting_actor_id="SYSTEM"))
    second = service.get_traceability_map(QueryOne(case_id=case_id, acting_actor_id="SYSTEM"))
    assert first == second
    assert first.edges and len({edge.id for edge in first.edges}) == len(first.edges)


def test_missing_finding_resolves_with_later_found_evidence(tmp_path):
    harness, intent, generation, policy = confirmed_binding_and_policy(tmp_path)
    missing = RemoteObservation(observation_kind=ObservationKind.NOT_FOUND, system=System.JIRA, external_identity=JiraIdentity(key="OPS-1"), generation_key=generation, package_binding=intent.package_binding, plan_binding=intent.plan_binding, status_policy_binding=policy, expected_previous_remote_revision="revision-1")
    accepted = harness.service.submit_remote_snapshot(SubmitRemoteSnapshotCommand(command_id=uuid4(), case_id=harness.case_id, acting_actor_id="SYSTEM", expected_case_revision=harness.revision, observation=missing))
    found = RemoteObservation(observation_kind=ObservationKind.FOUND, system=System.JIRA, external_identity=JiraIdentity(key="OPS-1"), generation_key=generation, package_binding=intent.package_binding, plan_binding=intent.plan_binding, status_policy_binding=policy, remote_revision="r2", expected_previous_remote_revision="revision-1", native_status="To Do", owned_content=intent.request)
    harness.service.submit_remote_snapshot(SubmitRemoteSnapshotCommand(command_id=uuid4(), case_id=harness.case_id, acting_actor_id="SYSTEM", expected_case_revision=accepted.receipt.revision, observation=found))
    findings = harness.service.list_drift_findings(UUIDListQuery(case_id=harness.case_id, acting_actor_id="SYSTEM"))
    missing_rows = [item for item in findings.items if item.category == FindingCategory.MISSING_REMOTE_ITEM]
    assert len(missing_rows) == 1 and missing_rows[0].active is False and missing_rows[0].resolved_at is not None
