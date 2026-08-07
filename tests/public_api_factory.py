from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from specops_workflow import FrozenClock, WorkflowService, migrate
from specops_workflow.enums import (
    ApprovalScope,
    ConditionKind,
    Domain,
    GitHubKind,
    GitHubStatus,
    JiraKind,
    JiraStatus,
    ObservationKind,
    PlanTarget,
    ResultOutcome,
    Selector,
    SourceArtifactType,
    System,
)
from specops_workflow.models import (
    AcceptanceCheck,
    ApproveProjectionPlanCommand,
    ApproveSpecPackageCommand,
    ApproveStatusPolicyCommand,
    ArtifactBinding,
    AuditQuery,
    BodyAcceptanceCheck,
    CreateCaseCommand,
    CreateProjectionPlanCommand,
    CreateSpecPackageCommand,
    CreateStatusPolicyCommand,
    GitHubIdentity,
    JiraIdentity,
    JsonPointer,
    MarkSpecPackageReadyCommand,
    NativeStatusMapping,
    NominalSuccess,
    OperationIntent,
    ProjectionItem,
    ProjectionPlanPayload,
    QueryOne,
    RecordOperationResultCommand,
    RegisterSourceArtifactCommand,
    RemoteObservation,
    Requirement,
    ReviseStatusPolicyCommand,
    SourceArtifactIdentity,
    SourceRef,
    SpecPackagePayload,
    StartExternalOperationCommand,
    StatusCondition,
    StatusPolicyPayload,
    StatusRule,
    StructuredWorkBody,
    TechnicalDecision,
    UUIDListQuery,
)


FROZEN_NOW = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
SOURCE_HASH = "0" * 64


class DeterministicUuidSequence:
    """Repeatable authored/service IDs with RFC-4122 version/variant bits."""

    def __init__(self, *, prefix: int) -> None:
        self._prefix = prefix
        self._counter = 0

    def new(self) -> UUID:
        self._counter += 1
        return UUID(f"{self._prefix:08x}-0000-4000-8000-{self._counter:012x}")


@dataclass
class PublicWorkflowHarness:
    service: WorkflowService
    database_url: str
    database_path: Path
    authored_ids: DeterministicUuidSequence
    case_id: UUID
    pm_actor_id: UUID
    dev_lead_actor_id: UUID
    source_ref: SourceRef | None = None
    package_payload: SpecPackagePayload | None = None
    package_binding: ArtifactBinding | None = None
    jira_payload: ProjectionPlanPayload | None = None
    jira_binding: ArtifactBinding | None = None
    status_payload: StatusPolicyPayload | None = None
    status_binding: ArtifactBinding | None = None
    github_payload: ProjectionPlanPayload | None = None
    github_binding: ArtifactBinding | None = None
    successful_command_ids: list[UUID] = field(default_factory=list)

    def id(self) -> UUID:
        return self.authored_ids.new()

    @property
    def revision(self) -> int:
        return self.service.get_workflow_view(
            QueryOne(case_id=self.case_id, acting_actor_id=self.pm_actor_id)
        ).revision

    def record(self, result):
        self.successful_command_ids.append(result.receipt.command_id)
        return result

    def build_approved_package(self) -> ArtifactBinding:
        source_id = self.id()
        registered = self.record(
            self.service.register_source_artifact(
                RegisterSourceArtifactCommand(
                    command_id=self.id(),
                    case_id=self.case_id,
                    acting_actor_id="SYSTEM",
                    expected_case_revision=self.revision,
                    identity=SourceArtifactIdentity(
                        artifact_id=source_id,
                        case_id=self.case_id,
                        type=SourceArtifactType.OTHER,
                        version=1,
                        media_type="application/json",
                        canonical_locator="/registered/evidence.json",
                        content_hash=SOURCE_HASH,
                    ),
                )
            )
        )
        self.source_ref = SourceRef(
            artifact_id=source_id,
            version=1,
            content_hash=SOURCE_HASH,
            location=JsonPointer(pointer="/units/0"),
        )
        requirement_id, decision_id, check_id = self.id(), self.id(), self.id()
        self.package_payload = SpecPackagePayload(
            requirements=[
                Requirement(
                    unit_id=requirement_id,
                    statement="Record an exact approved outcome",
                    domain=Domain.BUSINESS,
                    delivery_required=True,
                    source_refs=[self.source_ref],
                )
            ],
            technical_decisions=[
                TechnicalDecision(
                    unit_id=decision_id,
                    statement="Use deterministic state transitions",
                    domain=Domain.TECHNICAL,
                    delivery_required=True,
                    source_refs=[self.source_ref],
                    provisional=False,
                )
            ],
            acceptance_checks=[
                AcceptanceCheck(
                    check_id=check_id,
                    statement="The workflow is reproducible",
                    domain=Domain.CROSS_DOMAIN,
                    related_unit_ids=[requirement_id, decision_id],
                    source_refs=[self.source_ref],
                )
            ],
        )
        package_id = self.id()
        created = self.record(
            self.service.create_spec_package(
                CreateSpecPackageCommand(
                    command_id=self.id(),
                    case_id=self.case_id,
                    acting_actor_id="SYSTEM",
                    expected_case_revision=registered.receipt.revision,
                    package_id=package_id,
                    content_schema_version=1,
                    hash_schema_version=1,
                    payload=self.package_payload,
                )
            )
        )
        ready = self.record(
            self.service.mark_spec_package_ready(
                MarkSpecPackageReadyCommand(
                    command_id=self.id(),
                    case_id=self.case_id,
                    acting_actor_id="SYSTEM",
                    expected_case_revision=created.receipt.revision,
                    expected_artifact_id=package_id,
                    expected_artifact_version=1,
                    expected_artifact_hash=created.binding.semantic_hash,
                )
            )
        )
        business = self.record(
            self.service.approve_spec_package(
                ApproveSpecPackageCommand(
                    command_id=self.id(),
                    case_id=self.case_id,
                    acting_actor_id=self.pm_actor_id,
                    expected_case_revision=ready.receipt.revision,
                    expected_artifact_id=package_id,
                    expected_artifact_version=1,
                    expected_artifact_hash=created.binding.semantic_hash,
                    scope=ApprovalScope.BUSINESS,
                )
            )
        )
        self.record(
            self.service.approve_spec_package(
                ApproveSpecPackageCommand(
                    command_id=self.id(),
                    case_id=self.case_id,
                    acting_actor_id=self.dev_lead_actor_id,
                    expected_case_revision=business.receipt.revision,
                    expected_artifact_id=package_id,
                    expected_artifact_version=1,
                    expected_artifact_hash=created.binding.semantic_hash,
                    scope=ApprovalScope.TECHNICAL,
                )
            )
        )
        self.package_binding = created.binding
        return created.binding

    def build_approved_jira_plan(self) -> ArtifactBinding:
        assert self.package_payload is not None and self.package_binding is not None
        epic_id, task_id = self.id(), self.id()
        requirement = self.package_payload.requirements[0]
        decision = self.package_payload.technical_decisions[0]
        check = self.package_payload.acceptance_checks[0]

        def body(item_id: UUID, units: list[UUID]) -> StructuredWorkBody:
            return StructuredWorkBody(
                package_id=self.package_binding.artifact_id,
                package_version=self.package_binding.version,
                package_hash=self.package_binding.semantic_hash,
                generation_key=f"specops:{self.case_id}:jira:{item_id}",
                source_unit_ids=units,
                acceptance_checks=[
                    BodyAcceptanceCheck(check_id=check.check_id, statement=check.statement)
                ],
                provisional=False,
            )

        all_units = [requirement.unit_id, decision.unit_id]
        self.jira_payload = ProjectionPlanPayload(
            package_binding=self.package_binding,
            target=PlanTarget.JIRA,
            project_key="OPS",
            items=[
                ProjectionItem(
                    item_id=epic_id,
                    kind=JiraKind.EPIC,
                    domain=Domain.CROSS_DOMAIN,
                    title="Approved outcome",
                    body=body(epic_id, all_units),
                    source_unit_ids=all_units,
                    implementation_required=False,
                ),
                ProjectionItem(
                    item_id=task_id,
                    kind=JiraKind.TASK,
                    domain=Domain.CROSS_DOMAIN,
                    title="Implement deterministic transition",
                    body=body(task_id, all_units),
                    source_unit_ids=all_units,
                    parent_item_id=epic_id,
                    implementation_required=True,
                    repository="local/workflow",
                ),
            ],
        )
        plan_id = self.id()
        created = self.record(
            self.service.create_projection_plan(
                CreateProjectionPlanCommand(
                    command_id=self.id(),
                    case_id=self.case_id,
                    acting_actor_id="SYSTEM",
                    expected_case_revision=self.revision,
                    plan_id=plan_id,
                    content_schema_version=1,
                    hash_schema_version=1,
                    payload=self.jira_payload,
                )
            )
        )
        business = self.record(
            self.service.approve_projection_plan(
                ApproveProjectionPlanCommand(
                    command_id=self.id(),
                    case_id=self.case_id,
                    acting_actor_id=self.pm_actor_id,
                    expected_case_revision=created.receipt.revision,
                    expected_artifact_id=plan_id,
                    expected_artifact_version=1,
                    expected_artifact_hash=created.binding.semantic_hash,
                    scope=ApprovalScope.BUSINESS,
                )
            )
        )
        self.record(
            self.service.approve_projection_plan(
                ApproveProjectionPlanCommand(
                    command_id=self.id(),
                    case_id=self.case_id,
                    acting_actor_id=self.dev_lead_actor_id,
                    expected_case_revision=business.receipt.revision,
                    expected_artifact_id=plan_id,
                    expected_artifact_version=1,
                    expected_artifact_hash=created.binding.semantic_hash,
                    scope=ApprovalScope.TECHNICAL,
                )
            )
        )
        self.jira_binding = created.binding
        return created.binding

    def _policy(self, *, include_github_rule: bool) -> StatusPolicyPayload:
        assert self.jira_payload is not None
        rules = [
            StatusRule(
                rule_id=self.id(),
                target_system=System.JIRA,
                selector=Selector.ALL_PROJECTED_ITEMS,
                target_normalized_status=JiraStatus.TODO,
                all_of=[StatusCondition(kind=ConditionKind.PACKAGE_CURRENT_APPROVED)],
            )
        ]
        if include_github_rule:
            rules.append(
                StatusRule(
                    rule_id=self.id(),
                    target_system=System.GITHUB,
                    selector=Selector.ALL_PROJECTED_ITEMS,
                    target_normalized_status=GitHubStatus.OPEN,
                    all_of=[StatusCondition(kind=ConditionKind.PACKAGE_CURRENT_APPROVED)],
                )
            )
        return StatusPolicyPayload(
            mappings=[
                NativeStatusMapping(
                    mapping_id=self.id(), system=System.JIRA, native_status="To Do", normalized_status=JiraStatus.TODO
                ),
                NativeStatusMapping(
                    mapping_id=self.id(), system=System.JIRA, native_status="Done", normalized_status=JiraStatus.DONE
                ),
                NativeStatusMapping(
                    mapping_id=self.id(), system=System.JIRA, native_status="Cancelled", normalized_status=JiraStatus.CANCELLED
                ),
                NativeStatusMapping(
                    mapping_id=self.id(), system=System.GITHUB, native_status="open", normalized_status=GitHubStatus.OPEN
                ),
                NativeStatusMapping(
                    mapping_id=self.id(), system=System.GITHUB, native_status="closed", normalized_status=GitHubStatus.CLOSED
                ),
            ],
            rules=rules,
        )

    def build_approved_status_policy(self) -> ArtifactBinding:
        self.status_payload = self._policy(include_github_rule=False)
        policy_id = self.id()
        created = self.record(
            self.service.create_status_policy(
                CreateStatusPolicyCommand(
                    command_id=self.id(),
                    case_id=self.case_id,
                    acting_actor_id=self.pm_actor_id,
                    expected_case_revision=self.revision,
                    policy_id=policy_id,
                    content_schema_version=1,
                    hash_schema_version=1,
                    payload=self.status_payload,
                )
            )
        )
        self._approve_policy(created.binding)
        self.status_binding = created.binding
        return created.binding

    def _approve_policy(self, binding: ArtifactBinding) -> None:
        business = self.record(
            self.service.approve_status_policy(
                ApproveStatusPolicyCommand(
                    command_id=self.id(),
                    case_id=self.case_id,
                    acting_actor_id=self.pm_actor_id,
                    expected_case_revision=self.revision,
                    expected_artifact_id=binding.artifact_id,
                    expected_artifact_version=binding.version,
                    expected_artifact_hash=binding.semantic_hash,
                    scope=ApprovalScope.BUSINESS,
                )
            )
        )
        self.record(
            self.service.approve_status_policy(
                ApproveStatusPolicyCommand(
                    command_id=self.id(),
                    case_id=self.case_id,
                    acting_actor_id=self.dev_lead_actor_id,
                    expected_case_revision=business.receipt.revision,
                    expected_artifact_id=binding.artifact_id,
                    expected_artifact_version=binding.version,
                    expected_artifact_hash=binding.semantic_hash,
                    scope=ApprovalScope.TECHNICAL,
                )
            )
        )

    def pending_intents(self) -> list[OperationIntent]:
        return self.service.list_pending_operation_intents(
            UUIDListQuery(case_id=self.case_id, acting_actor_id="SYSTEM", limit=500)
        ).items

    def apply_intent(self, intent: OperationIntent, ordinal: int) -> None:
        operation_id = self.id()
        started = self.record(
            self.service.start_external_operation(
                StartExternalOperationCommand(
                    command_id=self.id(),
                    case_id=self.case_id,
                    acting_actor_id="SYSTEM",
                    expected_case_revision=self.revision,
                    operation_id=operation_id,
                    intent_id=intent.intent_id,
                    idempotency_key=f"operation-{intent.system.value.lower()}-{ordinal}",
                )
            )
        )
        if intent.system == System.JIRA:
            identity = JiraIdentity(key=f"OPS-{ordinal}")
            native_status = "To Do"
        else:
            identity = GitHubIdentity(
                repository="local/workflow",
                issue_number=ordinal,
                node_id=f"node-{ordinal}",
                html_url=f"https://github.com/local/workflow/issues/{ordinal}",
            )
            native_status = "open"
        observation = RemoteObservation(
            observation_kind=ObservationKind.FOUND,
            system=intent.system,
            external_identity=identity,
            generation_key=intent.request["generation_key"],
            package_binding=intent.package_binding,
            plan_binding=intent.plan_binding,
            jira_plan_binding=intent.jira_plan_binding,
            status_policy_binding=intent.status_policy_binding,
            remote_revision=f"revision-{ordinal}",
            native_status=native_status,
            owned_content=intent.request,
        )
        self.record(
            self.service.record_operation_result(
                RecordOperationResultCommand(
                    command_id=self.id(),
                    case_id=self.case_id,
                    acting_actor_id="SYSTEM",
                    expected_case_revision=started.receipt.revision,
                    operation_id=operation_id,
                    attempt=1,
                    outcome=NominalSuccess(
                        outcome=ResultOutcome.NOMINAL_SUCCESS, read_back=observation
                    ),
                )
            )
        )

    def apply_jira(self) -> None:
        ordinal = 0
        while True:
            intents = [item for item in self.pending_intents() if item.system == System.JIRA]
            if not intents:
                return
            ordinal += 1
            if ordinal > len(self.jira_payload.items):
                raise AssertionError("Jira intent derivation did not converge")
            self.apply_intent(intents[0], ordinal)

    def build_approved_github_plan(self) -> ArtifactBinding:
        assert self.package_binding is not None
        assert self.jira_binding is not None and self.jira_payload is not None
        implementation = next(item for item in self.jira_payload.items if item.implementation_required)
        jira_delivery = self.service.get_delivery_view(
            QueryOne(case_id=self.case_id, acting_actor_id="SYSTEM")
        )
        jira_item = next(item for item in jira_delivery.items if item.item_id == implementation.item_id)
        assert isinstance(jira_item.external_identity, JiraIdentity)
        github_item_id = self.id()
        body = StructuredWorkBody(
            package_id=self.package_binding.artifact_id,
            package_version=self.package_binding.version,
            package_hash=self.package_binding.semantic_hash,
            generation_key=f"specops:{self.case_id}:github:{github_item_id}",
            source_unit_ids=implementation.source_unit_ids,
            acceptance_checks=implementation.body.acceptance_checks,
            provisional=False,
            jira_key=jira_item.external_identity.key,
        )
        self.github_payload = ProjectionPlanPayload(
            package_binding=self.package_binding,
            target=PlanTarget.GITHUB,
            jira_plan_binding=self.jira_binding,
            items=[
                ProjectionItem(
                    item_id=github_item_id,
                    kind=GitHubKind.ISSUE,
                    domain=implementation.domain,
                    title=implementation.title,
                    body=body,
                    source_unit_ids=implementation.source_unit_ids,
                    implementation_required=False,
                    repository="local/workflow",
                    primary_jira_item_id=implementation.item_id,
                )
            ],
        )
        plan_id = self.id()
        created = self.record(
            self.service.create_projection_plan(
                CreateProjectionPlanCommand(
                    command_id=self.id(),
                    case_id=self.case_id,
                    acting_actor_id="SYSTEM",
                    expected_case_revision=self.revision,
                    plan_id=plan_id,
                    content_schema_version=1,
                    hash_schema_version=1,
                    payload=self.github_payload,
                )
            )
        )
        self.record(
            self.service.approve_projection_plan(
                ApproveProjectionPlanCommand(
                    command_id=self.id(),
                    case_id=self.case_id,
                    acting_actor_id=self.dev_lead_actor_id,
                    expected_case_revision=created.receipt.revision,
                    expected_artifact_id=plan_id,
                    expected_artifact_version=1,
                    expected_artifact_hash=created.binding.semantic_hash,
                    scope=ApprovalScope.TECHNICAL,
                )
            )
        )
        self.github_binding = created.binding
        return created.binding

    def revise_and_approve_policy_for_github(self) -> ArtifactBinding:
        assert self.status_binding is not None
        self.status_payload = self._policy(include_github_rule=True)
        revised = self.record(
            self.service.revise_status_policy(
                ReviseStatusPolicyCommand(
                    command_id=self.id(),
                    case_id=self.case_id,
                    acting_actor_id=self.pm_actor_id,
                    expected_case_revision=self.revision,
                    expected_artifact_id=self.status_binding.artifact_id,
                    expected_artifact_version=self.status_binding.version,
                    expected_artifact_hash=self.status_binding.semantic_hash,
                    content_schema_version=1,
                    hash_schema_version=1,
                    payload=self.status_payload,
                )
            )
        )
        self._approve_policy(revised.binding)
        self.status_binding = revised.binding
        return revised.binding

    def apply_github(self) -> None:
        ordinal = 0
        while True:
            intents = [item for item in self.pending_intents() if item.system == System.GITHUB]
            if not intents:
                return
            ordinal += 1
            if ordinal > len(self.github_payload.items):
                raise AssertionError("GitHub intent derivation did not converge")
            self.apply_intent(intents[0], ordinal)

    def audit_events(self):
        return self.service.list_audit_events(
            AuditQuery(case_id=self.case_id, acting_actor_id="SYSTEM", limit=500)
        ).items

    def complete(self) -> PublicWorkflowHarness:
        self.build_approved_package()
        self.build_approved_jira_plan()
        self.build_approved_status_policy()
        self.apply_jira()
        self.build_approved_github_plan()
        self.revise_and_approve_policy_for_github()
        self.apply_github()
        return self


def fresh_harness(tmp_path: Path) -> PublicWorkflowHarness:
    database_path = tmp_path / "new" / "workflow.sqlite"
    database_path.parent.mkdir(parents=True)
    assert not database_path.exists()
    database_url = f"sqlite:///{database_path}"
    migrate(database_url)
    authored = DeterministicUuidSequence(prefix=0x10000000)
    service_ids = DeterministicUuidSequence(prefix=0x20000000)
    case_id, pm_actor_id, dev_lead_actor_id = authored.new(), authored.new(), authored.new()
    service = WorkflowService(
        clock=FrozenClock(FROZEN_NOW), ids=service_ids, database_url=database_url
    )
    created = service.create_case(
        CreateCaseCommand(
            command_id=authored.new(),
            case_id=case_id,
            acting_actor_id=pm_actor_id,
            pm_actor_id=pm_actor_id,
            dev_lead_actor_id=dev_lead_actor_id,
        )
    )
    return PublicWorkflowHarness(
        service=service,
        database_url=database_url,
        database_path=database_path,
        authored_ids=authored,
        case_id=case_id,
        pm_actor_id=pm_actor_id,
        dev_lead_actor_id=dev_lead_actor_id,
        successful_command_ids=[created.receipt.command_id],
    )
