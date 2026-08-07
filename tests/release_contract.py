from __future__ import annotations

from typing import Any

from specops_workflow.canonical import canonical_data
from specops_workflow.errors import ErrorCode
from specops_workflow.models import (
    AddParticipantCommand,
    AmbiguityFindingResult,
    ApprovalResult,
    ApproveProjectionPlanCommand,
    ApproveSpecPackageCommand,
    ApproveStatusPolicyCommand,
    AuditPage,
    CaseResult,
    CreateCaseCommand,
    CreateProjectionPlanCommand,
    CreateSpecPackageCommand,
    CreateStatusPolicyCommand,
    DelegationResult,
    DeliveryView,
    FindingPage,
    GrantDelegationCommand,
    IntentPage,
    MarkSpecPackageReadyCommand,
    OperationResult,
    ParticipantResult,
    ProjectionPlanResult,
    RecordAmbiguityFindingCommand,
    RecordOperationResultCommand,
    RegisterSourceArtifactCommand,
    ReconcileOperationCommand,
    ReplayResult,
    ResolveAmbiguityFindingCommand,
    RevokeDelegationCommand,
    ReviseProjectionPlanCommand,
    ReviseSpecPackageCommand,
    ReviseStatusPolicyCommand,
    SnapshotResult,
    SourceArtifactResult,
    SpecPackageResult,
    StartExternalOperationCommand,
    StatusPolicyResult,
    SubmitRemoteSnapshotCommand,
    TraceabilityMap,
    WorkflowView,
)


EV_IDS = tuple(f"EV-{number:03d}" for number in range(1, 43))

COMMAND_MODELS = (
    CreateCaseCommand,
    AddParticipantCommand,
    GrantDelegationCommand,
    RevokeDelegationCommand,
    RegisterSourceArtifactCommand,
    RecordAmbiguityFindingCommand,
    ResolveAmbiguityFindingCommand,
    CreateSpecPackageCommand,
    ReviseSpecPackageCommand,
    MarkSpecPackageReadyCommand,
    ApproveSpecPackageCommand,
    CreateProjectionPlanCommand,
    ReviseProjectionPlanCommand,
    ApproveProjectionPlanCommand,
    CreateStatusPolicyCommand,
    ReviseStatusPolicyCommand,
    ApproveStatusPolicyCommand,
    StartExternalOperationCommand,
    RecordOperationResultCommand,
    ReconcileOperationCommand,
    SubmitRemoteSnapshotCommand,
)

MUTATION_RESULT_MODELS = (
    CaseResult,
    ParticipantResult,
    DelegationResult,
    SourceArtifactResult,
    AmbiguityFindingResult,
    SpecPackageResult,
    ProjectionPlanResult,
    StatusPolicyResult,
    ApprovalResult,
    OperationResult,
    SnapshotResult,
)

READ_MODELS = (
    WorkflowView,
    DeliveryView,
    TraceabilityMap,
    FindingPage,
    AuditPage,
    IntentPage,
)

PUBLIC_SCHEMA_MODELS = (*COMMAND_MODELS, *MUTATION_RESULT_MODELS, *READ_MODELS, ReplayResult)

AUTHORIZATION_ROWS = (
    "create_case",
    "add_participant",
    "grant_or_revoke_delegation",
    "register_source_or_record_ambiguity",
    "resolve_ambiguity",
    "draft_package_or_projection",
    "approve_artifact",
    "create_or_revise_status_policy",
    "external_callback",
    "read_model",
)

OPERATION_BRANCHES = (
    "first_attempt_pending",
    "pending_retry_blocked",
    "failed_exact_retry",
    "failed_changed_fingerprint_conflict",
    "unknown_retry_blocked",
    "unknown_reconcile_found",
    "unknown_reconcile_not_found",
    "succeeded_exact_command_replay",
    "succeeded_readback_reconcile",
    "idempotency_key_conflict",
    "operation_id_conflict",
    "intent_id_conflict",
)


def schema_snapshot() -> dict[str, Any]:
    return {
        model.__name__: canonical_data(model.model_json_schema(mode="validation"))
        for model in PUBLIC_SCHEMA_MODELS
    }


def release_manifest() -> dict[str, Any]:
    return {
        "ev_ids": list(EV_IDS),
        "command_schemas": [model.__name__ for model in COMMAND_MODELS],
        "mutation_result_schemas": [model.__name__ for model in MUTATION_RESULT_MODELS],
        "read_schemas": [model.__name__ for model in READ_MODELS],
        "generic_replay_schema": ReplayResult.__name__,
        "stable_failure_codes": [item.value for item in ErrorCode],
        "rejecting_fixtures": {item.value: f"reject_{item.value.lower()}" for item in ErrorCode},
        "authorization_counterparts": list(AUTHORIZATION_ROWS),
        "operation_branches": list(OPERATION_BRANCHES),
        "accepted_rejecting_fixtures": 0,
        "duplicate_operations": 0,
        "duplicate_bindings": 0,
    }
