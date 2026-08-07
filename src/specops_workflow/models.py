from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from .enums import *  # noqa: F403

MAX_INT = 9_223_372_036_854_775_807
BoundedInt = Annotated[int, Field(strict=True, ge=1, le=MAX_INT)]
Revision = Annotated[int, Field(strict=True, ge=0, le=MAX_INT)]
Hash = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]
ShortText = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=255, strip_whitespace=False)]
Statement = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=20_000, strip_whitespace=False)]
RenderedText = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=250_000, strip_whitespace=False)]
Actor = UUID | Literal["SYSTEM"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, validate_assignment=True)

    @field_validator("*")
    @classmethod
    def reject_bad_strings(cls, value: Any) -> Any:
        if isinstance(value, str) and (value != value.strip() or "\x00" in value or any(ord(c) < 32 for c in value)):
            raise ValueError("strings must be trimmed and contain no ASCII control characters")
        return value


class ArtifactBinding(StrictModel):
    artifact_kind: ArtifactKind
    artifact_id: UUID
    version: BoundedInt
    semantic_hash: Hash


class JsonPointer(StrictModel):
    kind: Literal["JSON_POINTER"] = "JSON_POINTER"
    pointer: Annotated[str, StringConstraints(strict=True, max_length=4096)]


class LineRange(StrictModel):
    kind: Literal["LINE_RANGE"] = "LINE_RANGE"
    start: BoundedInt
    end: BoundedInt

    @model_validator(mode="after")
    def ordered(self) -> LineRange:
        if self.start > self.end: raise ValueError("start must not exceed end")
        return self


class WorkbookRange(StrictModel):
    kind: Literal["WORKBOOK_RANGE"] = "WORKBOOK_RANGE"
    sheet: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=31)]
    a1: ShortText


ExactLocation = Annotated[JsonPointer | LineRange | WorkbookRange, Field(discriminator="kind")]


class SourceRef(StrictModel):
    artifact_id: UUID
    version: BoundedInt
    content_hash: Hash
    location: ExactLocation


class SourceArtifactIdentity(StrictModel):
    artifact_id: UUID
    case_id: UUID
    type: SourceArtifactType
    version: BoundedInt
    media_type: Literal["application/json", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "text/markdown", "text/plain", "application/octet-stream"]
    canonical_locator: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=4096)]
    content_hash: Hash
    registered_at: datetime | None = None


class Requirement(StrictModel):
    unit_id: UUID
    statement: Statement
    domain: Domain
    delivery_required: bool
    source_refs: Annotated[list[SourceRef], Field(min_length=1, max_length=100)]


class TechnicalDecision(Requirement):
    domain: Literal[Domain.TECHNICAL, Domain.CROSS_DOMAIN]
    provisional: bool
    provisional_delegation_id: UUID | None = None

    @model_validator(mode="after")
    def provisional_shape(self) -> TechnicalDecision:
        if self.provisional != (self.provisional_delegation_id is not None):
            raise ValueError("provisional and provisional_delegation_id must agree")
        return self


class AcceptanceCheck(StrictModel):
    check_id: UUID
    statement: Statement
    domain: Domain
    related_unit_ids: Annotated[list[UUID], Field(min_length=1, max_length=100)]
    source_refs: Annotated[list[SourceRef], Field(min_length=1, max_length=100)]


class SpecPackagePayload(StrictModel):
    requirements: Annotated[list[Requirement], Field(min_length=1, max_length=500)]
    technical_decisions: Annotated[list[TechnicalDecision], Field(min_length=1, max_length=500)]
    acceptance_checks: Annotated[list[AcceptanceCheck], Field(min_length=1, max_length=500)]


class BodyAcceptanceCheck(StrictModel):
    check_id: UUID
    statement: Statement


class StructuredWorkBody(StrictModel):
    package_id: UUID
    package_version: BoundedInt
    package_hash: Hash
    generation_key: ShortText
    source_unit_ids: Annotated[list[UUID], Field(min_length=1, max_length=500)]
    acceptance_checks: Annotated[list[BodyAcceptanceCheck], Field(max_length=500)] = []
    dependency_item_ids: Annotated[list[UUID], Field(max_length=500)] = []
    provisional: bool
    jira_key: ShortText | None = None


class ProjectionItem(StrictModel):
    item_id: UUID
    kind: JiraKind | GitHubKind
    domain: Domain
    title: ShortText
    body: StructuredWorkBody
    source_unit_ids: Annotated[list[UUID], Field(min_length=1, max_length=500)]
    parent_item_id: UUID | None = None
    dependency_item_ids: Annotated[list[UUID], Field(max_length=500)] = []
    implementation_required: bool
    repository: ShortText | None = None
    primary_jira_item_id: UUID | None = None


class ProjectionPlanPayload(StrictModel):
    package_binding: ArtifactBinding
    target: PlanTarget
    project_key: ShortText | None = None
    jira_plan_binding: ArtifactBinding | None = None
    items: Annotated[list[ProjectionItem], Field(min_length=1, max_length=500)]


class NativeStatusMapping(StrictModel):
    mapping_id: UUID
    system: System
    native_status: ShortText
    normalized_status: JiraStatus | GitHubStatus


class StatusCondition(StrictModel):
    kind: ConditionKind
    jira_states: Annotated[list[JiraStatus], Field(max_length=5)] = []
    github_states: Annotated[list[GitHubStatus], Field(max_length=2)] = []


class StatusRule(StrictModel):
    rule_id: UUID
    target_system: System
    selector: Selector
    item_ids: Annotated[list[UUID], Field(max_length=500)] = []
    target_normalized_status: JiraStatus | GitHubStatus
    all_of: Annotated[list[StatusCondition], Field(min_length=1, max_length=4)]


class StatusPolicyPayload(StrictModel):
    mappings: Annotated[list[NativeStatusMapping], Field(min_length=1, max_length=500)]
    rules: Annotated[list[StatusRule], Field(min_length=1, max_length=1000)]


class JiraIdentity(StrictModel):
    key: Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Z][A-Z0-9]{1,9}-[1-9][0-9]{0,17}$")]


class GitHubIdentity(StrictModel):
    repository: ShortText
    issue_number: BoundedInt
    node_id: ShortText
    html_url: ShortText

    @model_validator(mode="after")
    def canonical_url(self) -> GitHubIdentity:
        if self.repository.lower() != self.repository or self.html_url != f"https://github.com/{self.repository}/issues/{self.issue_number}":
            raise ValueError("GitHub identity must use the canonical repository issue URL")
        return self


ExternalIdentity = JiraIdentity | GitHubIdentity


class RemoteObservation(StrictModel):
    observation_kind: ObservationKind
    system: System
    external_identity: ExternalIdentity
    generation_key: ShortText
    package_binding: ArtifactBinding
    plan_binding: ArtifactBinding
    jira_plan_binding: ArtifactBinding | None = None
    status_policy_binding: ArtifactBinding
    remote_revision: ShortText | None = None
    expected_previous_remote_revision: ShortText | None = None
    native_status: ShortText | None = None
    owned_content: dict[str, Any] | None = None

    @model_validator(mode="after")
    def closed_shape(self) -> RemoteObservation:
        if self.observation_kind == ObservationKind.FOUND:
            if self.remote_revision is None or self.native_status is None or self.owned_content is None: raise ValueError("FOUND requires revision, status, and owned content")
        elif self.remote_revision is not None or self.native_status is not None or self.owned_content is not None:
            raise ValueError("NOT_FOUND stores no remote revision, status, or owned content")
        return self


class CommandBase(StrictModel):
    command_id: UUID
    case_id: UUID
    acting_actor_id: Actor
    expected_case_revision: Revision


class CreateCaseCommand(StrictModel):
    command_id: UUID
    case_id: UUID
    acting_actor_id: UUID
    pm_actor_id: UUID
    dev_lead_actor_id: UUID


class AddParticipantCommand(CommandBase): actor_id: UUID
class GrantDelegationCommand(CommandBase):
    delegation_id: UUID; delegate_id: UUID; domain: Literal[Domain.BUSINESS, Domain.TECHNICAL]
    command_names: Annotated[list[ShortText], Field(min_length=1, max_length=25)]
    artifact_kind: ArtifactKind | None = None; artifact_id: UUID | None = None
    valid_from: datetime; valid_until: datetime; later_review_required: bool
class RevokeDelegationCommand(CommandBase): delegation_id: UUID
class RegisterSourceArtifactCommand(CommandBase): identity: SourceArtifactIdentity
class RecordAmbiguityFindingCommand(CommandBase):
    finding_id: UUID; category: AmbiguityCategory; domain: Domain; severity: Severity
    evidence_refs: Annotated[list[SourceRef], Field(min_length=1, max_length=100)]; clarification_question: Statement
class ResolveAmbiguityFindingCommand(CommandBase):
    finding_id: UUID; resolution_text: Statement; resolution_source_refs: Annotated[list[SourceRef], Field(min_length=1, max_length=100)]
class ArtifactCommand(CommandBase):
    expected_artifact_id: UUID; expected_artifact_version: BoundedInt; expected_artifact_hash: Hash
class CreateSpecPackageCommand(CommandBase):
    package_id: UUID; content_schema_version: BoundedInt; hash_schema_version: BoundedInt; payload: SpecPackagePayload
class ReviseSpecPackageCommand(ArtifactCommand):
    content_schema_version: BoundedInt; hash_schema_version: BoundedInt; payload: SpecPackagePayload
class MarkSpecPackageReadyCommand(ArtifactCommand): pass
class ApproveSpecPackageCommand(ArtifactCommand): scope: ApprovalScope
class CreateProjectionPlanCommand(CommandBase):
    plan_id: UUID; content_schema_version: BoundedInt; hash_schema_version: BoundedInt; payload: ProjectionPlanPayload
class ReviseProjectionPlanCommand(ArtifactCommand):
    content_schema_version: BoundedInt; hash_schema_version: BoundedInt; payload: ProjectionPlanPayload
class ApproveProjectionPlanCommand(ArtifactCommand): scope: ApprovalScope
class CreateStatusPolicyCommand(CommandBase):
    policy_id: UUID; content_schema_version: BoundedInt; hash_schema_version: BoundedInt; payload: StatusPolicyPayload
class ReviseStatusPolicyCommand(ArtifactCommand):
    content_schema_version: BoundedInt; hash_schema_version: BoundedInt; payload: StatusPolicyPayload
class ApproveStatusPolicyCommand(ArtifactCommand): scope: ApprovalScope
class StartExternalOperationCommand(CommandBase): operation_id: UUID; intent_id: UUID; idempotency_key: ShortText


class ExplicitFailure(StrictModel): outcome: Literal[ResultOutcome.EXPLICIT_FAILURE]; failure_code: ShortText
class NominalSuccess(StrictModel): outcome: Literal[ResultOutcome.NOMINAL_SUCCESS]; read_back: RemoteObservation | None = None
OperationOutcome = Annotated[ExplicitFailure | NominalSuccess, Field(discriminator="outcome")]
class RecordOperationResultCommand(CommandBase): operation_id: UUID; attempt: BoundedInt; outcome: OperationOutcome
class ReconcileOperationCommand(CommandBase): operation_id: UUID; attempt: BoundedInt; observation: RemoteObservation
class SubmitRemoteSnapshotCommand(CommandBase): observation: RemoteObservation


class Receipt(StrictModel): case_id: UUID; revision: BoundedInt; command_id: UUID; occurred_at: datetime
class CaseResult(StrictModel): case_id: UUID; pm_actor_id: UUID; dev_lead_actor_id: UUID; receipt: Receipt
class ParticipantResult(StrictModel): case_id: UUID; actor_id: UUID; receipt: Receipt
class DelegationResult(StrictModel): case_id: UUID; delegation_id: UUID; revoked_at: datetime | None; receipt: Receipt
class SourceArtifactResult(StrictModel): identity: SourceArtifactIdentity; receipt: Receipt
class ResolutionRecord(StrictModel):
    resolution_id: UUID; scope: ApprovalScope; actor_id: UUID; delegation_id: UUID | None = None; delegator_id: UUID | None = None
    later_review_required: bool = False; resolution_text: Statement; resolution_source_refs: list[SourceRef]; recorded_at: datetime
    reviewed_by: UUID | None = None; reviewed_at: datetime | None = None
class AmbiguityFindingResult(StrictModel):
    finding_id: UUID; status: FindingStatus; resolutions: list[ResolutionRecord]; resolved_at: datetime | None; receipt: Receipt
class SpecPackageResult(StrictModel): binding: ArtifactBinding; state: PackageState; receipt: Receipt
class ProjectionPlanResult(StrictModel): binding: ArtifactBinding; target: PlanTarget; state: PlanState; derived_intent_ids: list[UUID] = []; receipt: Receipt
class StatusPolicyResult(StrictModel): binding: ArtifactBinding; state: PolicyState; receipt: Receipt
class ApprovalResult(StrictModel): approval_id: UUID; scope: ApprovalScope; artifact_binding: ArtifactBinding; receipt: Receipt
class OperationResult(StrictModel):
    operation_id: UUID; intent_id: UUID; system: System; action: Action; status: OperationStatus; attempt: BoundedInt
    failure_code: ShortText | None = None; confirmation: RemoteObservation | None = None; confirmed_snapshot_sequence: BoundedInt | None = None; receipt: Receipt
class SnapshotResult(StrictModel):
    observation_kind: Literal["FOUND_ACCEPTED", "NOT_FOUND_ACCEPTED"]; observation: RemoteObservation
    binding_id: UUID; observation_sequence: BoundedInt; active_finding_ids: list[UUID]; receipt: Receipt
class ReplayResult(StrictModel): kind: Literal["MUTATED"] = "MUTATED"; result: dict[str, Any]


class QueryOne(StrictModel): case_id: UUID; acting_actor_id: Actor
class AuditQuery(QueryOne): limit: Annotated[int, Field(strict=True, ge=1, le=500)] = 100; after_cursor: BoundedInt | None = None
class UUIDListQuery(QueryOne): limit: Annotated[int, Field(strict=True, ge=1, le=500)] = 100; after_cursor: UUID | None = None
class WorkflowView(StrictModel):
    case_id: UUID; revision: Revision; current_package: ArtifactBinding | None = None; current_jira_plan: ArtifactBinding | None = None
    current_github_plan: ArtifactBinding | None = None; current_status_policy: ArtifactBinding | None = None; setup_stage: SetupStage
    foundation_ready: bool; workflow_health: WorkflowHealth; blocker_ids: list[UUID] = []; pending_later_review_ids: list[UUID] = []
class DeliveryItem(StrictModel):
    system: System; plan_id: UUID; plan_version: BoundedInt; item_id: UUID; external_identity: ExternalIdentity | None = None
    normalized_status: JiraStatus | GitHubStatus; lifecycle_state: Lifecycle; remote_revision: ShortText | None = None; pending_intent_ids: list[UUID] = []
class DeliveryView(StrictModel): delivery_state: DeliveryState; delivery_complete: bool; items: Annotated[list[DeliveryItem], Field(max_length=2000)]
class TraceEndpoint(StrictModel): kind: ShortText; id: UUID; version: BoundedInt | None = None
class TraceEdge(StrictModel): id: UUID; edge_type: ShortText; from_endpoint: TraceEndpoint; to_endpoint: TraceEndpoint
class TraceabilityMap(StrictModel): case_id: UUID; complete: bool; edges: list[TraceEdge]
class DriftFinding(StrictModel):
    id: UUID; case_id: UUID; category: FindingCategory; affected: dict[str, Any]; expected_value: Any; observed_value: Any
    active: bool; created_at: datetime; resolved_at: datetime | None = None
class AuditEvent(StrictModel):
    event_id: UUID; case_id: UUID; case_sequence: BoundedInt; command_id: UUID; command_name: ShortText; command_fingerprint: Hash
    actor: Actor; occurred_at: datetime; target_ids: list[UUID]; before_case_revision: Revision; after_case_revision: BoundedInt
    metadata: dict[str, Any]; result: dict[str, Any]
class OperationIntent(StrictModel):
    intent_id: UUID; system: System; item_ref: dict[str, Any]; action: Action; request: dict[str, Any]
    package_binding: ArtifactBinding; plan_binding: ArtifactBinding; jira_plan_binding: ArtifactBinding | None = None
    status_policy_binding: ArtifactBinding; request_owned_content_hash: Hash; fingerprint: Hash
    existing: ExternalIdentity | None = None; expected: ShortText | None = None
    target_normalized_status: JiraStatus | GitHubStatus | None = None; contributing_rule_ids: list[UUID] = []
class AuditPage(StrictModel): items: list[AuditEvent]; next_cursor: BoundedInt | None = None
class FindingPage(StrictModel): items: list[DriftFinding]; next_cursor: UUID | None = None
class IntentPage(StrictModel): items: list[OperationIntent]; next_cursor: UUID | None = None

COMMAND_MODELS = {
    c.__name__: c for c in (
        CreateCaseCommand, AddParticipantCommand, GrantDelegationCommand, RevokeDelegationCommand,
        RegisterSourceArtifactCommand, RecordAmbiguityFindingCommand, ResolveAmbiguityFindingCommand,
        CreateSpecPackageCommand, ReviseSpecPackageCommand, MarkSpecPackageReadyCommand, ApproveSpecPackageCommand,
        CreateProjectionPlanCommand, ReviseProjectionPlanCommand, ApproveProjectionPlanCommand,
        CreateStatusPolicyCommand, ReviseStatusPolicyCommand, ApproveStatusPolicyCommand,
        StartExternalOperationCommand, RecordOperationResultCommand, ReconcileOperationCommand, SubmitRemoteSnapshotCommand,
    )
}
