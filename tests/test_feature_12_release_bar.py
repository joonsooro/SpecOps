from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import CheckConstraint, UniqueConstraint, inspect, text
from sqlalchemy.exc import DatabaseError

from specops_workflow import FrozenClock, WorkflowService
from specops_workflow.canonical import HashRecipe, artifact_hash, default_registry, sha256
from specops_workflow.enums import (
    Action,
    AmbiguityCategory,
    ApprovalScope,
    ArtifactKind,
    ConditionKind,
    Domain,
    FindingCategory,
    FindingStatus,
    JiraKind,
    JiraStatus,
    ObservationKind,
    OperationStatus,
    PackageState,
    PlanTarget,
    ResultOutcome,
    Selector,
    SetupStage,
    Severity,
    SourceArtifactType,
    System,
    WorkflowHealth,
)
from specops_workflow.errors import DomainError, ErrorCode
from specops_workflow.models import (
    AddParticipantCommand,
    AcceptanceCheck,
    ApproveProjectionPlanCommand,
    ApproveSpecPackageCommand,
    ApproveStatusPolicyCommand,
    ArtifactBinding,
    AuditQuery,
    BodyAcceptanceCheck,
    CreateProjectionPlanCommand,
    CreateCaseCommand,
    ExplicitFailure,
    GrantDelegationCommand,
    JsonPointer,
    LineRange,
    MarkSpecPackageReadyCommand,
    NominalSuccess,
    ProjectionItem,
    ProjectionPlanPayload,
    QueryOne,
    RecordAmbiguityFindingCommand,
    RecordOperationResultCommand,
    ReconcileOperationCommand,
    RegisterSourceArtifactCommand,
    Requirement,
    RemoteObservation,
    ReplayResult,
    ResolveAmbiguityFindingCommand,
    RevokeDelegationCommand,
    ReviseProjectionPlanCommand,
    ReviseSpecPackageCommand,
    ReviseStatusPolicyCommand,
    SourceArtifactIdentity,
    SourceRef,
    StartExternalOperationCommand,
    StatusCondition,
    StatusPolicyPayload,
    StatusRule,
    StructuredWorkBody,
    SubmitRemoteSnapshotCommand,
    TechnicalDecision,
    UUIDListQuery,
    WorkbookRange,
)
from specops_workflow.persistence import engine_for, metadata

from public_api_factory import (
    FROZEN_NOW,
    DeterministicUuidSequence,
    fresh_harness,
)
from release_contract import (
    AUTHORIZATION_ROWS,
    COMMAND_MODELS,
    EV_IDS,
    MUTATION_RESULT_MODELS,
    OPERATION_BRANCHES,
    PUBLIC_SCHEMA_MODELS,
    READ_MODELS,
    release_manifest,
    schema_snapshot,
)


SNAPSHOT_DIR = Path(__file__).parent / "snapshots"


def _public_reads(service: WorkflowService, case_id):
    one = QueryOne(case_id=case_id, acting_actor_id="SYSTEM")
    uuids = UUIDListQuery(case_id=case_id, acting_actor_id="SYSTEM", limit=500)
    audit = AuditQuery(case_id=case_id, acting_actor_id="SYSTEM", limit=500)
    return (
        service.get_workflow_view(one),
        service.get_delivery_view(one),
        service.get_traceability_map(one),
        service.list_drift_findings(uuids),
        service.list_pending_operation_intents(uuids),
        service.list_audit_events(audit),
    )


def _confirmation(harness, system: System) -> RemoteObservation:
    for event in reversed(harness.audit_events()):
        if event.command_name != "record_operation_result":
            continue
        value = event.result.get("confirmation")
        if value and value.get("system") == system.value:
            return RemoteObservation.model_validate_json(json.dumps(value))
    raise AssertionError(f"no {system.value} confirmation found")


def _database_rows(database_url: str) -> dict[str, list[tuple]]:
    engine = engine_for(database_url)
    result: dict[str, list[tuple]] = {}
    with engine.connect() as connection:
        for name in sorted(metadata.tables):
            rows = [tuple(row) for row in connection.execute(text(f'SELECT * FROM "{name}"'))]
            result[name] = sorted(rows, key=repr)
    return result


def _found_for_intent(
    intent,
    *,
    external_identity=None,
    remote_revision: str = "matrix-revision",
    expected_previous_remote_revision: str | None = None,
    native_status: str = "To Do",
    owned_content=None,
    generation_key: str | None = None,
) -> RemoteObservation:
    return RemoteObservation(
        observation_kind=ObservationKind.FOUND,
        system=intent.system,
        external_identity=external_identity or {"key": "OPS-900"},
        generation_key=generation_key or intent.request["generation_key"],
        package_binding=intent.package_binding,
        plan_binding=intent.plan_binding,
        jira_plan_binding=intent.jira_plan_binding,
        status_policy_binding=intent.status_policy_binding,
        remote_revision=remote_revision,
        expected_previous_remote_revision=expected_previous_remote_revision,
        native_status=native_status,
        owned_content=intent.request if owned_content is None else owned_content,
    )


@pytest.mark.ev("EV-001")
@pytest.mark.ev("EV-011")
@pytest.mark.ev("EV-021")
@pytest.mark.ev("EV-023")
@pytest.mark.ev("EV-025")
@pytest.mark.ev("EV-026")
@pytest.mark.ev("EV-027")
@pytest.mark.ev("EV-028")
@pytest.mark.ev("EV-032")
@pytest.mark.ev("EV-035")
@pytest.mark.ev("EV-036")
@pytest.mark.ev("EV-040")
@pytest.mark.ev("EV-041")
def test_clean_public_workflow_reaches_linked_with_exact_audit(tmp_path):
    harness = fresh_harness(tmp_path).complete()
    workflow, delivery, trace, findings, intents, audit = _public_reads(
        harness.service, harness.case_id
    )

    assert workflow.setup_stage == SetupStage.LINKED
    assert workflow.foundation_ready is True
    assert workflow.workflow_health == WorkflowHealth.HEALTHY
    assert all(
        value is not None
        for value in (
            workflow.current_package,
            workflow.current_jira_plan,
            workflow.current_github_plan,
            workflow.current_status_policy,
        )
    )
    assert delivery.items and all(item.external_identity is not None for item in delivery.items)
    assert len({(item.system, str(item.external_identity)) for item in delivery.items}) == len(
        delivery.items
    )
    assert trace.complete is True and trace.edges
    assert len({edge.id for edge in trace.edges}) == len(trace.edges)
    assert findings.items == [] and intents.items == []
    assert len(audit.items) == len(harness.successful_command_ids) == workflow.revision
    assert [event.case_sequence for event in audit.items] == list(
        range(1, workflow.revision + 1)
    )
    assert {event.command_id for event in audit.items} == set(harness.successful_command_ids)
    operation_events = [event for event in audit.items if event.command_name == "start_external_operation"]
    assert len(operation_events) == len({event.result["operation_id"] for event in operation_events})
    assert {event.result["action"] for event in operation_events} == {Action.CREATE.value}
    assert {item.value for item in Action} == {
        "CREATE", "UPDATE", "RETIRE", "TRANSITION_STATUS"
    }
    assert all(item.pending_intent_ids == [] for item in delivery.items)


@pytest.mark.ev("EV-002")
def test_release_manifest_and_canonical_schema_snapshot_are_exact():
    manifest = json.loads((SNAPSHOT_DIR / "release_manifest.json").read_text())
    schemas = json.loads((SNAPSHOT_DIR / "public_schemas.json").read_text())

    assert manifest == release_manifest()
    assert schemas == schema_snapshot()
    assert tuple(manifest["ev_ids"]) == EV_IDS
    assert len(COMMAND_MODELS) == 21
    assert len(MUTATION_RESULT_MODELS) == 11
    assert len(READ_MODELS) == 6
    assert len(PUBLIC_SCHEMA_MODELS) == 39
    assert len(manifest["stable_failure_codes"]) == 22
    assert set(manifest["rejecting_fixtures"]) == set(manifest["stable_failure_codes"])
    assert tuple(manifest["authorization_counterparts"]) == AUTHORIZATION_ROWS
    assert tuple(manifest["operation_branches"]) == OPERATION_BRANCHES
    assert manifest["accepted_rejecting_fixtures"] == 0
    assert manifest["duplicate_operations"] == 0
    assert manifest["duplicate_bindings"] == 0
    for model in PUBLIC_SCHEMA_MODELS:
        with pytest.raises(ValidationError) as caught:
            model.model_validate({"__unexpected__": True})
        assert any(error["type"] == "extra_forbidden" for error in caught.value.errors())


@pytest.mark.ev("EV-002")
def test_all_22_stable_error_codes_have_executable_rejecting_fixtures(tmp_path):
    observed: set[ErrorCode] = set()

    def reject(code: ErrorCode, harness, call) -> None:
        before = len(harness.audit_events())
        with pytest.raises(DomainError) as caught:
            call()
        assert caught.value.code == code
        assert len(harness.audit_events()) == before
        observed.add(code)

    base = fresh_harness(tmp_path / "base")
    reject(
        ErrorCode.RECORD_NOT_FOUND,
        base,
        lambda: base.service.revoke_delegation(
            RevokeDelegationCommand(
                command_id=base.id(),
                case_id=base.case_id,
                acting_actor_id=base.pm_actor_id,
                expected_case_revision=base.revision,
                delegation_id=base.id(),
            )
        ),
    )
    reject(
        ErrorCode.SYSTEM_ACTION_FORBIDDEN,
        base,
        lambda: base.service.add_participant(
            AddParticipantCommand(
                command_id=base.id(),
                case_id=base.case_id,
                acting_actor_id="SYSTEM",
                expected_case_revision=base.revision,
                actor_id=base.id(),
            )
        ),
    )
    reject(
        ErrorCode.AUTHORITY_REQUIRED,
        base,
        lambda: base.service.add_participant(
            AddParticipantCommand(
                command_id=base.id(),
                case_id=base.case_id,
                acting_actor_id=base.id(),
                expected_case_revision=base.revision,
                actor_id=base.id(),
            )
        ),
    )
    slot_id = base.id()
    reject(
        ErrorCode.AUTHORITY_SLOT_OCCUPIED,
        base,
        lambda: base.service.create_case(
            CreateCaseCommand(
                command_id=base.id(),
                case_id=base.id(),
                acting_actor_id=slot_id,
                pm_actor_id=slot_id,
                dev_lead_actor_id=slot_id,
            )
        ),
    )

    delegate_id = base.id()
    base.record(
        base.service.add_participant(
            AddParticipantCommand(
                command_id=base.id(),
                case_id=base.case_id,
                acting_actor_id=base.pm_actor_id,
                expected_case_revision=base.revision,
                actor_id=delegate_id,
            )
        )
    )
    base.record(
        base.service.grant_delegation(
            GrantDelegationCommand(
                command_id=base.id(),
                case_id=base.case_id,
                acting_actor_id=base.pm_actor_id,
                expected_case_revision=base.revision,
                delegation_id=base.id(),
                delegate_id=delegate_id,
                domain=Domain.BUSINESS,
                command_names=["register_source_artifact"],
                valid_from=FROZEN_NOW - timedelta(days=2),
                valid_until=FROZEN_NOW - timedelta(days=1),
                later_review_required=False,
            )
        )
    )

    def delegated_source():
        return base.service.register_source_artifact(
            RegisterSourceArtifactCommand(
                command_id=base.id(),
                case_id=base.case_id,
                acting_actor_id=delegate_id,
                expected_case_revision=base.revision,
                identity=SourceArtifactIdentity(
                    artifact_id=base.id(),
                    case_id=base.case_id,
                    type=SourceArtifactType.OTHER,
                    version=1,
                    media_type="application/json",
                    canonical_locator="/matrix/inactive.json",
                    content_hash="0" * 64,
                ),
            )
        )

    reject(ErrorCode.DELEGATION_NOT_ACTIVE, base, delegated_source)
    base.record(
        base.service.grant_delegation(
            GrantDelegationCommand(
                command_id=base.id(),
                case_id=base.case_id,
                acting_actor_id=base.pm_actor_id,
                expected_case_revision=base.revision,
                delegation_id=base.id(),
                delegate_id=delegate_id,
                domain=Domain.BUSINESS,
                command_names=["record_ambiguity_finding"],
                valid_from=FROZEN_NOW - timedelta(days=1),
                valid_until=FROZEN_NOW + timedelta(days=1),
                later_review_required=False,
            )
        )
    )
    reject(ErrorCode.DELEGATION_SCOPE_MISMATCH, base, delegated_source)

    first_command_id = base.audit_events()[0].command_id
    second_case_id, second_pm, second_dev = base.id(), base.id(), base.id()
    base.record(
        base.service.create_case(
            CreateCaseCommand(
                command_id=base.id(),
                case_id=second_case_id,
                acting_actor_id=second_pm,
                pm_actor_id=second_pm,
                dev_lead_actor_id=second_dev,
            )
        )
    )
    reject(
        ErrorCode.CROSS_CASE_REFERENCE,
        base,
        lambda: base.service.add_participant(
            AddParticipantCommand(
                command_id=first_command_id,
                case_id=second_case_id,
                acting_actor_id=second_pm,
                expected_case_revision=1,
                actor_id=base.id(),
            )
        ),
    )
    reject(
        ErrorCode.IDEMPOTENCY_CONFLICT,
        base,
        lambda: base.service.create_case(
            CreateCaseCommand(
                command_id=first_command_id,
                case_id=base.case_id,
                acting_actor_id=base.pm_actor_id,
                pm_actor_id=base.pm_actor_id,
                dev_lead_actor_id=base.id(),
            )
        ),
    )
    reject(
        ErrorCode.STALE_ARTIFACT_BINDING,
        base,
        lambda: base.service.add_participant(
            AddParticipantCommand(
                command_id=base.id(),
                case_id=base.case_id,
                acting_actor_id=base.pm_actor_id,
                expected_case_revision=0,
                actor_id=base.id(),
            )
        ),
    )
    reject(
        ErrorCode.INVALID_SOURCE_REFERENCE,
        base,
        lambda: base.service.record_ambiguity_finding(
            RecordAmbiguityFindingCommand(
                command_id=base.id(),
                case_id=base.case_id,
                acting_actor_id="SYSTEM",
                expected_case_revision=base.revision,
                finding_id=base.id(),
                category=AmbiguityCategory.UNDEFINED_TERM,
                domain=Domain.BUSINESS,
                severity=Severity.ADVISORY,
                evidence_refs=[
                    SourceRef(
                        artifact_id=base.id(),
                        version=1,
                        content_hash="0" * 64,
                        location=JsonPointer(pointer=""),
                    )
                ],
                clarification_question="Which registered source proves this?",
            )
        ),
    )

    package = fresh_harness(tmp_path / "package")
    package.build_approved_package()
    binding = package.package_binding
    reject(
        ErrorCode.APPROVAL_ALREADY_EXISTS,
        package,
        lambda: package.service.approve_spec_package(
            ApproveSpecPackageCommand(
                command_id=package.id(),
                case_id=package.case_id,
                acting_actor_id=package.pm_actor_id,
                expected_case_revision=package.revision,
                expected_artifact_id=binding.artifact_id,
                expected_artifact_version=binding.version,
                expected_artifact_hash=binding.semantic_hash,
                scope=ApprovalScope.BUSINESS,
            )
        ),
    )
    reject(
        ErrorCode.NO_SEMANTIC_CHANGE,
        package,
        lambda: package.service.revise_spec_package(
            ReviseSpecPackageCommand(
                command_id=package.id(),
                case_id=package.case_id,
                acting_actor_id="SYSTEM",
                expected_case_revision=package.revision,
                expected_artifact_id=binding.artifact_id,
                expected_artifact_version=binding.version,
                expected_artifact_hash=binding.semantic_hash,
                content_schema_version=1,
                hash_schema_version=1,
                payload=package.package_payload,
            )
        ),
    )
    reject(
        ErrorCode.INVALID_TRANSITION,
        package,
        lambda: package.service.mark_spec_package_ready(
            MarkSpecPackageReadyCommand(
                command_id=package.id(),
                case_id=package.case_id,
                acting_actor_id="SYSTEM",
                expected_case_revision=package.revision,
                expected_artifact_id=binding.artifact_id,
                expected_artifact_version=binding.version,
                expected_artifact_hash=binding.semantic_hash,
            )
        ),
    )

    invalid_plan = fresh_harness(tmp_path / "invalid-plan")
    invalid_plan.build_approved_package()
    invalid_plan.build_approved_jira_plan()
    bad_task = invalid_plan.jira_payload.items[1].model_copy(update={"parent_item_id": None})
    bad_payload = invalid_plan.jira_payload.model_copy(
        update={"items": [invalid_plan.jira_payload.items[0], bad_task]}
    )
    reject(
        ErrorCode.INVALID_PROJECTION_PLAN,
        invalid_plan,
        lambda: invalid_plan.service.revise_projection_plan(
            ReviseProjectionPlanCommand(
                command_id=invalid_plan.id(),
                case_id=invalid_plan.case_id,
                acting_actor_id="SYSTEM",
                expected_case_revision=invalid_plan.revision,
                expected_artifact_id=invalid_plan.jira_binding.artifact_id,
                expected_artifact_version=1,
                expected_artifact_hash=invalid_plan.jira_binding.semantic_hash,
                content_schema_version=1,
                hash_schema_version=1,
                payload=bad_payload,
            )
        ),
    )

    github = fresh_harness(tmp_path / "approval-binding")
    github.build_approved_package()
    github.build_approved_jira_plan()
    github.build_approved_status_policy()
    github.apply_jira()
    github.build_approved_github_plan()
    reject(
        ErrorCode.APPROVAL_BINDING_MISMATCH,
        github,
        lambda: github.service.approve_projection_plan(
            ApproveProjectionPlanCommand(
                command_id=github.id(),
                case_id=github.case_id,
                acting_actor_id=github.pm_actor_id,
                expected_case_revision=github.revision,
                expected_artifact_id=github.github_binding.artifact_id,
                expected_artifact_version=1,
                expected_artifact_hash=github.github_binding.semantic_hash,
                scope=ApprovalScope.BUSINESS,
            )
        ),
    )

    no_policy = fresh_harness(tmp_path / "no-policy")
    fake_package = ArtifactBinding(
        artifact_kind=ArtifactKind.SPEC_PACKAGE,
        artifact_id=no_policy.id(),
        version=1,
        semantic_hash="1" * 64,
    )
    fake_plan = ArtifactBinding(
        artifact_kind=ArtifactKind.PROJECTION_PLAN,
        artifact_id=no_policy.id(),
        version=1,
        semantic_hash="2" * 64,
    )
    fake_policy = ArtifactBinding(
        artifact_kind=ArtifactKind.STATUS_POLICY,
        artifact_id=no_policy.id(),
        version=1,
        semantic_hash="3" * 64,
    )
    dummy_observation = RemoteObservation(
        observation_kind=ObservationKind.FOUND,
        system=System.JIRA,
        external_identity={"key": "OPS-901"},
        generation_key=f"specops:{no_policy.case_id}:jira:{no_policy.id()}",
        package_binding=fake_package,
        plan_binding=fake_plan,
        status_policy_binding=fake_policy,
        remote_revision="dummy-revision",
        native_status="To Do",
        owned_content={"title": "dummy"},
    )
    reject(
        ErrorCode.STATUS_POLICY_NOT_APPROVED,
        no_policy,
        lambda: no_policy.service.submit_remote_snapshot(
            SubmitRemoteSnapshotCommand(
                command_id=no_policy.id(),
                case_id=no_policy.case_id,
                acting_actor_id="SYSTEM",
                expected_case_revision=no_policy.revision,
                observation=dummy_observation,
            )
        ),
    )

    operation = fresh_harness(tmp_path / "operation")
    operation.build_approved_package()
    operation.build_approved_jira_plan()
    operation.build_approved_status_policy()
    intent = operation.pending_intents()[0]
    operation_id = operation.id()
    operation.record(
        operation.service.start_external_operation(
            StartExternalOperationCommand(
                command_id=operation.id(),
                case_id=operation.case_id,
                acting_actor_id="SYSTEM",
                expected_case_revision=operation.revision,
                operation_id=operation_id,
                intent_id=intent.intent_id,
                idempotency_key="matrix-operation",
            )
        )
    )
    reject(
        ErrorCode.OPERATION_RETRY_BLOCKED,
        operation,
        lambda: operation.service.start_external_operation(
            StartExternalOperationCommand(
                command_id=operation.id(),
                case_id=operation.case_id,
                acting_actor_id="SYSTEM",
                expected_case_revision=operation.revision,
                operation_id=operation_id,
                intent_id=intent.intent_id,
                idempotency_key="matrix-operation",
            )
        ),
    )
    operation.record(
        operation.service.record_operation_result(
            RecordOperationResultCommand(
                command_id=operation.id(),
                case_id=operation.case_id,
                acting_actor_id="SYSTEM",
                expected_case_revision=operation.revision,
                operation_id=operation_id,
                attempt=1,
                outcome=NominalSuccess(outcome=ResultOutcome.NOMINAL_SUCCESS, read_back=None),
            )
        )
    )

    def reconcile(observation):
        return operation.service.reconcile_operation(
            ReconcileOperationCommand(
                command_id=operation.id(),
                case_id=operation.case_id,
                acting_actor_id="SYSTEM",
                expected_case_revision=operation.revision,
                operation_id=operation_id,
                attempt=1,
                observation=observation,
            )
        )

    reject(
        ErrorCode.UNKNOWN_EXTERNAL_RESULT,
        operation,
        lambda: reconcile(
            _found_for_intent(intent, owned_content={**intent.request, "title": "changed"})
        ),
    )
    reject(
        ErrorCode.UNCONFIRMED_EXTERNAL_ID,
        operation,
        lambda: reconcile(
            _found_for_intent(intent, generation_key=f"{intent.request['generation_key']}:wrong")
        ),
    )
    reject(
        ErrorCode.REMOTE_VERSION_MISMATCH,
        operation,
        lambda: reconcile(
            _found_for_intent(intent, expected_previous_remote_revision="unexpected")
        ),
    )

    unmapped = fresh_harness(tmp_path / "unmapped")
    unmapped.build_approved_package()
    unmapped.build_approved_jira_plan()
    unmapped.build_approved_status_policy()
    unmapped.apply_intent(unmapped.pending_intents()[0], 1)
    first_confirmation = _confirmation(unmapped, System.JIRA)
    unmapped.record(
        unmapped.service.submit_remote_snapshot(
            SubmitRemoteSnapshotCommand(
                command_id=unmapped.id(),
                case_id=unmapped.case_id,
                acting_actor_id="SYSTEM",
                expected_case_revision=unmapped.revision,
                observation=first_confirmation.model_copy(
                    update={
                        "expected_previous_remote_revision": first_confirmation.remote_revision,
                        "remote_revision": "unmapped-revision",
                        "native_status": "Unmapped",
                    }
                ),
            )
        )
    )
    next_intent = unmapped.pending_intents()[0]
    reject(
        ErrorCode.UNMAPPED_REMOTE_STATUS,
        unmapped,
        lambda: unmapped.service.start_external_operation(
            StartExternalOperationCommand(
                command_id=unmapped.id(),
                case_id=unmapped.case_id,
                acting_actor_id="SYSTEM",
                expected_case_revision=unmapped.revision,
                operation_id=unmapped.id(),
                intent_id=next_intent.intent_id,
                idempotency_key="unmapped-blocker",
            )
        ),
    )

    blocking = fresh_harness(tmp_path / "blocking")
    blocking.build_approved_package()
    blocking.build_approved_jira_plan()
    blocking.build_approved_status_policy()
    blocking.apply_intent(blocking.pending_intents()[0], 1)
    first_confirmation = _confirmation(blocking, System.JIRA)
    blocking.record(
        blocking.service.submit_remote_snapshot(
            SubmitRemoteSnapshotCommand(
                command_id=blocking.id(),
                case_id=blocking.case_id,
                acting_actor_id="SYSTEM",
                expected_case_revision=blocking.revision,
                observation=first_confirmation.model_copy(
                    update={
                        "expected_previous_remote_revision": first_confirmation.remote_revision,
                        "remote_revision": "content-drift-revision",
                        "owned_content": {
                            **first_confirmation.owned_content,
                            "title": "externally changed",
                        },
                    }
                ),
            )
        )
    )
    blocked_intent = blocking.pending_intents()[0]
    reject(
        ErrorCode.BLOCKING_FINDING,
        blocking,
        lambda: blocking.service.start_external_operation(
            StartExternalOperationCommand(
                command_id=blocking.id(),
                case_id=blocking.case_id,
                acting_actor_id="SYSTEM",
                expected_case_revision=blocking.revision,
                operation_id=blocking.id(),
                intent_id=blocked_intent.intent_id,
                idempotency_key="content-drift-blocker",
            )
        ),
    )

    assert observed == set(ErrorCode)


@pytest.mark.ev("EV-003")
def test_found_and_not_found_restart_is_publicly_equal(tmp_path):
    harness = fresh_harness(tmp_path).complete()
    confirmed = _confirmation(harness, System.GITHUB)
    missing = confirmed.model_copy(
        update={
            "observation_kind": ObservationKind.NOT_FOUND,
            "status_policy_binding": harness.status_binding,
            "expected_previous_remote_revision": confirmed.remote_revision,
            "remote_revision": None,
            "native_status": None,
            "owned_content": None,
        }
    )
    harness.record(
        harness.service.submit_remote_snapshot(
            SubmitRemoteSnapshotCommand(
                command_id=harness.id(),
                case_id=harness.case_id,
                acting_actor_id="SYSTEM",
                expected_case_revision=harness.revision,
                observation=missing,
            )
        )
    )
    before = _public_reads(harness.service, harness.case_id)
    reopened = WorkflowService(
        clock=FrozenClock(FROZEN_NOW),
        ids=DeterministicUuidSequence(prefix=0x30000000),
        database_url=harness.database_url,
    )
    after = _public_reads(reopened, harness.case_id)
    assert before == after


@pytest.mark.ev("EV-004")
def test_domain_failure_rolls_back_every_table_and_public_read(tmp_path):
    harness = fresh_harness(tmp_path).complete()
    before_rows = _database_rows(harness.database_url)
    before_reads = _public_reads(harness.service, harness.case_id)
    before_audit = len(harness.audit_events())
    with pytest.raises(DomainError) as caught:
        harness.service.add_participant(
            AddParticipantCommand(
                command_id=harness.id(),
                case_id=harness.case_id,
                acting_actor_id=harness.id(),
                expected_case_revision=harness.revision,
                actor_id=harness.id(),
            )
        )
    assert caught.value.code == ErrorCode.AUTHORITY_REQUIRED
    assert _database_rows(harness.database_url) == before_rows
    assert _public_reads(harness.service, harness.case_id) == before_reads
    assert len(harness.audit_events()) == before_audit


@pytest.mark.ev("EV-005")
@pytest.mark.ev("EV-006")
@pytest.mark.ev("EV-007")
@pytest.mark.ev("EV-008")
@pytest.mark.ev("EV-009")
@pytest.mark.ev("EV-010")
@pytest.mark.ev("EV-012")
def test_authority_delegation_later_review_and_ambiguity_history(tmp_path):
    harness = fresh_harness(tmp_path)
    with pytest.raises(DomainError) as occupied:
        harness.service.create_case(
            CreateCaseCommand(
                command_id=harness.id(),
                case_id=harness.id(),
                acting_actor_id=harness.pm_actor_id,
                pm_actor_id=harness.pm_actor_id,
                dev_lead_actor_id=harness.pm_actor_id,
            )
        )
    assert occupied.value.code == ErrorCode.AUTHORITY_SLOT_OCCUPIED

    audit_before = len(harness.audit_events())
    with pytest.raises(DomainError) as forbidden:
        harness.service.add_participant(
            AddParticipantCommand(
                command_id=harness.id(),
                case_id=harness.case_id,
                acting_actor_id="SYSTEM",
                expected_case_revision=harness.revision,
                actor_id=harness.id(),
            )
        )
    assert forbidden.value.code == ErrorCode.SYSTEM_ACTION_FORBIDDEN
    assert len(harness.audit_events()) == audit_before

    delegate = harness.id()
    harness.record(
        harness.service.add_participant(
            AddParticipantCommand(
                command_id=harness.id(),
                case_id=harness.case_id,
                acting_actor_id=harness.pm_actor_id,
                expected_case_revision=harness.revision,
                actor_id=delegate,
            )
        )
    )
    expired_id = harness.id()
    harness.record(
        harness.service.grant_delegation(
            GrantDelegationCommand(
                command_id=harness.id(),
                case_id=harness.case_id,
                acting_actor_id=harness.pm_actor_id,
                expected_case_revision=harness.revision,
                delegation_id=expired_id,
                delegate_id=delegate,
                domain=Domain.BUSINESS,
                command_names=["register_source_artifact"],
                valid_from=FROZEN_NOW - timedelta(days=2),
                valid_until=FROZEN_NOW - timedelta(days=1),
                later_review_required=False,
            )
        )
    )
    with pytest.raises(DomainError) as inactive:
        harness.service.register_source_artifact(
            RegisterSourceArtifactCommand(
                command_id=harness.id(),
                case_id=harness.case_id,
                acting_actor_id=delegate,
                expected_case_revision=harness.revision,
                identity=SourceArtifactIdentity(
                    artifact_id=harness.id(),
                    case_id=harness.case_id,
                    type=SourceArtifactType.OTHER,
                    version=1,
                    media_type="application/json",
                    canonical_locator="/delegated/evidence.json",
                    content_hash="0" * 64,
                ),
            )
        )
    assert inactive.value.code == ErrorCode.DELEGATION_NOT_ACTIVE

    active_id = harness.id()
    harness.record(
        harness.service.grant_delegation(
            GrantDelegationCommand(
                command_id=harness.id(),
                case_id=harness.case_id,
                acting_actor_id=harness.pm_actor_id,
                expected_case_revision=harness.revision,
                delegation_id=active_id,
                delegate_id=delegate,
                domain=Domain.BUSINESS,
                command_names=["register_source_artifact"],
                valid_from=FROZEN_NOW,
                valid_until=FROZEN_NOW + timedelta(days=1),
                later_review_required=False,
            )
        )
    )
    delegated_source = harness.id()
    harness.record(
        harness.service.register_source_artifact(
            RegisterSourceArtifactCommand(
                command_id=harness.id(),
                case_id=harness.case_id,
                acting_actor_id=delegate,
                expected_case_revision=harness.revision,
                identity=SourceArtifactIdentity(
                    artifact_id=delegated_source,
                    case_id=harness.case_id,
                    type=SourceArtifactType.OTHER,
                    version=1,
                    media_type="application/json",
                    canonical_locator="/delegated/evidence.json",
                    content_hash="0" * 64,
                ),
            )
        )
    )

    harness.build_approved_package()
    finding_id = harness.id()
    finding = harness.record(
        harness.service.record_ambiguity_finding(
            RecordAmbiguityFindingCommand(
                command_id=harness.id(),
                case_id=harness.case_id,
                acting_actor_id="SYSTEM",
                expected_case_revision=harness.revision,
                finding_id=finding_id,
                category=AmbiguityCategory.MISSING_EDGE_CASE,
                domain=Domain.CROSS_DOMAIN,
                severity=Severity.ADVISORY,
                evidence_refs=[harness.source_ref],
                clarification_question="Which exact edge is required?",
            )
        )
    )
    assert finding.status == FindingStatus.OPEN
    business_resolution = harness.record(
        harness.service.resolve_ambiguity_finding(
            ResolveAmbiguityFindingCommand(
                command_id=harness.id(),
                case_id=harness.case_id,
                acting_actor_id=harness.pm_actor_id,
                expected_case_revision=harness.revision,
                finding_id=finding_id,
                resolution_text="Use the registered boundary",
                resolution_source_refs=[harness.source_ref],
            )
        )
    )
    assert business_resolution.status == FindingStatus.OPEN
    resolved = harness.record(
        harness.service.resolve_ambiguity_finding(
            ResolveAmbiguityFindingCommand(
                command_id=harness.id(),
                case_id=harness.case_id,
                acting_actor_id=harness.dev_lead_actor_id,
                expected_case_revision=harness.revision,
                finding_id=finding_id,
                resolution_text="Validate the same boundary",
                resolution_source_refs=[harness.source_ref],
            )
        )
    )
    assert resolved.status == FindingStatus.RESOLVED and len(resolved.resolutions) == 2
    audit_count = len(harness.audit_events())
    with pytest.raises(DomainError) as duplicate:
        harness.service.resolve_ambiguity_finding(
            ResolveAmbiguityFindingCommand(
                command_id=harness.id(),
                case_id=harness.case_id,
                acting_actor_id=harness.pm_actor_id,
                expected_case_revision=harness.revision,
                finding_id=finding_id,
                resolution_text="Duplicate scope",
                resolution_source_refs=[harness.source_ref],
            )
        )
    assert duplicate.value.code == ErrorCode.APPROVAL_ALREADY_EXISTS
    assert len(harness.audit_events()) == audit_count

    later_review_delegation = harness.id()
    harness.record(
        harness.service.grant_delegation(
            GrantDelegationCommand(
                command_id=harness.id(),
                case_id=harness.case_id,
                acting_actor_id=harness.dev_lead_actor_id,
                expected_case_revision=harness.revision,
                delegation_id=later_review_delegation,
                delegate_id=delegate,
                domain=Domain.TECHNICAL,
                command_names=["approve_spec_package"],
                artifact_kind=ArtifactKind.SPEC_PACKAGE,
                artifact_id=harness.package_binding.artifact_id,
                valid_from=FROZEN_NOW,
                valid_until=FROZEN_NOW + timedelta(days=1),
                later_review_required=True,
            )
        )
    )
    revised_payload = harness.package_payload.model_copy(deep=True)
    revised_payload.requirements[0].statement = "Record a later-reviewed approved outcome"
    revised = harness.record(
        harness.service.revise_spec_package(
            ReviseSpecPackageCommand(
                command_id=harness.id(),
                case_id=harness.case_id,
                acting_actor_id="SYSTEM",
                expected_case_revision=harness.revision,
                expected_artifact_id=harness.package_binding.artifact_id,
                expected_artifact_version=1,
                expected_artifact_hash=harness.package_binding.semantic_hash,
                content_schema_version=1,
                hash_schema_version=1,
                payload=revised_payload,
            )
        )
    )
    ready = harness.record(
        harness.service.mark_spec_package_ready(
            MarkSpecPackageReadyCommand(
                command_id=harness.id(),
                case_id=harness.case_id,
                acting_actor_id="SYSTEM",
                expected_case_revision=harness.revision,
                expected_artifact_id=revised.binding.artifact_id,
                expected_artifact_version=2,
                expected_artifact_hash=revised.binding.semantic_hash,
            )
        )
    )
    business = harness.record(
        harness.service.approve_spec_package(
            ApproveSpecPackageCommand(
                command_id=harness.id(),
                case_id=harness.case_id,
                acting_actor_id=harness.pm_actor_id,
                expected_case_revision=ready.receipt.revision,
                expected_artifact_id=revised.binding.artifact_id,
                expected_artifact_version=2,
                expected_artifact_hash=revised.binding.semantic_hash,
                scope=ApprovalScope.BUSINESS,
            )
        )
    )
    delegated = harness.record(
        harness.service.approve_spec_package(
            ApproveSpecPackageCommand(
                command_id=harness.id(),
                case_id=harness.case_id,
                acting_actor_id=delegate,
                expected_case_revision=business.receipt.revision,
                expected_artifact_id=revised.binding.artifact_id,
                expected_artifact_version=2,
                expected_artifact_hash=revised.binding.semantic_hash,
                scope=ApprovalScope.TECHNICAL,
            )
        )
    )
    pending = harness.service.get_workflow_view(
        QueryOne(case_id=harness.case_id, acting_actor_id="SYSTEM")
    )
    assert delegated.approval_id in pending.pending_later_review_ids
    assert pending.foundation_ready is False
    harness.record(
        harness.service.revoke_delegation(
            RevokeDelegationCommand(
                command_id=harness.id(),
                case_id=harness.case_id,
                acting_actor_id=harness.dev_lead_actor_id,
                expected_case_revision=harness.revision,
                delegation_id=later_review_delegation,
            )
        )
    )
    assert delegated.approval_id in harness.service.get_workflow_view(
        QueryOne(case_id=harness.case_id, acting_actor_id="SYSTEM")
    ).pending_later_review_ids
    harness.record(
        harness.service.approve_spec_package(
            ApproveSpecPackageCommand(
                command_id=harness.id(),
                case_id=harness.case_id,
                acting_actor_id=harness.dev_lead_actor_id,
                expected_case_revision=harness.revision,
                expected_artifact_id=revised.binding.artifact_id,
                expected_artifact_version=2,
                expected_artifact_hash=revised.binding.semantic_hash,
                scope=ApprovalScope.TECHNICAL,
            )
        )
    )
    assert harness.service.get_workflow_view(
        QueryOne(case_id=harness.case_id, acting_actor_id="SYSTEM")
    ).pending_later_review_ids == []


@pytest.mark.ev("EV-013")
@pytest.mark.ev("EV-014")
@pytest.mark.ev("EV-015")
@pytest.mark.ev("EV-016")
@pytest.mark.ev("EV-017")
def test_immutable_revisions_reverts_and_golden_hashes(tmp_path):
    harness = fresh_harness(tmp_path)
    original = harness.build_approved_package()
    harness.build_approved_jira_plan()
    assert harness.service.get_workflow_view(
        QueryOne(case_id=harness.case_id, acting_actor_id="SYSTEM")
    ).current_package.semantic_hash == original.semantic_hash

    revised_payload = harness.package_payload.model_copy(deep=True)
    revised_requirement = revised_payload.requirements[0].model_copy(
        update={"statement": "Record a revised exact approved outcome"}
    )
    revised_payload = revised_payload.model_copy(
        update={"requirements": [revised_requirement]}
    )
    revised = harness.record(
        harness.service.revise_spec_package(
            ReviseSpecPackageCommand(
                command_id=harness.id(),
                case_id=harness.case_id,
                acting_actor_id="SYSTEM",
                expected_case_revision=harness.revision,
                expected_artifact_id=original.artifact_id,
                expected_artifact_version=original.version,
                expected_artifact_hash=original.semantic_hash,
                content_schema_version=1,
                hash_schema_version=1,
                payload=revised_payload,
            )
        )
    )
    assert revised.binding.version == 2
    assert revised.binding.semantic_hash != original.semantic_hash
    assert harness.service.get_workflow_view(
        QueryOne(case_id=harness.case_id, acting_actor_id="SYSTEM")
    ).setup_stage == SetupStage.PACKAGE_REVIEW
    before = len(harness.audit_events())
    with pytest.raises(DomainError) as no_change:
        harness.service.revise_spec_package(
            ReviseSpecPackageCommand(
                command_id=harness.id(),
                case_id=harness.case_id,
                acting_actor_id="SYSTEM",
                expected_case_revision=harness.revision,
                expected_artifact_id=original.artifact_id,
                expected_artifact_version=2,
                expected_artifact_hash=revised.binding.semantic_hash,
                content_schema_version=1,
                hash_schema_version=1,
                payload=revised_payload,
            )
        )
    assert no_change.value.code == ErrorCode.NO_SEMANTIC_CHANGE
    assert len(harness.audit_events()) == before
    reverted = harness.record(
        harness.service.revise_spec_package(
            ReviseSpecPackageCommand(
                command_id=harness.id(),
                case_id=harness.case_id,
                acting_actor_id="SYSTEM",
                expected_case_revision=harness.revision,
                expected_artifact_id=original.artifact_id,
                expected_artifact_version=2,
                expected_artifact_hash=revised.binding.semantic_hash,
                content_schema_version=1,
                hash_schema_version=1,
                payload=harness.package_payload,
            )
        )
    )
    assert reverted.binding.version == 3
    assert reverted.binding.semantic_hash == original.semantic_hash

    assert artifact_hash(ArtifactKind.SPEC_PACKAGE, 1, 1, {"requirements": []}) == "c7a90a1c94b0e55bc55a42fe99c08d3149a891e75b8b22095bda04f1bae32675"
    assert artifact_hash(ArtifactKind.PROJECTION_PLAN, 1, 1, {"dependencies": ["a", "b"], "labels": ["a", "z"]}) == "33819f262772cf5f8c356edc08a92f16868e89a21bf0fb32e405fd4af662c0d9"
    assert artifact_hash(ArtifactKind.STATUS_POLICY, 1, 2, {"mappings": [], "rules": []}) == "056050a53c18bca89bec435279ea34a9649f4ae09270f8439772f18bb8f77db1"
    item_preimage = {
        "schema": "projection-item-v1",
        "target": "JIRA",
        "project_key": "OPS",
        "item": {
            "item_id": "00000000-0000-4000-8000-000000000001",
            "kind": "TASK",
            "domain": "BUSINESS",
            "title": "Example",
            "body": {
                "package_id": "00000000-0000-4000-8000-000000000003",
                "package_version": 1,
                "package_hash": "0" * 64,
                "generation_key": "specops:00000000-0000-4000-8000-000000000004:jira:00000000-0000-4000-8000-000000000001",
                "source_unit_ids": ["00000000-0000-4000-8000-000000000002"],
                "acceptance_checks": [],
                "dependency_item_ids": [],
                "provisional": False,
                "jira_key": None,
            },
            "source_unit_ids": ["00000000-0000-4000-8000-000000000002"],
            "parent_item_id": None,
            "dependency_item_ids": [],
            "implementation_required": False,
            "repository": None,
            "primary_jira_item_id": None,
        },
    }
    assert sha256(item_preimage) == "eb9c6b618b249afed2ceefcd414e6c4d030f39640a95457053e0ce3979a15032"
    operation_preimage = {
        "schema": "operation-request-v1",
        "case_id": "00000000-0000-4000-8000-000000000004",
        "system": "JIRA",
        "action": "CREATE",
        "generation_key": "specops:00000000-0000-4000-8000-000000000004:jira:00000000-0000-4000-8000-000000000001",
        "item_ref": {
            "kind": "CURRENT",
            "item_id": "00000000-0000-4000-8000-000000000001",
            "plan_id": "00000000-0000-4000-8000-000000000005",
            "plan_version": 1,
            "plan_hash": "1" * 64,
        },
        "package_binding": {
            "artifact_kind": "SPEC_PACKAGE",
            "artifact_id": "00000000-0000-4000-8000-000000000003",
            "version": 1,
            "hash": "0" * 64,
        },
        "plan_binding": {
            "artifact_kind": "PROJECTION_PLAN",
            "artifact_id": "00000000-0000-4000-8000-000000000005",
            "version": 1,
            "hash": "1" * 64,
        },
        "status_policy_binding": None,
        "request_owned_content_hash": "2" * 64,
        "external_identity": None,
        "expected_remote_revision": None,
        "target_normalized_status": None,
        "contributing_rule_ids": [],
    }
    assert sha256(operation_preimage) == "e5d5d286c021d0c46dc125e0cc3a7d7868e6d768a905946edc8160e7f0e841a6"
    engine = engine_for(harness.database_url)
    with engine.connect() as connection:
        versions = connection.execute(
            text("SELECT version, content_schema_version, hash_schema_version, semantic_hash FROM spec_package_versions ORDER BY version")
        ).all()
    assert versions == [
        (1, 1, 1, original.semantic_hash),
        (2, 1, 1, revised.binding.semantic_hash),
        (3, 1, 1, original.semantic_hash),
    ]

    registry = default_registry()
    old_digest = registry.hash(ArtifactKind.SPEC_PACKAGE, 1, 1, harness.package_payload)
    registry.register(
        HashRecipe(
            ArtifactKind.SPEC_PACKAGE,
            2,
            2,
            type(harness.package_payload).model_validate,
        )
    )
    assert registry.hash(ArtifactKind.SPEC_PACKAGE, 1, 1, harness.package_payload) == old_digest
    assert registry.hash(ArtifactKind.SPEC_PACKAGE, 2, 2, harness.package_payload) != old_digest
    with pytest.raises(DomainError) as unregistered:
        registry.hash(ArtifactKind.SPEC_PACKAGE, 99, 99, harness.package_payload)
    assert unregistered.value.code == ErrorCode.INVALID_TRANSITION

    reopened = WorkflowService(
        clock=FrozenClock(FROZEN_NOW),
        ids=DeterministicUuidSequence(prefix=0x31000000),
        database_url=harness.database_url,
    )
    assert reopened.get_workflow_view(
        QueryOne(case_id=harness.case_id, acting_actor_id="SYSTEM")
    ).current_package.semantic_hash == original.semantic_hash

    blocked = fresh_harness(tmp_path / "pending")
    blocked.build_approved_package()
    blocked.build_approved_jira_plan()
    blocked.build_approved_status_policy()
    pending_intent = blocked.pending_intents()[0]
    blocked_operation_id = blocked.id()
    blocked.record(
        blocked.service.start_external_operation(
            StartExternalOperationCommand(
                command_id=blocked.id(),
                case_id=blocked.case_id,
                acting_actor_id="SYSTEM",
                expected_case_revision=blocked.revision,
                operation_id=blocked_operation_id,
                intent_id=pending_intent.intent_id,
                idempotency_key="blocks-package-revision",
            )
        )
    )
    blocked_payload = blocked.package_payload.model_copy(deep=True)
    blocked_payload.requirements[0].statement = "A pending operation blocks revision"
    blocked_audit = len(blocked.audit_events())
    with pytest.raises(DomainError) as pending_error:
        blocked.service.revise_spec_package(
            ReviseSpecPackageCommand(
                command_id=blocked.id(),
                case_id=blocked.case_id,
                acting_actor_id="SYSTEM",
                expected_case_revision=blocked.revision,
                expected_artifact_id=blocked.package_binding.artifact_id,
                expected_artifact_version=1,
                expected_artifact_hash=blocked.package_binding.semantic_hash,
                content_schema_version=1,
                hash_schema_version=1,
                payload=blocked_payload,
            )
        )
    assert pending_error.value.code == ErrorCode.INVALID_TRANSITION
    assert len(blocked.audit_events()) == blocked_audit
    blocked.record(
        blocked.service.record_operation_result(
            RecordOperationResultCommand(
                command_id=blocked.id(),
                case_id=blocked.case_id,
                acting_actor_id="SYSTEM",
                expected_case_revision=blocked.revision,
                operation_id=blocked_operation_id,
                attempt=1,
                outcome=NominalSuccess(
                    outcome=ResultOutcome.NOMINAL_SUCCESS,
                    read_back=None,
                ),
            )
        )
    )
    unknown_audit = len(blocked.audit_events())
    with pytest.raises(DomainError) as unknown_error:
        blocked.service.revise_spec_package(
            ReviseSpecPackageCommand(
                command_id=blocked.id(),
                case_id=blocked.case_id,
                acting_actor_id="SYSTEM",
                expected_case_revision=blocked.revision,
                expected_artifact_id=blocked.package_binding.artifact_id,
                expected_artifact_version=1,
                expected_artifact_hash=blocked.package_binding.semantic_hash,
                content_schema_version=1,
                hash_schema_version=1,
                payload=blocked_payload,
            )
        )
    assert unknown_error.value.code == ErrorCode.INVALID_TRANSITION
    assert len(blocked.audit_events()) == unknown_audit


@pytest.mark.ev("EV-018")
def test_source_trust_validates_shapes_without_reading_paths(tmp_path):
    harness = fresh_harness(tmp_path)
    source_id = harness.id()
    accepted = harness.record(
        harness.service.register_source_artifact(
            RegisterSourceArtifactCommand(
                command_id=harness.id(),
                case_id=harness.case_id,
                acting_actor_id="SYSTEM",
                expected_case_revision=harness.revision,
                identity=SourceArtifactIdentity(
                    artifact_id=source_id,
                    case_id=harness.case_id,
                    type=SourceArtifactType.OTHER,
                    version=1,
                    media_type="application/json",
                    canonical_locator="/this/path/does/not/exist.json",
                    content_hash="0" * 64,
                ),
            )
        )
    )
    assert accepted.identity.canonical_locator == "/this/path/does/not/exist.json"
    with pytest.raises(ValidationError):
        JsonPointer(pointer="/~2")
    with pytest.raises(ValidationError):
        LineRange(start=2, end=1)
    assert WorkbookRange(sheet="Sheet", a1="A1:XFD1048576")
    with pytest.raises(ValidationError):
        WorkbookRange(sheet="Bad/Sheet", a1="A1")
    with pytest.raises(ValidationError):
        WorkbookRange(sheet="Sheet", a1="XFE1")

    wrong_ref = SourceRef(
        artifact_id=source_id,
        version=1,
        content_hash="f" * 64,
        location=JsonPointer(pointer=""),
    )
    before = len(harness.audit_events())
    with pytest.raises(DomainError) as mismatch:
        harness.service.record_ambiguity_finding(
            RecordAmbiguityFindingCommand(
                command_id=harness.id(),
                case_id=harness.case_id,
                acting_actor_id="SYSTEM",
                expected_case_revision=harness.revision,
                finding_id=harness.id(),
                category=AmbiguityCategory.UNDEFINED_TERM,
                domain=Domain.BUSINESS,
                severity=Severity.ADVISORY,
                evidence_refs=[wrong_ref],
                clarification_question="What is the exact term?",
            )
        )
    assert mismatch.value.code == ErrorCode.INVALID_SOURCE_REFERENCE
    assert len(harness.audit_events()) == before


@pytest.mark.ev("EV-019")
@pytest.mark.ev("EV-020")
@pytest.mark.ev("EV-022")
@pytest.mark.ev("EV-024")
def test_projection_hierarchy_cycle_leaf_and_revision_contracts(tmp_path):
    invalid_harness = fresh_harness(tmp_path / "invalid")
    invalid_harness.build_approved_package()
    invalid_harness.build_approved_jira_plan()
    current = invalid_harness.jira_payload
    invalid_task = current.items[1].model_copy(update={"parent_item_id": None})
    invalid_payload = current.model_copy(update={"items": [current.items[0], invalid_task]})
    before = len(invalid_harness.audit_events())
    with pytest.raises(DomainError) as hierarchy:
        invalid_harness.service.revise_projection_plan(
            ReviseProjectionPlanCommand(
                command_id=invalid_harness.id(),
                case_id=invalid_harness.case_id,
                acting_actor_id="SYSTEM",
                expected_case_revision=invalid_harness.revision,
                expected_artifact_id=invalid_harness.jira_binding.artifact_id,
                expected_artifact_version=1,
                expected_artifact_hash=invalid_harness.jira_binding.semantic_hash,
                content_schema_version=1,
                hash_schema_version=1,
                payload=invalid_payload,
            )
        )
    assert hierarchy.value.code == ErrorCode.INVALID_PROJECTION_PLAN
    assert len(invalid_harness.audit_events()) == before

    cycle_epic = current.items[0].model_copy(
        update={"dependency_item_ids": [current.items[1].item_id], "body": current.items[0].body.model_copy(update={"dependency_item_ids": [current.items[1].item_id]})}
    )
    cycle_task = current.items[1].model_copy(
        update={"dependency_item_ids": [current.items[0].item_id], "body": current.items[1].body.model_copy(update={"dependency_item_ids": [current.items[0].item_id]})}
    )
    cycle_payload = current.model_copy(update={"items": [cycle_epic, cycle_task]})
    with pytest.raises(DomainError) as cycle:
        invalid_harness.service.revise_projection_plan(
            ReviseProjectionPlanCommand(
                command_id=invalid_harness.id(),
                case_id=invalid_harness.case_id,
                acting_actor_id="SYSTEM",
                expected_case_revision=invalid_harness.revision,
                expected_artifact_id=invalid_harness.jira_binding.artifact_id,
                expected_artifact_version=1,
                expected_artifact_hash=invalid_harness.jira_binding.semantic_hash,
                content_schema_version=1,
                hash_schema_version=1,
                payload=cycle_payload,
            )
        )
    assert cycle.value.code == ErrorCode.INVALID_PROJECTION_PLAN

    harness = fresh_harness(tmp_path / "revision").complete()
    before_delivery = harness.service.get_delivery_view(
        QueryOne(case_id=harness.case_id, acting_actor_id="SYSTEM")
    )
    prior_identities = {
        item.item_id: item.external_identity
        for item in before_delivery.items
        if item.system == System.JIRA
    }
    revised_items = list(harness.jira_payload.items)
    revised_items[1] = revised_items[1].model_copy(update={"title": "Implement revised deterministic transition"})
    payload = harness.jira_payload.model_copy(update={"items": revised_items})
    revised = harness.record(
        harness.service.revise_projection_plan(
            ReviseProjectionPlanCommand(
                command_id=harness.id(),
                case_id=harness.case_id,
                acting_actor_id="SYSTEM",
                expected_case_revision=harness.revision,
                expected_artifact_id=harness.jira_binding.artifact_id,
                expected_artifact_version=1,
                expected_artifact_hash=harness.jira_binding.semantic_hash,
                content_schema_version=1,
                hash_schema_version=1,
                payload=payload,
            )
        )
    )
    assert revised.binding.version == 2
    assert harness.service.get_workflow_view(
        QueryOne(case_id=harness.case_id, acting_actor_id="SYSTEM")
    ).setup_stage == SetupStage.JIRA_REVIEW
    after_delivery = harness.service.get_delivery_view(
        QueryOne(case_id=harness.case_id, acting_actor_id="SYSTEM")
    )
    assert {
        item.item_id: item.external_identity
        for item in after_delivery.items
        if item.system == System.JIRA
    } == prior_identities


@pytest.mark.ev("EV-024")
def test_jira_revision_emits_exact_new_changed_removed_matrix(tmp_path):
    harness = fresh_harness(tmp_path)
    harness.build_approved_package()
    harness.build_approved_jira_plan()
    harness.build_approved_status_policy()
    harness.apply_jira()

    def approve(binding) -> None:
        business = harness.record(
            harness.service.approve_projection_plan(
                ApproveProjectionPlanCommand(
                    command_id=harness.id(),
                    case_id=harness.case_id,
                    acting_actor_id=harness.pm_actor_id,
                    expected_case_revision=harness.revision,
                    expected_artifact_id=binding.artifact_id,
                    expected_artifact_version=binding.version,
                    expected_artifact_hash=binding.semantic_hash,
                    scope=ApprovalScope.BUSINESS,
                )
            )
        )
        harness.record(
            harness.service.approve_projection_plan(
                ApproveProjectionPlanCommand(
                    command_id=harness.id(),
                    case_id=harness.case_id,
                    acting_actor_id=harness.dev_lead_actor_id,
                    expected_case_revision=business.receipt.revision,
                    expected_artifact_id=binding.artifact_id,
                    expected_artifact_version=binding.version,
                    expected_artifact_hash=binding.semantic_hash,
                    scope=ApprovalScope.TECHNICAL,
                )
            )
        )

    epic, original_task = harness.jira_payload.items
    check = harness.package_payload.acceptance_checks[0]
    source_ids = list(original_task.source_unit_ids)

    def extra_item(item_id, title):
        return ProjectionItem(
            item_id=item_id,
            kind=JiraKind.TASK,
            domain=Domain.CROSS_DOMAIN,
            title=title,
            body=StructuredWorkBody(
                package_id=harness.package_binding.artifact_id,
                package_version=harness.package_binding.version,
                package_hash=harness.package_binding.semantic_hash,
                generation_key=f"specops:{harness.case_id}:jira:{item_id}",
                source_unit_ids=source_ids,
                acceptance_checks=[
                    BodyAcceptanceCheck(check_id=check.check_id, statement=check.statement)
                ],
                provisional=False,
            ),
            source_unit_ids=source_ids,
            parent_item_id=epic.item_id,
            implementation_required=False,
        )

    bound_removed_id, unconfirmed_removed_id = harness.id(), harness.id()
    bound_removed = extra_item(bound_removed_id, "Bound item removed in the next revision")
    unconfirmed_removed = extra_item(
        unconfirmed_removed_id, "Unconfirmed item removed in the next revision"
    )
    version_two_payload = harness.jira_payload.model_copy(
        update={"items": [epic, original_task, bound_removed, unconfirmed_removed]}
    )
    version_two = harness.record(
        harness.service.revise_projection_plan(
            ReviseProjectionPlanCommand(
                command_id=harness.id(),
                case_id=harness.case_id,
                acting_actor_id="SYSTEM",
                expected_case_revision=harness.revision,
                expected_artifact_id=harness.jira_binding.artifact_id,
                expected_artifact_version=1,
                expected_artifact_hash=harness.jira_binding.semantic_hash,
                content_schema_version=1,
                hash_schema_version=1,
                payload=version_two_payload,
            )
        )
    )
    approve(version_two.binding)
    version_two_intents = harness.pending_intents()
    assert {
        (item.action, str(item.item_ref["item_id"])) for item in version_two_intents
    } == {
        (Action.CREATE, str(bound_removed_id)),
        (Action.CREATE, str(unconfirmed_removed_id)),
    }
    bound_intent = next(
        item for item in version_two_intents if str(item.item_ref["item_id"]) == str(bound_removed_id)
    )
    harness.apply_intent(bound_intent, 3)
    bound_identity = next(
        item.external_identity
        for item in harness.service.get_delivery_view(
            QueryOne(case_id=harness.case_id, acting_actor_id="SYSTEM")
        ).items
        if item.item_id == bound_removed_id
    )

    new_id = harness.id()
    new_item = extra_item(new_id, "New item created by version three")
    changed_task = original_task.model_copy(
        update={"title": "Changed retained item updated by version three"}
    )
    version_three_payload = version_two_payload.model_copy(
        update={"items": [epic, changed_task, new_item]}
    )
    version_three = harness.record(
        harness.service.revise_projection_plan(
            ReviseProjectionPlanCommand(
                command_id=harness.id(),
                case_id=harness.case_id,
                acting_actor_id="SYSTEM",
                expected_case_revision=harness.revision,
                expected_artifact_id=version_two.binding.artifact_id,
                expected_artifact_version=version_two.binding.version,
                expected_artifact_hash=version_two.binding.semantic_hash,
                content_schema_version=1,
                hash_schema_version=1,
                payload=version_three_payload,
            )
        )
    )
    assert version_three.binding.version == 3
    assert harness.pending_intents() == []
    approve(version_three.binding)
    matrix = harness.pending_intents()
    assert {(item.action, str(item.item_ref["item_id"])) for item in matrix} == {
        (Action.UPDATE, str(original_task.item_id)),
        (Action.RETIRE, str(bound_removed_id)),
        (Action.CREATE, str(new_id)),
    }
    assert all(str(item.item_ref["item_id"]) != str(unconfirmed_removed_id) for item in matrix)
    assert all(str(item.item_ref["item_id"]) != str(epic.item_id) for item in matrix)
    retire = next(item for item in matrix if item.action == Action.RETIRE)
    assert retire.item_ref["kind"] == "TOMBSTONE"
    assert retire.item_ref["plan_version"] == 3
    assert retire.item_ref["prior_plan_version"] == 2
    assert retire.existing == bound_identity
    assert retire.request["generation_key"] == f"specops:{harness.case_id}:jira:{bound_removed_id}"
    update = next(item for item in matrix if item.action == Action.UPDATE)
    retained_identity = next(
        item.external_identity
        for item in harness.service.get_delivery_view(
            QueryOne(case_id=harness.case_id, acting_actor_id="SYSTEM")
        ).items
        if item.item_id == original_task.item_id
    )
    assert update.existing == retained_identity


@pytest.mark.ev("EV-029")
@pytest.mark.ev("EV-030")
@pytest.mark.ev("EV-031")
@pytest.mark.ev("EV-033")
@pytest.mark.ev("EV-034")
def test_operation_retry_unknown_reconcile_and_replay_branches(tmp_path):
    harness = fresh_harness(tmp_path)
    harness.build_approved_package()
    harness.build_approved_jira_plan()
    harness.build_approved_status_policy()
    intents = [item for item in harness.pending_intents() if item.system == System.JIRA]
    assert len(intents) == 1 and intents[0].action == Action.CREATE
    intent = intents[0]
    operation_id = harness.id()
    start_command = StartExternalOperationCommand(
        command_id=harness.id(),
        case_id=harness.case_id,
        acting_actor_id="SYSTEM",
        expected_case_revision=harness.revision,
        operation_id=operation_id,
        intent_id=intent.intent_id,
        idempotency_key="retry-branch",
    )
    started = harness.record(harness.service.start_external_operation(start_command))
    assert started.status == OperationStatus.PENDING and started.attempt == 1
    with pytest.raises(DomainError) as blind:
        harness.service.start_external_operation(
            start_command.model_copy(
                update={"command_id": harness.id(), "expected_case_revision": harness.revision}
            )
        )
    assert blind.value.code == ErrorCode.OPERATION_RETRY_BLOCKED

    unknown = harness.record(
        harness.service.record_operation_result(
            RecordOperationResultCommand(
                command_id=harness.id(),
                case_id=harness.case_id,
                acting_actor_id="SYSTEM",
                expected_case_revision=harness.revision,
                operation_id=operation_id,
                attempt=1,
                outcome=NominalSuccess(outcome=ResultOutcome.NOMINAL_SUCCESS, read_back=None),
            )
        )
    )
    assert unknown.status == OperationStatus.UNKNOWN and unknown.confirmation is None
    with pytest.raises(DomainError) as unknown_retry:
        harness.service.start_external_operation(
            start_command.model_copy(
                update={"command_id": harness.id(), "expected_case_revision": harness.revision}
            )
        )
    assert unknown_retry.value.code == ErrorCode.OPERATION_RETRY_BLOCKED

    observation = RemoteObservation(
        observation_kind=ObservationKind.FOUND,
        system=System.JIRA,
        external_identity={"key": "OPS-1"},
        generation_key=intent.request["generation_key"],
        package_binding=intent.package_binding,
        plan_binding=intent.plan_binding,
        jira_plan_binding=intent.jira_plan_binding,
        status_policy_binding=intent.status_policy_binding,
        remote_revision="revision-1",
        native_status="To Do",
        owned_content=intent.request,
    )
    reconcile_command = ReconcileOperationCommand(
        command_id=harness.id(),
        case_id=harness.case_id,
        acting_actor_id="SYSTEM",
        expected_case_revision=harness.revision,
        operation_id=operation_id,
        attempt=1,
        observation=observation,
    )
    reconciled = harness.record(harness.service.reconcile_operation(reconcile_command))
    assert reconciled.status == OperationStatus.SUCCEEDED
    replayed = harness.service.reconcile_operation(reconcile_command)
    assert isinstance(replayed, ReplayResult)
    assert replayed.kind == "MUTATED"
    assert replayed.result["operation_id"] == str(operation_id)
    assert len(harness.audit_events()) == len(harness.successful_command_ids)
    refreshed = harness.record(
        harness.service.reconcile_operation(
            ReconcileOperationCommand(
                command_id=harness.id(),
                case_id=harness.case_id,
                acting_actor_id="SYSTEM",
                expected_case_revision=harness.revision,
                operation_id=operation_id,
                attempt=1,
                observation=observation.model_copy(
                    update={
                        "expected_previous_remote_revision": observation.remote_revision,
                        "remote_revision": "revision-2",
                    }
                ),
            )
        )
    )
    assert refreshed.status == OperationStatus.SUCCEEDED
    assert refreshed.confirmed_snapshot_sequence == 2
    assert refreshed.confirmation.remote_revision == "revision-2"

    remaining = [item for item in harness.pending_intents() if item.system == System.JIRA]
    assert len(remaining) == 1 and remaining[0].action == Action.CREATE
    with pytest.raises(DomainError) as key_conflict:
        harness.service.start_external_operation(
            StartExternalOperationCommand(
                command_id=harness.id(),
                case_id=harness.case_id,
                acting_actor_id="SYSTEM",
                expected_case_revision=harness.revision,
                operation_id=harness.id(),
                intent_id=remaining[0].intent_id,
                idempotency_key="retry-branch",
            )
        )
    assert key_conflict.value.code == ErrorCode.IDEMPOTENCY_CONFLICT

    failed_operation_id = harness.id()
    failed_start = harness.record(
        harness.service.start_external_operation(
            StartExternalOperationCommand(
                command_id=harness.id(),
                case_id=harness.case_id,
                acting_actor_id="SYSTEM",
                expected_case_revision=harness.revision,
                operation_id=failed_operation_id,
                intent_id=remaining[0].intent_id,
                idempotency_key="failed-retry-branch",
            )
        )
    )
    failed = harness.record(
        harness.service.record_operation_result(
            RecordOperationResultCommand(
                command_id=harness.id(),
                case_id=harness.case_id,
                acting_actor_id="SYSTEM",
                expected_case_revision=failed_start.receipt.revision,
                operation_id=failed_operation_id,
                attempt=1,
                outcome=ExplicitFailure(
                    outcome=ResultOutcome.EXPLICIT_FAILURE,
                    failure_code="transport-failed",
                ),
            )
        )
    )
    retried = harness.record(
        harness.service.start_external_operation(
            StartExternalOperationCommand(
                command_id=harness.id(),
                case_id=harness.case_id,
                acting_actor_id="SYSTEM",
                expected_case_revision=failed.receipt.revision,
                operation_id=failed_operation_id,
                intent_id=remaining[0].intent_id,
                idempotency_key="failed-retry-branch",
            )
        )
    )
    assert retried.status == OperationStatus.PENDING and retried.attempt == 2


@pytest.mark.ev("EV-034")
def test_unknown_update_reconciles_not_found_to_failed_without_new_binding(tmp_path):
    harness = fresh_harness(tmp_path).complete()
    github_item = harness.github_payload.items[0]
    changed = github_item.model_copy(update={"title": "Changed issue for unknown reconciliation"})
    payload = harness.github_payload.model_copy(update={"items": [changed]})
    revised = harness.record(
        harness.service.revise_projection_plan(
            ReviseProjectionPlanCommand(
                command_id=harness.id(),
                case_id=harness.case_id,
                acting_actor_id="SYSTEM",
                expected_case_revision=harness.revision,
                expected_artifact_id=harness.github_binding.artifact_id,
                expected_artifact_version=harness.github_binding.version,
                expected_artifact_hash=harness.github_binding.semantic_hash,
                content_schema_version=1,
                hash_schema_version=1,
                payload=payload,
            )
        )
    )
    harness.record(
        harness.service.approve_projection_plan(
            ApproveProjectionPlanCommand(
                command_id=harness.id(),
                case_id=harness.case_id,
                acting_actor_id=harness.dev_lead_actor_id,
                expected_case_revision=harness.revision,
                expected_artifact_id=revised.binding.artifact_id,
                expected_artifact_version=revised.binding.version,
                expected_artifact_hash=revised.binding.semantic_hash,
                scope=ApprovalScope.TECHNICAL,
            )
        )
    )
    intent = next(item for item in harness.pending_intents() if item.action == Action.UPDATE)
    operation_id = harness.id()
    harness.record(
        harness.service.start_external_operation(
            StartExternalOperationCommand(
                command_id=harness.id(),
                case_id=harness.case_id,
                acting_actor_id="SYSTEM",
                expected_case_revision=harness.revision,
                operation_id=operation_id,
                intent_id=intent.intent_id,
                idempotency_key="unknown-update-not-found",
            )
        )
    )
    harness.record(
        harness.service.record_operation_result(
            RecordOperationResultCommand(
                command_id=harness.id(),
                case_id=harness.case_id,
                acting_actor_id="SYSTEM",
                expected_case_revision=harness.revision,
                operation_id=operation_id,
                attempt=1,
                outcome=NominalSuccess(outcome=ResultOutcome.NOMINAL_SUCCESS, read_back=None),
            )
        )
    )
    not_found = RemoteObservation(
        observation_kind=ObservationKind.NOT_FOUND,
        system=System.GITHUB,
        external_identity=intent.existing,
        generation_key=intent.request["generation_key"],
        package_binding=intent.package_binding,
        plan_binding=intent.plan_binding,
        jira_plan_binding=intent.jira_plan_binding,
        status_policy_binding=intent.status_policy_binding,
        expected_previous_remote_revision=intent.expected,
    )
    reconciled = harness.record(
        harness.service.reconcile_operation(
            ReconcileOperationCommand(
                command_id=harness.id(),
                case_id=harness.case_id,
                acting_actor_id="SYSTEM",
                expected_case_revision=harness.revision,
                operation_id=operation_id,
                attempt=1,
                observation=not_found,
            )
        )
    )
    assert reconciled.status == OperationStatus.FAILED
    assert reconciled.failure_code == "REMOTE_NOT_FOUND"
    assert reconciled.confirmation is None
    assert any(
        item.category == FindingCategory.MISSING_REMOTE_ITEM and item.active
        for item in harness.service.list_drift_findings(
            UUIDListQuery(case_id=harness.case_id, acting_actor_id="SYSTEM", limit=500)
        ).items
    )


@pytest.mark.ev("EV-037")
@pytest.mark.ev("EV-038")
@pytest.mark.ev("EV-039")
def test_policy_intents_and_snapshot_drift_are_deterministic(tmp_path):
    left = fresh_harness(tmp_path / "left")
    right = fresh_harness(tmp_path / "right")
    for harness in (left, right):
        harness.build_approved_package()
        harness.build_approved_jira_plan()
        harness.build_approved_status_policy()
    assert left.pending_intents() == right.pending_intents()
    assert all(intent.fingerprint == sha256({
        "schema": "operation-request-v1",
        "case_id": left.case_id,
        "system": intent.system,
        "action": intent.action,
        "generation_key": intent.request["generation_key"],
        "item_ref": intent.item_ref,
        "package_binding": intent.package_binding,
        "plan_binding": intent.plan_binding,
        "status_policy_binding": intent.status_policy_binding,
        "request_owned_content_hash": intent.request_owned_content_hash,
        "external_identity": intent.existing,
        "expected_remote_revision": intent.expected,
        "target_normalized_status": intent.target_normalized_status,
        "contributing_rule_ids": intent.contributing_rule_ids,
    }) for intent in left.pending_intents())

    harness = fresh_harness(tmp_path / "drift").complete()
    confirmed = _confirmation(harness, System.GITHUB)
    missing = confirmed.model_copy(
        update={
            "observation_kind": ObservationKind.NOT_FOUND,
            "status_policy_binding": harness.status_binding,
            "expected_previous_remote_revision": confirmed.remote_revision,
            "remote_revision": None,
            "native_status": None,
            "owned_content": None,
        }
    )
    missing_result = harness.record(
        harness.service.submit_remote_snapshot(
            SubmitRemoteSnapshotCommand(
                command_id=harness.id(),
                case_id=harness.case_id,
                acting_actor_id="SYSTEM",
                expected_case_revision=harness.revision,
                observation=missing,
            )
        )
    )
    assert missing_result.observation_sequence == 2
    assert len(missing_result.active_finding_ids) == 1
    missing_id = missing_result.active_finding_ids[0]

    restored = confirmed.model_copy(
        update={
            "status_policy_binding": harness.status_binding,
            "expected_previous_remote_revision": confirmed.remote_revision,
            "remote_revision": "revision-restored",
        }
    )
    restored_result = harness.record(
        harness.service.submit_remote_snapshot(
            SubmitRemoteSnapshotCommand(
                command_id=harness.id(),
                case_id=harness.case_id,
                acting_actor_id="SYSTEM",
                expected_case_revision=harness.revision,
                observation=restored,
            )
        )
    )
    assert restored_result.observation_sequence == 3
    findings = harness.service.list_drift_findings(
        UUIDListQuery(case_id=harness.case_id, acting_actor_id="SYSTEM", limit=500)
    ).items
    missing_finding = next(item for item in findings if item.id == missing_id)
    assert missing_finding.category == FindingCategory.MISSING_REMOTE_ITEM
    assert missing_finding.active is False and missing_finding.resolved_at == FROZEN_NOW

    changed = restored.model_copy(
        update={
            "remote_revision": "revision-changed",
            "expected_previous_remote_revision": "revision-restored",
            "owned_content": {**restored.owned_content, "labels": ["specops", "changed"]},
        }
    )
    drifted = harness.record(
        harness.service.submit_remote_snapshot(
            SubmitRemoteSnapshotCommand(
                command_id=harness.id(),
                case_id=harness.case_id,
                acting_actor_id="SYSTEM",
                expected_case_revision=harness.revision,
                observation=changed,
            )
        )
    )
    categories = {
        item.category
        for item in harness.service.list_drift_findings(
            UUIDListQuery(case_id=harness.case_id, acting_actor_id="SYSTEM", limit=500)
        ).items
        if item.id in drifted.active_finding_ids
    }
    assert FindingCategory.CONTENT_DRIFT in categories


@pytest.mark.ev("EV-037")
def test_status_sync_resolves_on_equality_and_conflicting_targets_block_intent(tmp_path):
    harness = fresh_harness(tmp_path)
    harness.build_approved_package()
    harness.build_approved_jira_plan()
    harness.build_approved_status_policy()
    assert all(item.action != Action.TRANSITION_STATUS for item in harness.pending_intents())
    assert harness.service.list_drift_findings(
        UUIDListQuery(case_id=harness.case_id, acting_actor_id="SYSTEM", limit=500)
    ).items == []
    harness.apply_jira()

    confirmation = _confirmation(harness, System.JIRA)
    changed_status = confirmation.model_copy(
        update={
            "expected_previous_remote_revision": confirmation.remote_revision,
            "remote_revision": "status-done-revision",
            "native_status": "Done",
        }
    )
    harness.record(
        harness.service.submit_remote_snapshot(
            SubmitRemoteSnapshotCommand(
                command_id=harness.id(),
                case_id=harness.case_id,
                acting_actor_id="SYSTEM",
                expected_case_revision=harness.revision,
                observation=changed_status,
            )
        )
    )
    transition = [
        item for item in harness.pending_intents() if item.action == Action.TRANSITION_STATUS
    ]
    assert len(transition) == 1
    assert transition[0].target_normalized_status == JiraStatus.TODO
    assert transition[0].contributing_rule_ids == [harness.status_payload.rules[0].rule_id]
    sync_finding = next(
        item
        for item in harness.service.list_drift_findings(
            UUIDListQuery(case_id=harness.case_id, acting_actor_id="SYSTEM", limit=500)
        ).items
        if item.category == FindingCategory.STATUS_SYNC_REQUIRED and item.active
    )
    assert sync_finding.expected_value == JiraStatus.TODO.value
    assert sync_finding.observed_value == JiraStatus.DONE.value
    assert all(
        not (item.category == FindingCategory.CONTENT_DRIFT and item.active)
        for item in harness.service.list_drift_findings(
            UUIDListQuery(case_id=harness.case_id, acting_actor_id="SYSTEM", limit=500)
        ).items
    )

    equal_status = changed_status.model_copy(
        update={
            "expected_previous_remote_revision": changed_status.remote_revision,
            "remote_revision": "status-todo-revision",
            "native_status": "To Do",
        }
    )
    harness.record(
        harness.service.submit_remote_snapshot(
            SubmitRemoteSnapshotCommand(
                command_id=harness.id(),
                case_id=harness.case_id,
                acting_actor_id="SYSTEM",
                expected_case_revision=harness.revision,
                observation=equal_status,
            )
        )
    )
    assert all(item.action != Action.TRANSITION_STATUS for item in harness.pending_intents())
    resolved = next(
        item
        for item in harness.service.list_drift_findings(
            UUIDListQuery(case_id=harness.case_id, acting_actor_id="SYSTEM", limit=500)
        ).items
        if item.id == sync_finding.id
    )
    assert resolved.active is False and resolved.resolved_at == FROZEN_NOW

    item_id = next(
        item.item_id
        for item in harness.service.get_delivery_view(
            QueryOne(case_id=harness.case_id, acting_actor_id="SYSTEM")
        ).items
        if item.system == System.JIRA
    )
    conflict_payload = StatusPolicyPayload(
        mappings=harness.status_payload.mappings,
        rules=[
            StatusRule(
                rule_id=harness.id(),
                target_system=System.JIRA,
                selector=Selector.ITEM_IDS,
                item_ids=[item_id],
                target_normalized_status=JiraStatus.TODO,
                all_of=[
                    StatusCondition(kind=ConditionKind.PACKAGE_CURRENT_APPROVED)
                ],
            ),
            StatusRule(
                rule_id=harness.id(),
                target_system=System.JIRA,
                selector=Selector.ITEM_IDS,
                item_ids=[item_id],
                target_normalized_status=JiraStatus.DONE,
                all_of=[
                    StatusCondition(kind=ConditionKind.PACKAGE_CURRENT_APPROVED)
                ],
            ),
        ],
    )
    revised = harness.record(
        harness.service.revise_status_policy(
            ReviseStatusPolicyCommand(
                command_id=harness.id(),
                case_id=harness.case_id,
                acting_actor_id=harness.pm_actor_id,
                expected_case_revision=harness.revision,
                expected_artifact_id=harness.status_binding.artifact_id,
                expected_artifact_version=harness.status_binding.version,
                expected_artifact_hash=harness.status_binding.semantic_hash,
                content_schema_version=1,
                hash_schema_version=1,
                payload=conflict_payload,
            )
        )
    )
    business = harness.record(
        harness.service.approve_status_policy(
            ApproveStatusPolicyCommand(
                command_id=harness.id(),
                case_id=harness.case_id,
                acting_actor_id=harness.pm_actor_id,
                expected_case_revision=harness.revision,
                expected_artifact_id=revised.binding.artifact_id,
                expected_artifact_version=revised.binding.version,
                expected_artifact_hash=revised.binding.semantic_hash,
                scope=ApprovalScope.BUSINESS,
            )
        )
    )
    harness.record(
        harness.service.approve_status_policy(
            ApproveStatusPolicyCommand(
                command_id=harness.id(),
                case_id=harness.case_id,
                acting_actor_id=harness.dev_lead_actor_id,
                expected_case_revision=business.receipt.revision,
                expected_artifact_id=revised.binding.artifact_id,
                expected_artifact_version=revised.binding.version,
                expected_artifact_hash=revised.binding.semantic_hash,
                scope=ApprovalScope.TECHNICAL,
            )
        )
    )
    conflicts = [
        item
        for item in harness.service.list_drift_findings(
            UUIDListQuery(case_id=harness.case_id, acting_actor_id="SYSTEM", limit=500)
        ).items
        if item.category == FindingCategory.STATUS_CONFLICT and item.active
    ]
    assert len(conflicts) == 1
    assert sorted(conflicts[0].observed_value) == [JiraStatus.DONE.value, JiraStatus.TODO.value]
    assert all(
        not (
            item.action == Action.TRANSITION_STATUS
            and str(item.item_ref["item_id"]) == str(item_id)
        )
        for item in harness.pending_intents()
    )


@pytest.mark.ev("EV-039")
def test_retirement_tombstone_blocks_until_found_proves_retired(tmp_path):
    harness = fresh_harness(tmp_path)
    harness.build_approved_package()
    harness.build_approved_jira_plan()
    harness.build_approved_status_policy()
    harness.apply_jira()
    epic, removed_task = harness.jira_payload.items
    prior_delivery = harness.service.get_delivery_view(
        QueryOne(case_id=harness.case_id, acting_actor_id="SYSTEM")
    )
    prior_item = next(item for item in prior_delivery.items if item.item_id == removed_task.item_id)

    revised_payload = harness.jira_payload.model_copy(update={"items": [epic]})
    revised = harness.record(
        harness.service.revise_projection_plan(
            ReviseProjectionPlanCommand(
                command_id=harness.id(),
                case_id=harness.case_id,
                acting_actor_id="SYSTEM",
                expected_case_revision=harness.revision,
                expected_artifact_id=harness.jira_binding.artifact_id,
                expected_artifact_version=harness.jira_binding.version,
                expected_artifact_hash=harness.jira_binding.semantic_hash,
                content_schema_version=1,
                hash_schema_version=1,
                payload=revised_payload,
            )
        )
    )
    business = harness.record(
        harness.service.approve_projection_plan(
            ApproveProjectionPlanCommand(
                command_id=harness.id(),
                case_id=harness.case_id,
                acting_actor_id=harness.pm_actor_id,
                expected_case_revision=harness.revision,
                expected_artifact_id=revised.binding.artifact_id,
                expected_artifact_version=revised.binding.version,
                expected_artifact_hash=revised.binding.semantic_hash,
                scope=ApprovalScope.BUSINESS,
            )
        )
    )
    harness.record(
        harness.service.approve_projection_plan(
            ApproveProjectionPlanCommand(
                command_id=harness.id(),
                case_id=harness.case_id,
                acting_actor_id=harness.dev_lead_actor_id,
                expected_case_revision=business.receipt.revision,
                expected_artifact_id=revised.binding.artifact_id,
                expected_artifact_version=revised.binding.version,
                expected_artifact_hash=revised.binding.semantic_hash,
                scope=ApprovalScope.TECHNICAL,
            )
        )
    )
    retire = next(item for item in harness.pending_intents() if item.action == Action.RETIRE)
    pending_delivery = harness.service.get_delivery_view(
        QueryOne(case_id=harness.case_id, acting_actor_id="SYSTEM")
    )
    pending_item = next(
        item for item in pending_delivery.items if item.item_id == removed_task.item_id
    )
    assert pending_item.external_identity == prior_item.external_identity
    assert pending_item.lifecycle_state.value == "ACTIVE"
    assert retire.intent_id in pending_item.pending_intent_ids

    missing = RemoteObservation(
        observation_kind=ObservationKind.NOT_FOUND,
        system=System.JIRA,
        external_identity=retire.existing,
        generation_key=retire.request["generation_key"],
        package_binding=retire.package_binding,
        plan_binding=retire.plan_binding,
        status_policy_binding=retire.status_policy_binding,
        expected_previous_remote_revision=retire.expected,
    )
    missing_result = harness.record(
        harness.service.submit_remote_snapshot(
            SubmitRemoteSnapshotCommand(
                command_id=harness.id(),
                case_id=harness.case_id,
                acting_actor_id="SYSTEM",
                expected_case_revision=harness.revision,
                observation=missing,
            )
        )
    )
    missing_finding = next(
        item
        for item in harness.service.list_drift_findings(
            UUIDListQuery(case_id=harness.case_id, acting_actor_id="SYSTEM", limit=500)
        ).items
        if item.category == FindingCategory.MISSING_REMOTE_ITEM and item.active
    )
    assert missing_result.observation_sequence == 2
    missing_delivery = next(
        item
        for item in harness.service.get_delivery_view(
            QueryOne(case_id=harness.case_id, acting_actor_id="SYSTEM")
        ).items
        if item.item_id == removed_task.item_id
    )
    assert missing_delivery.lifecycle_state.value == "UNKNOWN"

    retired = RemoteObservation(
        observation_kind=ObservationKind.FOUND,
        system=System.JIRA,
        external_identity=retire.existing,
        generation_key=retire.request["generation_key"],
        package_binding=retire.package_binding,
        plan_binding=retire.plan_binding,
        status_policy_binding=retire.status_policy_binding,
        expected_previous_remote_revision=retire.expected,
        remote_revision="retirement-proof-revision",
        native_status="Cancelled",
        owned_content=retire.request,
    )
    retired_result = harness.record(
        harness.service.submit_remote_snapshot(
            SubmitRemoteSnapshotCommand(
                command_id=harness.id(),
                case_id=harness.case_id,
                acting_actor_id="SYSTEM",
                expected_case_revision=harness.revision,
                observation=retired,
            )
        )
    )
    assert retired_result.observation_sequence == 3
    resolved = next(
        item
        for item in harness.service.list_drift_findings(
            UUIDListQuery(case_id=harness.case_id, acting_actor_id="SYSTEM", limit=500)
        ).items
        if item.id == missing_finding.id
    )
    assert resolved.active is False and resolved.resolved_at == FROZEN_NOW
    assert all(
        item.item_id != removed_task.item_id
        for item in harness.service.get_delivery_view(
            QueryOne(case_id=harness.case_id, acting_actor_id="SYSTEM")
        ).items
    )
    assert all(
        not (
            item.action == Action.RETIRE
            and str(item.item_ref["item_id"]) == str(removed_task.item_id)
        )
        for item in harness.pending_intents()
    )


@pytest.mark.ev("EV-040")
def test_trace_overflow_rejects_without_truncation_write_or_audit(tmp_path):
    harness = fresh_harness(tmp_path)
    original = harness.build_approved_package()
    source_ref = harness.source_ref
    decision = harness.package_payload.technical_decisions[0]
    check = harness.package_payload.acceptance_checks[0]
    requirements = [
        Requirement(
            unit_id=harness.id(),
            statement=f"Required trace source {number:03d}",
            domain=Domain.BUSINESS,
            delivery_required=True,
            source_refs=[source_ref],
        )
        for number in range(399)
    ]
    expanded_payload = harness.package_payload.model_copy(
        update={
            "requirements": requirements,
            "technical_decisions": [
                TechnicalDecision(
                    unit_id=decision.unit_id,
                    statement=decision.statement,
                    domain=decision.domain,
                    delivery_required=True,
                    source_refs=[source_ref],
                    provisional=False,
                )
            ],
            "acceptance_checks": [
                AcceptanceCheck(
                    check_id=check.check_id,
                    statement=check.statement,
                    domain=check.domain,
                    related_unit_ids=[requirements[0].unit_id, decision.unit_id],
                    source_refs=[source_ref],
                )
            ],
        }
    )
    revised = harness.record(
        harness.service.revise_spec_package(
            ReviseSpecPackageCommand(
                command_id=harness.id(),
                case_id=harness.case_id,
                acting_actor_id="SYSTEM",
                expected_case_revision=harness.revision,
                expected_artifact_id=original.artifact_id,
                expected_artifact_version=original.version,
                expected_artifact_hash=original.semantic_hash,
                content_schema_version=1,
                hash_schema_version=1,
                payload=expanded_payload,
            )
        )
    )
    ready = harness.record(
        harness.service.mark_spec_package_ready(
            MarkSpecPackageReadyCommand(
                command_id=harness.id(),
                case_id=harness.case_id,
                acting_actor_id="SYSTEM",
                expected_case_revision=harness.revision,
                expected_artifact_id=revised.binding.artifact_id,
                expected_artifact_version=revised.binding.version,
                expected_artifact_hash=revised.binding.semantic_hash,
            )
        )
    )
    business = harness.record(
        harness.service.approve_spec_package(
            ApproveSpecPackageCommand(
                command_id=harness.id(),
                case_id=harness.case_id,
                acting_actor_id=harness.pm_actor_id,
                expected_case_revision=ready.receipt.revision,
                expected_artifact_id=revised.binding.artifact_id,
                expected_artifact_version=revised.binding.version,
                expected_artifact_hash=revised.binding.semantic_hash,
                scope=ApprovalScope.BUSINESS,
            )
        )
    )
    harness.record(
        harness.service.approve_spec_package(
            ApproveSpecPackageCommand(
                command_id=harness.id(),
                case_id=harness.case_id,
                acting_actor_id=harness.dev_lead_actor_id,
                expected_case_revision=business.receipt.revision,
                expected_artifact_id=revised.binding.artifact_id,
                expected_artifact_version=revised.binding.version,
                expected_artifact_hash=revised.binding.semantic_hash,
                scope=ApprovalScope.TECHNICAL,
            )
        )
    )

    source_ids = [item.unit_id for item in requirements] + [decision.unit_id]
    item_ids = [harness.id() for _ in range(500)]
    items = []
    for number, item_id in enumerate(item_ids):
        body = StructuredWorkBody(
            package_id=revised.binding.artifact_id,
            package_version=revised.binding.version,
            package_hash=revised.binding.semantic_hash,
            generation_key=f"specops:{harness.case_id}:jira:{item_id}",
            source_unit_ids=source_ids,
            acceptance_checks=[
                BodyAcceptanceCheck(check_id=check.check_id, statement=check.statement)
            ],
            provisional=False,
        )
        items.append(
            ProjectionItem(
                item_id=item_id,
                kind=JiraKind.EPIC if number == 0 else JiraKind.TASK,
                domain=Domain.CROSS_DOMAIN,
                title=f"Trace projection {number:03d}",
                body=body,
                source_unit_ids=source_ids,
                parent_item_id=None if number == 0 else item_ids[0],
                implementation_required=False,
            )
        )
    payload = ProjectionPlanPayload(
        package_binding=revised.binding,
        target=PlanTarget.JIRA,
        project_key="OPS",
        items=items,
    )
    audit_before = len(harness.audit_events())
    with pytest.raises(DomainError) as overflow:
        harness.service.create_projection_plan(
            CreateProjectionPlanCommand(
                command_id=harness.id(),
                case_id=harness.case_id,
                acting_actor_id="SYSTEM",
                expected_case_revision=harness.revision,
                plan_id=harness.id(),
                content_schema_version=1,
                hash_schema_version=1,
                payload=payload,
            )
        )
    assert overflow.value.code == ErrorCode.INVALID_PROJECTION_PLAN
    assert len(harness.audit_events()) == audit_before
    workflow = harness.service.get_workflow_view(
        QueryOne(case_id=harness.case_id, acting_actor_id="SYSTEM")
    )
    assert workflow.current_jira_plan is None
    with engine_for(harness.database_url).connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM projection_plans")).scalar_one() == 0
        assert connection.execute(text("SELECT COUNT(*) FROM projection_plan_versions")).scalar_one() == 0


@pytest.mark.ev("EV-042")
def test_fresh_migration_matches_metadata_and_append_only_contract(tmp_path):
    harness = fresh_harness(tmp_path)
    engine = engine_for(harness.database_url)
    inspector = inspect(engine)
    assert set(inspector.get_table_names()) == set(metadata.tables) | {"alembic_version"}
    assert len(metadata.tables) == 25
    for table_name, table in metadata.tables.items():
        actual_columns = {column["name"]: column for column in inspector.get_columns(table_name)}
        assert set(actual_columns) == {column.name for column in table.columns}
        for column in table.columns:
            assert actual_columns[column.name]["nullable"] == column.nullable
            assert str(actual_columns[column.name]["type"]).upper() == str(
                column.type.compile(dialect=engine.dialect)
            ).upper()
        assert set(inspector.get_pk_constraint(table_name)["constrained_columns"] or []) == {
            column.name for column in table.primary_key.columns
        }
        actual_foreign_keys = {
            (
                tuple(value["constrained_columns"]),
                value["referred_table"],
                tuple(value["referred_columns"]),
                tuple(sorted(value.get("options", {}).items())),
            )
            for value in inspector.get_foreign_keys(table_name)
        }
        expected_foreign_keys = set()
        for value in table.foreign_key_constraints:
            options = {"ondelete": value.ondelete}
            if value.deferrable is not None:
                options["deferrable"] = value.deferrable
            if value.initially is not None:
                options["initially"] = value.initially
            expected_foreign_keys.add(
                (
                    tuple(column.name for column in value.columns),
                    value.referred_table.name,
                    tuple(element.column.name for element in value.elements),
                    tuple(sorted(options.items())),
                )
            )
        assert actual_foreign_keys == expected_foreign_keys
        actual_uniques = {
            tuple(value["column_names"])
            for value in inspector.get_unique_constraints(table_name)
        }
        expected_uniques = {
            tuple(column.name for column in value.columns)
            for value in table.constraints
            if isinstance(value, UniqueConstraint)
        }
        assert actual_uniques == expected_uniques
        actual_indexes = {
            (value["name"], tuple(value["column_names"]), bool(value["unique"]))
            for value in inspector.get_indexes(table_name)
        }
        expected_indexes = {
            (value.name, tuple(column.name for column in value.columns), value.unique)
            for value in table.indexes
        }
        assert actual_indexes == expected_indexes
        actual_checks = {
            " ".join(value["sqltext"].split())
            for value in inspector.get_check_constraints(table_name)
        }
        expected_checks = {
            " ".join(str(value.sqltext).split())
            for value in table.constraints
            if isinstance(value, CheckConstraint)
        }
        assert actual_checks == expected_checks
    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0001"
        triggers = {
            row[0]
            for row in connection.execute(
                text("SELECT name FROM sqlite_master WHERE type='trigger'")
            )
        }
        occurred = connection.execute(
            text("SELECT occurred_at FROM audit_events WHERE case_id=:case_id"),
            {"case_id": str(harness.case_id)},
        ).scalar_one()
    assert triggers == {"audit_events_no_update", "audit_events_no_delete"}
    assert occurred == "2026-08-07T12:00:00.000000Z"
    assert harness.audit_events()[0].occurred_at == FROZEN_NOW
    with pytest.raises(DatabaseError, match="AUDIT_APPEND_ONLY"):
        with engine.begin() as connection:
            connection.execute(text("UPDATE audit_events SET command_name='changed'"))
    reopened = WorkflowService(clock=FrozenClock(FROZEN_NOW), database_url=harness.database_url)
    assert _public_reads(reopened, harness.case_id) == _public_reads(
        harness.service, harness.case_id
    )
