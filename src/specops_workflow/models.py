from __future__ import annotations

import re
import unicodedata
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
        if type(value) is str:
            if value != value.strip() or "\x00" in value or any(ord(c) < 32 for c in value):
                raise ValueError("strings must be trimmed and contain no ASCII control characters")
            value = unicodedata.normalize("NFC", value)
        return value


class ArtifactBinding(StrictModel):
    artifact_kind: ArtifactKind
    artifact_id: UUID
    version: BoundedInt
    semantic_hash: Hash


class JsonPointer(StrictModel):
    kind: Literal["JSON_POINTER"] = "JSON_POINTER"
    pointer: Annotated[str, StringConstraints(strict=True, max_length=4096)]

    @field_validator("pointer")
    @classmethod
    def valid_rfc6901(cls, value: str) -> str:
        if value and not value.startswith("/"):
            raise ValueError("JSON Pointer must be empty or begin with /")
        if not re.fullmatch(r"(?:[^~]|~[01])*", value):
            raise ValueError("invalid RFC 6901 escape")
        return value


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

    @model_validator(mode="after")
    def valid_workbook_range(self) -> WorkbookRange:
        if any(char in self.sheet for char in "[]:*?/\\"):
            raise ValueError("invalid worksheet name")
        match = re.fullmatch(r"([A-Z]{1,3})([1-9][0-9]{0,6})(?::([A-Z]{1,3})([1-9][0-9]{0,6}))?", self.a1)
        if match is None:
            raise ValueError("invalid A1 range")

        def column_number(column: str) -> int:
            result = 0
            for char in column:
                result = result * 26 + ord(char) - 64
            return result

        first_col, first_row = column_number(match[1]), int(match[2])
        last_col = column_number(match[3]) if match[3] else first_col
        last_row = int(match[4]) if match[4] else first_row
        if first_col > 16_384 or last_col > 16_384 or first_row > 1_048_576 or last_row > 1_048_576:
            raise ValueError("A1 range exceeds worksheet bounds")
        if last_row < first_row or last_col < first_col:
            raise ValueError("A1 range must be ordered")
        return self


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

    @field_validator("canonical_locator")
    @classmethod
    def canonical_posix_locator(cls, value: str) -> str:
        if not value.startswith("/") or (value != "/" and value.endswith("/")):
            raise ValueError("locator must be an absolute canonical POSIX path")
        if value == "/":
            return value
        segments = value.split("/")[1:]
        if any(segment in {"", ".", ".."} for segment in segments):
            raise ValueError("locator contains a non-canonical segment")
        return value


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

    @model_validator(mode="after")
    def unique_content_ids(self) -> SpecPackagePayload:
        values = [item.unit_id for item in self.requirements]
        values += [item.unit_id for item in self.technical_decisions]
        values += [item.check_id for item in self.acceptance_checks]
        if len(values) != len(set(values)):
            raise ValueError("package content IDs must be unique")
        unit_ids = {item.unit_id for item in [*self.requirements, *self.technical_decisions]}
        for check in self.acceptance_checks:
            if len(check.related_unit_ids) != len(set(check.related_unit_ids)) or not set(check.related_unit_ids).issubset(unit_ids):
                raise ValueError("acceptance check references must be unique existing units")
        return self


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

    @model_validator(mode="after")
    def unique_body_ids(self) -> StructuredWorkBody:
        if len(self.source_unit_ids) != len(set(self.source_unit_ids)):
            raise ValueError("source unit IDs must be unique")
        if len(self.dependency_item_ids) != len(set(self.dependency_item_ids)):
            raise ValueError("dependency item IDs must be unique")
        check_ids = [item.check_id for item in self.acceptance_checks]
        if len(check_ids) != len(set(check_ids)):
            raise ValueError("acceptance check IDs must be unique")
        return self


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

    @model_validator(mode="after")
    def target_shape(self) -> ProjectionPlanPayload:
        item_ids = [item.item_id for item in self.items]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("projection item IDs must be unique")
        if self.target == PlanTarget.JIRA:
            if self.project_key is None or not re.fullmatch(r"[A-Z][A-Z0-9]{1,9}", self.project_key) or self.jira_plan_binding is not None:
                raise ValueError("Jira plan requires only a canonical project key")
        elif self.project_key is not None or self.jira_plan_binding is None:
            raise ValueError("GitHub plan requires only a Jira-plan binding")
        return self


class NativeStatusMapping(StrictModel):
    mapping_id: UUID
    system: System
    native_status: ShortText
    normalized_status: JiraStatus | GitHubStatus

    @model_validator(mode="after")
    def matching_system(self) -> NativeStatusMapping:
        if self.system == System.JIRA and not isinstance(self.normalized_status, JiraStatus):
            raise ValueError("Jira mapping requires JiraStatus")
        if self.system == System.GITHUB and not isinstance(self.normalized_status, GitHubStatus):
            raise ValueError("GitHub mapping requires GitHubStatus")
        return self


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

    @model_validator(mode="after")
    def closed_rule_shape(self) -> StatusRule:
        if (self.selector == Selector.ITEM_IDS) != bool(self.item_ids):
            raise ValueError("ITEM_IDS selector requires nonempty item_ids only")
        if len(self.item_ids) != len(set(self.item_ids)):
            raise ValueError("item_ids must be unique")
        if self.target_system == System.JIRA:
            if not isinstance(self.target_normalized_status, JiraStatus) or self.target_normalized_status == JiraStatus.UNKNOWN:
                raise ValueError("Jira rule requires a non-UNKNOWN JiraStatus")
        elif not isinstance(self.target_normalized_status, GitHubStatus) or self.target_normalized_status == GitHubStatus.UNKNOWN:
            raise ValueError("GitHub rule requires a non-UNKNOWN GitHubStatus")
        condition_keys = [condition.model_dump_json() for condition in self.all_of]
        if len(condition_keys) != len(set(condition_keys)):
            raise ValueError("status conditions must be unique")
        return self


class StatusPolicyPayload(StrictModel):
    mappings: Annotated[list[NativeStatusMapping], Field(min_length=1, max_length=500)]
    rules: Annotated[list[StatusRule], Field(min_length=1, max_length=1000)]

    @model_validator(mode="after")
    def unique_policy_rows(self) -> StatusPolicyPayload:
        mapping_ids = [item.mapping_id for item in self.mappings]
        rule_ids = [item.rule_id for item in self.rules]
        pairs = [(item.system, item.native_status) for item in self.mappings]
        if len(mapping_ids) != len(set(mapping_ids)) or len(rule_ids) != len(set(rule_ids)) or len(pairs) != len(set(pairs)):
            raise ValueError("status mappings and rules must be unique")
        return self


class JiraIdentity(StrictModel):
    key: Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Z][A-Z0-9]{1,9}-[1-9][0-9]{0,17}$")]

    @field_validator("key")
    @classmethod
    def reject_render_sentinel(cls, value: str) -> str:
        if value == "AAAAAAAAAA-999999999999999999":
            raise ValueError("render-only Jira sizing sentinel is not an external identity")
        return value


class GitHubIdentity(StrictModel):
    repository: ShortText
    issue_number: BoundedInt
    node_id: ShortText
    html_url: ShortText

    @model_validator(mode="after")
    def canonical_url(self) -> GitHubIdentity:
        repository_pattern = r"^[a-z0-9](?:[a-z0-9-]{0,37}[a-z0-9])?/[a-z0-9._-]{1,100}$"
        if not re.fullmatch(repository_pattern, self.repository) or self.html_url != f"https://github.com/{self.repository}/issues/{self.issue_number}":
            raise ValueError("GitHub identity must use the canonical repository issue URL")
        if self.repository == "placeholder/placeholder" or self.node_id == "placeholder":
            raise ValueError("placeholder GitHub identity is not confirmable")
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

    @model_validator(mode="after")
    def closed_delegation_shape(self) -> GrantDelegationCommand:
        if self.valid_from.tzinfo is None or self.valid_until.tzinfo is None or self.valid_from > self.valid_until:
            raise ValueError("delegation bounds must be ordered timezone-aware instants")
        if len(self.command_names) != len(set(self.command_names)):
            raise ValueError("delegated commands must be unique")
        if (self.artifact_kind is None) != (self.artifact_id is None):
            raise ValueError("artifact kind and ID must be supplied together")
        return self
class RevokeDelegationCommand(CommandBase): delegation_id: UUID
class RegisterSourceArtifactCommand(CommandBase):
    identity: SourceArtifactIdentity

    @model_validator(mode="after")
    def unregistered_identity(self) -> RegisterSourceArtifactCommand:
        if self.identity.registered_at is not None:
            raise ValueError("registered_at is service generated")
        return self
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
class MutatedResult(StrictModel): kind: Literal["MUTATED"] = "MUTATED"; mutated: Literal[True] = True
class CaseResult(MutatedResult): case_id: UUID; pm_actor_id: UUID; dev_lead_actor_id: UUID; receipt: Receipt
class ParticipantResult(MutatedResult): case_id: UUID; actor_id: UUID; receipt: Receipt
class DelegationResult(MutatedResult): case_id: UUID; delegation_id: UUID; revoked_at: datetime | None; receipt: Receipt
class SourceArtifactResult(MutatedResult): identity: SourceArtifactIdentity; receipt: Receipt
class ResolutionRecord(StrictModel):
    resolution_id: UUID; scope: ApprovalScope; actor_id: UUID; delegation_id: UUID | None = None; delegator_id: UUID | None = None
    later_review_required: bool = False; resolution_text: Statement; resolution_source_refs: list[SourceRef]; recorded_at: datetime
    reviewed_by: UUID | None = None; reviewed_at: datetime | None = None
class AmbiguityFindingResult(MutatedResult):
    finding_id: UUID; status: FindingStatus; resolutions: list[ResolutionRecord]; resolved_at: datetime | None; receipt: Receipt
class SpecPackageResult(MutatedResult): binding: ArtifactBinding; state: PackageState; receipt: Receipt
class ProjectionPlanResult(MutatedResult): binding: ArtifactBinding; target: PlanTarget; state: PlanState; derived_intent_ids: list[UUID] = []; receipt: Receipt
class StatusPolicyResult(MutatedResult): binding: ArtifactBinding; state: PolicyState; receipt: Receipt
class ApprovalResult(MutatedResult): approval_id: UUID; scope: ApprovalScope; artifact_binding: ArtifactBinding; receipt: Receipt
class OperationResult(MutatedResult):
    operation_id: UUID; intent_id: UUID; system: System; action: Action; status: OperationStatus; attempt: BoundedInt
    failure_code: ShortText | None = None; confirmation: RemoteObservation | None = None; confirmed_snapshot_sequence: BoundedInt | None = None; receipt: Receipt
class SnapshotResult(MutatedResult):
    observation_kind: Literal["FOUND_ACCEPTED", "NOT_FOUND_ACCEPTED"]; observation: RemoteObservation
    binding_id: UUID; observation_sequence: BoundedInt; active_finding_ids: list[UUID]; receipt: Receipt
StoredMutationResult = (
    CaseResult | ParticipantResult | DelegationResult | SourceArtifactResult | AmbiguityFindingResult |
    SpecPackageResult | ProjectionPlanResult | StatusPolicyResult | ApprovalResult | OperationResult | SnapshotResult
)
class ReplayResult(StrictModel):
    kind: Literal["REPLAY"] = "REPLAY"
    mutated: Literal[False] = False
    stored_result: StoredMutationResult
    receipt: None = None


class QueryOne(StrictModel): case_id: UUID; acting_actor_id: Actor
class AuditQuery(QueryOne): limit: Annotated[int, Field(strict=True, ge=1, le=500)] = 100; after_cursor: BoundedInt | None = None
class UUIDListQuery(QueryOne): limit: Annotated[int, Field(strict=True, ge=1, le=500)] = 100; after_cursor: UUID | None = None
class OperationAttemptQuery(QueryOne):
    limit: Annotated[int, Field(strict=True, ge=1, le=500)] = 100
    after_cursor: Annotated[str, Field(strict=True, min_length=38, max_length=64, pattern=r"^[0-9a-f-]{36}:[1-9][0-9]*$")] | None = None
class WorkflowView(StrictModel):
    case_id: UUID; revision: Revision; current_package: ArtifactBinding | None = None; current_jira_plan: ArtifactBinding | None = None
    current_github_plan: ArtifactBinding | None = None; current_status_policy: ArtifactBinding | None = None; setup_stage: SetupStage
    foundation_ready: bool; workflow_health: WorkflowHealth; blocker_ids: list[UUID] = []; pending_later_review_ids: list[UUID] = []
class DeliveryItem(StrictModel):
    system: System; plan_id: UUID; plan_version: BoundedInt; item_id: UUID; external_identity: ExternalIdentity | None = None
    normalized_status: JiraStatus | GitHubStatus; lifecycle_state: Lifecycle; remote_revision: ShortText | None = None; pending_intent_ids: list[UUID] = []
class DeliveryView(StrictModel): delivery_state: DeliveryState; delivery_complete: bool; items: Annotated[list[DeliveryItem], Field(max_length=2000)]
class TraceEndpoint(StrictModel): kind: TraceNodeKind; id: UUID; version: BoundedInt | None = None
class TraceEdge(StrictModel): id: UUID; edge_type: TraceEdgeType; from_endpoint: TraceEndpoint; to_endpoint: TraceEndpoint
class TraceabilityMap(StrictModel): case_id: UUID; complete: bool; edges: list[TraceEdge]
class EntityValue(StrictModel): kind: Literal["ENTITY"] = "ENTITY"; entity_kind: EntityKind; id: UUID
class BindingValue(StrictModel): kind: Literal["BINDING"] = "BINDING"; value: ArtifactBinding
class ContentValue(StrictModel): kind: Literal["CONTENT"] = "CONTENT"; value: Hash
class StatusValue(StrictModel):
    kind: Literal["STATUS"] = "STATUS"; system: System; value: JiraStatus | GitHubStatus

    @model_validator(mode="after")
    def status_matches_system(self):
        if (self.system == System.JIRA) != isinstance(self.value, JiraStatus):
            raise ValueError("status enum must match system")
        return self
class StatusSetValue(StrictModel):
    kind: Literal["STATUS_SET"] = "STATUS_SET"; system: System
    values: Annotated[list[JiraStatus | GitHubStatus], Field(min_length=2, max_length=5)]

    @model_validator(mode="after")
    def closed_unique_enum_order(self):
        enum_type = JiraStatus if self.system == System.JIRA else GitHubStatus
        if any(not isinstance(value, enum_type) for value in self.values):
            raise ValueError("status enum must match system")
        if len(set(self.values)) != len(self.values) or self.values != sorted(self.values, key=lambda value: list(enum_type).index(value)):
            raise ValueError("statuses must be unique and in enum order")
        return self
class LifecycleValue(StrictModel): kind: Literal["LIFECYCLE"] = "LIFECYCLE"; value: Lifecycle
class NativeMappingKeyValue(StrictModel): kind: Literal["MAPPING_KEY"] = "MAPPING_KEY"; system: System; native_status: ShortText
class OperationStateValue(StrictModel): kind: Literal["OPERATION_STATE"] = "OPERATION_STATE"; value: OperationStatus
class FindingStateValue(StrictModel): kind: Literal["FINDING_STATE"] = "FINDING_STATE"; value: FindingStatus
class ResolutionReviewValue(StrictModel):
    kind: Literal["RESOLUTION_REVIEW"] = "RESOLUTION_REVIEW"; finding_id: UUID; scope: ApprovalScope; delegation_id: UUID; reviewed: bool
class ExternalValue(StrictModel): kind: Literal["EXTERNAL"] = "EXTERNAL"; value: ExternalIdentity
class MissingValue(StrictModel): kind: Literal["MISSING"] = "MISSING"
class TraceValue(StrictModel): kind: Literal["TRACE"] = "TRACE"; value: TraceEdge
class ApprovalValue(StrictModel):
    kind: Literal["APPROVAL"] = "APPROVAL"; binding: ArtifactBinding; scope: ApprovalScope; actor_id: UUID; delegation_id: UUID | None = None
FindingValue = Annotated[
    EntityValue | BindingValue | ContentValue | StatusValue | StatusSetValue | LifecycleValue |
    NativeMappingKeyValue | OperationStateValue | FindingStateValue | ResolutionReviewValue |
    ExternalValue | MissingValue | TraceValue | ApprovalValue,
    Field(discriminator="kind"),
]
class DriftFinding(StrictModel):
    id: UUID; case_id: UUID; category: FindingCategory; affected: FindingValue; expected_value: FindingValue; observed_value: FindingValue
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
class ApprovalRecord(StrictModel):
    approval_id: UUID; artifact_binding: ArtifactBinding; scope: ApprovalScope; actor_id: UUID
    delegation_id: UUID | None = None; delegator_id: UUID | None = None; later_review_required: bool; approved_at: datetime
class OperationAttemptRecord(StrictModel):
    operation_id: UUID; attempt: BoundedInt; intent_id: UUID; system: System; action: Action; idempotency_key: ShortText
    request: dict[str, Any]; expected_remote_revision: ShortText | None = None; status: OperationStatus
    failure_code: ShortText | None = None; result: OperationResult | None = None; started_at: datetime; completed_at: datetime | None = None
class AuditPage(StrictModel): items: list[AuditEvent]; next_cursor: BoundedInt | None = None
class FindingPage(StrictModel): items: list[DriftFinding]; next_cursor: UUID | None = None
class IntentPage(StrictModel): items: list[OperationIntent]; next_cursor: UUID | None = None
class ApprovalPage(StrictModel): items: list[ApprovalRecord]; next_cursor: UUID | None = None
class OperationAttemptPage(StrictModel): items: list[OperationAttemptRecord]; next_cursor: ShortText | None = None

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
