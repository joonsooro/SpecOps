from __future__ import annotations

import copy
import re
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid5

from pydantic import BaseModel

from .artifacts import ArtifactRoot, ArtifactVersion
from .canonical import artifact_hash, canonical_data, sha256
from .enums import *  # noqa: F403
from .errors import DomainError, ErrorCode
from .models import *  # noqa: F403
from .ports import Clock, RandomUuidGenerator, SystemClock, UuidGenerator
from .renderer import SENTINEL_JIRA_KEY, render_structured_body
from .state import AmbiguityState, ApprovalState, BindingState, CaseState, DelegationState, OperationState

RESOLUTION_NAMESPACE = UUID("4a5cbf7e-bcff-5a85-b670-c9a38145704d")
DRIFT_NAMESPACE = UUID("08f33a1b-f1af-56d7-8a0d-8ab2a87787f8")
INTENT_NAMESPACE = UUID("b91c74fb-902f-57cb-8dc3-aa89dd91702c")
ALLOWED_COMMANDS = {
    "create_case", "add_participant", "grant_delegation", "revoke_delegation", "register_source_artifact",
    "record_ambiguity_finding", "resolve_ambiguity_finding", "create_spec_package", "revise_spec_package",
    "mark_spec_package_ready", "approve_spec_package", "create_projection_plan", "revise_projection_plan",
    "approve_projection_plan", "create_status_policy", "revise_status_policy", "approve_status_policy",
    "start_external_operation", "record_operation_result", "reconcile_operation", "submit_remote_snapshot",
}


class WorkflowService:
    """Only public behavior boundary. Persistence is injected in feature 10."""

    def __init__(self, *, clock: Clock | None = None, ids: UuidGenerator | None = None) -> None:
        self.clock = clock or SystemClock()
        self.ids = ids or RandomUuidGenerator()
        self._cases: dict[UUID, CaseState] = {}
        self._command_case: dict[UUID, UUID] = {}

    def _now(self) -> datetime:
        return self.clock.now().astimezone(timezone.utc)

    @staticmethod
    def _fingerprint(name: str, command: BaseModel) -> str:
        return sha256({"schema": "command-v1", "command_name": name, "payload": command.model_dump(mode="python", exclude_none=False)})

    def _case(self, case_id: UUID) -> CaseState:
        try: return self._cases[case_id]
        except KeyError: raise DomainError(ErrorCode.RECORD_NOT_FOUND) from None

    def _begin(self, name: str, command: CommandBase) -> tuple[CaseState, BaseModel | None, str]:
        if command.acting_actor_id == "SYSTEM" and name in {"approve_spec_package", "approve_projection_plan", "approve_status_policy", "resolve_ambiguity_finding", "grant_delegation", "revoke_delegation", "add_participant"}:
            raise DomainError(ErrorCode.SYSTEM_ACTION_FORBIDDEN)
        owner = self._command_case.get(command.command_id)
        if owner is not None and owner != command.case_id: raise DomainError(ErrorCode.CROSS_CASE_REFERENCE)
        case = self._case(command.case_id)
        fingerprint = self._fingerprint(name, command)
        prior = case.command_results.get(command.command_id)
        if prior:
            if prior[0] != fingerprint: raise DomainError(ErrorCode.IDEMPOTENCY_CONFLICT)
            return case, prior[1], fingerprint
        if command.expected_case_revision != case.revision: raise DomainError(ErrorCode.STALE_ARTIFACT_BINDING)
        return case, None, fingerprint

    def _finish(self, case: CaseState, name: str, command: BaseModel, fingerprint: str, result_type: type[BaseModel], **values: Any) -> BaseModel:
        case.revision += 1
        receipt = Receipt(case_id=case.id, revision=case.revision, command_id=command.command_id, occurred_at=self._now())
        result = result_type(receipt=receipt, **values)
        case.command_results[command.command_id] = (fingerprint, result)
        self._command_case[command.command_id] = case.id
        return result

    def _authority_scope(self, case: CaseState, actor: UUID) -> ApprovalScope | None:
        if actor == case.pm_actor_id: return ApprovalScope.BUSINESS
        if actor == case.dev_lead_actor_id: return ApprovalScope.TECHNICAL
        return None

    def _authorize(self, case: CaseState, actor: Actor, command_name: str, *, scope: ApprovalScope | None = None, artifact_id: UUID | None = None) -> DelegationState | None:
        if actor == "SYSTEM":
            if command_name in {"register_source_artifact", "record_ambiguity_finding", "create_spec_package", "revise_spec_package", "mark_spec_package_ready", "create_projection_plan", "revise_projection_plan", "start_external_operation", "record_operation_result", "reconcile_operation", "submit_remote_snapshot"}: return None
            raise DomainError(ErrorCode.SYSTEM_ACTION_FORBIDDEN)
        direct = self._authority_scope(case, actor)
        if direct is not None and (scope is None or scope == direct): return None
        candidates = [item for item in case.delegations.values() if item.delegate_id == actor and command_name in item.command_names]
        now = self._now()
        active = [item for item in candidates if item.revoked_at is None and item.valid_from <= now <= item.valid_until]
        if candidates and not active: raise DomainError(ErrorCode.DELEGATION_NOT_ACTIVE)
        for item in active:
            if scope is not None and item.domain.value != scope.value: continue
            if item.artifact_id is not None and item.artifact_id != artifact_id: continue
            return item
        if active: raise DomainError(ErrorCode.DELEGATION_SCOPE_MISMATCH)
        raise DomainError(ErrorCode.AUTHORITY_REQUIRED)

    def create_case(self, command: CreateCaseCommand) -> CaseResult:
        owner = self._command_case.get(command.command_id)
        if owner is not None:
            if owner != command.case_id: raise DomainError(ErrorCode.CROSS_CASE_REFERENCE)
            prior = self._cases[owner].command_results[command.command_id]
            if prior[0] != self._fingerprint("create_case", command): raise DomainError(ErrorCode.IDEMPOTENCY_CONFLICT)
            return prior[1]  # type: ignore[return-value]
        if command.acting_actor_id != command.pm_actor_id: raise DomainError(ErrorCode.AUTHORITY_REQUIRED)
        if command.pm_actor_id == command.dev_lead_actor_id: raise DomainError(ErrorCode.AUTHORITY_SLOT_OCCUPIED)
        if command.case_id in self._cases: raise DomainError(ErrorCode.AUTHORITY_SLOT_OCCUPIED)
        case = CaseState(command.case_id, command.pm_actor_id, command.dev_lead_actor_id, self._now())
        case.participants.update({command.pm_actor_id, command.dev_lead_actor_id})
        self._cases[case.id] = case
        return self._finish(case, "create_case", command, self._fingerprint("create_case", command), CaseResult, case_id=case.id, pm_actor_id=case.pm_actor_id, dev_lead_actor_id=case.dev_lead_actor_id)  # type: ignore[return-value]

    def add_participant(self, command: AddParticipantCommand) -> ParticipantResult:
        case, replay, fp = self._begin("add_participant", command)
        if replay: return replay  # type: ignore[return-value]
        if command.acting_actor_id not in {case.pm_actor_id, case.dev_lead_actor_id}: raise DomainError(ErrorCode.AUTHORITY_REQUIRED)
        case.participants.add(command.actor_id)
        return self._finish(case, "add_participant", command, fp, ParticipantResult, case_id=case.id, actor_id=command.actor_id)  # type: ignore[return-value]

    def grant_delegation(self, command: GrantDelegationCommand) -> DelegationResult:
        case, replay, fp = self._begin("grant_delegation", command)
        if replay: return replay  # type: ignore[return-value]
        expected = case.pm_actor_id if command.domain == Domain.BUSINESS else case.dev_lead_actor_id
        if command.acting_actor_id != expected: raise DomainError(ErrorCode.AUTHORITY_REQUIRED)
        if command.delegate_id not in case.participants: raise DomainError(ErrorCode.AUTHORITY_REQUIRED)
        if command.valid_from > command.valid_until or any(name not in ALLOWED_COMMANDS for name in command.command_names): raise DomainError(ErrorCode.INVALID_TRANSITION)
        if (command.artifact_kind is None) != (command.artifact_id is None): raise DomainError(ErrorCode.INVALID_TRANSITION)
        case.delegations[command.delegation_id] = DelegationState(command.delegation_id, expected, command.delegate_id, command.domain, frozenset(command.command_names), command.artifact_kind.value if command.artifact_kind else None, command.artifact_id, command.valid_from, command.valid_until, command.later_review_required, self._now())
        return self._finish(case, "grant_delegation", command, fp, DelegationResult, case_id=case.id, delegation_id=command.delegation_id, revoked_at=None)  # type: ignore[return-value]

    def revoke_delegation(self, command: RevokeDelegationCommand) -> DelegationResult:
        case, replay, fp = self._begin("revoke_delegation", command)
        if replay: return replay  # type: ignore[return-value]
        delegation = case.delegations.get(command.delegation_id)
        if delegation is None: raise DomainError(ErrorCode.RECORD_NOT_FOUND)
        if command.acting_actor_id != delegation.delegator_id: raise DomainError(ErrorCode.AUTHORITY_REQUIRED)
        delegation.revoked_at = self._now()
        return self._finish(case, "revoke_delegation", command, fp, DelegationResult, case_id=case.id, delegation_id=delegation.id, revoked_at=delegation.revoked_at)  # type: ignore[return-value]

    def register_source_artifact(self, command: RegisterSourceArtifactCommand) -> SourceArtifactResult:
        case, replay, fp = self._begin("register_source_artifact", command)
        if replay: return replay  # type: ignore[return-value]
        self._authorize(case, command.acting_actor_id, "register_source_artifact")
        identity = command.identity
        if identity.case_id != case.id: raise DomainError(ErrorCode.CROSS_CASE_REFERENCE)
        self._validate_locator(identity.canonical_locator)
        stored = identity.model_copy(update={"registered_at": self._now()})
        case.sources[(stored.artifact_id, stored.version)] = stored
        return self._finish(case, "register_source_artifact", command, fp, SourceArtifactResult, identity=stored)  # type: ignore[return-value]

    @staticmethod
    def _validate_locator(locator: str) -> None:
        if not locator.startswith("/") or (locator != "/" and locator.endswith("/")) or "//" in locator or any(part in {".", ".."} for part in locator.split("/")):
            raise DomainError(ErrorCode.INVALID_SOURCE_REFERENCE)

    def _validate_source_ref(self, case: CaseState, ref: SourceRef) -> None:
        identity = case.sources.get((ref.artifact_id, ref.version))
        if identity is None:
            if any(key[0] == ref.artifact_id for other in self._cases.values() if other.id != case.id for key in other.sources): raise DomainError(ErrorCode.CROSS_CASE_REFERENCE)
            raise DomainError(ErrorCode.INVALID_SOURCE_REFERENCE)
        if identity.content_hash != ref.content_hash: raise DomainError(ErrorCode.INVALID_SOURCE_REFERENCE)
        kind = ref.location.kind
        allowed = {"JSON_POINTER": {"application/json"}, "LINE_RANGE": {"text/markdown", "text/plain"}, "WORKBOOK_RANGE": {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}}
        if identity.media_type not in allowed[kind]: raise DomainError(ErrorCode.INVALID_SOURCE_REFERENCE)

    def record_ambiguity_finding(self, command: RecordAmbiguityFindingCommand) -> AmbiguityFindingResult:
        case, replay, fp = self._begin("record_ambiguity_finding", command)
        if replay: return replay  # type: ignore[return-value]
        self._authorize(case, command.acting_actor_id, "record_ambiguity_finding")
        for ref in command.evidence_refs: self._validate_source_ref(case, ref)
        state = AmbiguityState(command.finding_id, command.category.value, command.domain, command.severity.value, command.evidence_refs, command.clarification_question, self._now())
        case.ambiguities[state.id] = state
        return self._finish(case, "record_ambiguity_finding", command, fp, AmbiguityFindingResult, finding_id=state.id, status=state.status, resolutions=[], resolved_at=None)  # type: ignore[return-value]

    @staticmethod
    def _required_scopes(domain: Domain) -> set[ApprovalScope]:
        if domain == Domain.BUSINESS: return {ApprovalScope.BUSINESS}
        if domain == Domain.TECHNICAL: return {ApprovalScope.TECHNICAL}
        return {ApprovalScope.BUSINESS, ApprovalScope.TECHNICAL}

    def resolve_ambiguity_finding(self, command: ResolveAmbiguityFindingCommand) -> AmbiguityFindingResult:
        case, replay, fp = self._begin("resolve_ambiguity_finding", command)
        if replay: return replay  # type: ignore[return-value]
        finding = case.ambiguities.get(command.finding_id)
        if finding is None: raise DomainError(ErrorCode.RECORD_NOT_FOUND)
        actor_scope = self._authority_scope(case, command.acting_actor_id) if command.acting_actor_id != "SYSTEM" else None
        scopes = self._required_scopes(finding.domain)
        if actor_scope not in scopes:
            active = [d for d in case.delegations.values() if d.delegate_id == command.acting_actor_id and "resolve_ambiguity_finding" in d.command_names]
            if not active: raise DomainError(ErrorCode.AUTHORITY_REQUIRED)
            actor_scope = ApprovalScope(active[0].domain.value)
        delegation = self._authorize(case, command.acting_actor_id, "resolve_ambiguity_finding", scope=actor_scope)
        prior = next((r for r in finding.resolutions if r.scope == actor_scope), None)
        now = self._now()
        if prior:
            if prior.later_review_required and prior.reviewed_at is None and command.acting_actor_id == prior.delegator_id:
                prior.reviewed_by = command.acting_actor_id; prior.reviewed_at = now
            else: raise DomainError(ErrorCode.APPROVAL_ALREADY_EXISTS)
        else:
            for ref in command.resolution_source_refs: self._validate_source_ref(case, ref)
            name = {"case_id": case.id, "finding_id": finding.id, "schema": "ambiguity-resolution-v1", "scope": actor_scope.value}
            record = ResolutionRecord(resolution_id=uuid5(RESOLUTION_NAMESPACE, canonical_data(name).__str__()), scope=actor_scope, actor_id=command.acting_actor_id, delegation_id=delegation.id if delegation else None, delegator_id=delegation.delegator_id if delegation else None, later_review_required=delegation.later_review_required if delegation else False, resolution_text=command.resolution_text, resolution_source_refs=command.resolution_source_refs, recorded_at=now)
            finding.resolutions.append(record)
        if scopes.issubset({r.scope for r in finding.resolutions}): finding.status = FindingStatus.RESOLVED; finding.resolved_at = now
        return self._finish(case, "resolve_ambiguity_finding", command, fp, AmbiguityFindingResult, finding_id=finding.id, status=finding.status, resolutions=finding.resolutions, resolved_at=finding.resolved_at)  # type: ignore[return-value]

    @staticmethod
    def _source_sort(ref: SourceRef) -> tuple[Any, ...]:
        location = ref.location
        detail = getattr(location, "pointer", None) or (getattr(location, "start", None), getattr(location, "end", None)) or (getattr(location, "sheet", None), getattr(location, "a1", None))
        return (ref.artifact_id.bytes, ref.version, location.kind, str(detail))

    def _normalized_package_payload(self, payload: SpecPackagePayload) -> dict[str, Any]:
        def refs(values: list[SourceRef]) -> list[dict[str, Any]]:
            return [item.model_dump(mode="python", exclude_none=False) for item in sorted(values, key=self._source_sort)]
        requirements = []
        for item in sorted(payload.requirements, key=lambda row: row.unit_id.bytes):
            value = item.model_dump(mode="python", exclude_none=False); value["source_refs"] = refs(item.source_refs); requirements.append(value)
        decisions = []
        for item in sorted(payload.technical_decisions, key=lambda row: row.unit_id.bytes):
            value = item.model_dump(mode="python", exclude_none=False); value["source_refs"] = refs(item.source_refs); decisions.append(value)
        checks = []
        for item in sorted(payload.acceptance_checks, key=lambda row: row.check_id.bytes):
            value = item.model_dump(mode="python", exclude_none=False); value["related_unit_ids"] = sorted(item.related_unit_ids, key=lambda value: value.bytes); value["source_refs"] = refs(item.source_refs); checks.append(value)
        return {"requirements": requirements, "technical_decisions": decisions, "acceptance_checks": checks}

    def _validate_package_shape(self, case: CaseState, payload: SpecPackagePayload, *, ready: bool) -> None:
        units = [item.unit_id for item in payload.requirements] + [item.unit_id for item in payload.technical_decisions]
        all_ids = units + [item.check_id for item in payload.acceptance_checks]
        if len(all_ids) != len(set(all_ids)): raise DomainError(ErrorCode.INVALID_TRANSITION)
        unit_set = set(units)
        for item in [*payload.requirements, *payload.technical_decisions, *payload.acceptance_checks]:
            if ready:
                for ref in item.source_refs: self._validate_source_ref(case, ref)
        for check in payload.acceptance_checks:
            if len(check.related_unit_ids) != len(set(check.related_unit_ids)) or not set(check.related_unit_ids).issubset(unit_set): raise DomainError(ErrorCode.INVALID_TRANSITION)
        for decision in payload.technical_decisions:
            if decision.provisional:
                delegation = case.delegations.get(decision.provisional_delegation_id)
                if delegation is None or delegation.domain != Domain.TECHNICAL or not delegation.later_review_required:
                    raise DomainError(ErrorCode.INVALID_TRANSITION)
        if ready and any(item.status == FindingStatus.OPEN and item.severity == Severity.BLOCKING.value for item in case.ambiguities.values()):
            raise DomainError(ErrorCode.BLOCKING_FINDING)

    def create_spec_package(self, command: CreateSpecPackageCommand) -> SpecPackageResult:
        case, replay, fp = self._begin("create_spec_package", command)
        if replay: return replay  # type: ignore[return-value]
        self._authorize(case, command.acting_actor_id, "create_spec_package", artifact_id=command.package_id)
        if case.package is not None: raise DomainError(ErrorCode.INVALID_TRANSITION)
        self._validate_package_shape(case, command.payload, ready=False)
        digest = artifact_hash(ArtifactKind.SPEC_PACKAGE, command.content_schema_version, command.hash_schema_version, self._normalized_package_payload(command.payload))
        root = ArtifactRoot(ArtifactKind.SPEC_PACKAGE, command.package_id)
        root.versions.append(ArtifactVersion(1, digest, command.content_schema_version, command.hash_schema_version, command.payload, PackageState.DRAFT.value))
        case.package = root
        return self._finish(case, "create_spec_package", command, fp, SpecPackageResult, binding=root.binding, state=PackageState.DRAFT)  # type: ignore[return-value]

    def revise_spec_package(self, command: ReviseSpecPackageCommand) -> SpecPackageResult:
        case, replay, fp = self._begin("revise_spec_package", command)
        if replay: return replay  # type: ignore[return-value]
        if case.package is None: raise DomainError(ErrorCode.RECORD_NOT_FOUND)
        self._authorize(case, command.acting_actor_id, "revise_spec_package", artifact_id=case.package.artifact_id)
        case.package.assert_binding(command.expected_artifact_id, command.expected_artifact_version, command.expected_artifact_hash)
        self._validate_package_shape(case, command.payload, ready=False)
        digest = artifact_hash(ArtifactKind.SPEC_PACKAGE, command.content_schema_version, command.hash_schema_version, self._normalized_package_payload(command.payload))
        case.package.revise(ArtifactVersion(case.package.current.version + 1, digest, command.content_schema_version, command.hash_schema_version, command.payload, PackageState.DRAFT.value))
        for plan in case.plans.values():
            current = plan.current; plan.versions[-1] = ArtifactVersion(current.version, current.semantic_hash, current.content_schema_version, current.hash_schema_version, current.payload, PlanState.STALE.value)
        return self._finish(case, "revise_spec_package", command, fp, SpecPackageResult, binding=case.package.binding, state=PackageState.DRAFT)  # type: ignore[return-value]

    def mark_spec_package_ready(self, command: MarkSpecPackageReadyCommand) -> SpecPackageResult:
        case, replay, fp = self._begin("mark_spec_package_ready", command)
        if replay: return replay  # type: ignore[return-value]
        if case.package is None: raise DomainError(ErrorCode.RECORD_NOT_FOUND)
        self._authorize(case, command.acting_actor_id, "mark_spec_package_ready", artifact_id=case.package.artifact_id)
        case.package.assert_binding(command.expected_artifact_id, command.expected_artifact_version, command.expected_artifact_hash)
        current = case.package.current
        if current.state != PackageState.DRAFT.value: raise DomainError(ErrorCode.INVALID_TRANSITION)
        self._validate_package_shape(case, current.payload, ready=True)
        case.package.versions[-1] = ArtifactVersion(current.version, current.semantic_hash, current.content_schema_version, current.hash_schema_version, current.payload, PackageState.READY.value)
        return self._finish(case, "mark_spec_package_ready", command, fp, SpecPackageResult, binding=case.package.binding, state=PackageState.READY)  # type: ignore[return-value]

    def approve_spec_package(self, command: ApproveSpecPackageCommand) -> ApprovalResult:
        case, replay, fp = self._begin("approve_spec_package", command)
        if replay: return replay  # type: ignore[return-value]
        if case.package is None: raise DomainError(ErrorCode.RECORD_NOT_FOUND)
        case.package.assert_binding(command.expected_artifact_id, command.expected_artifact_version, command.expected_artifact_hash)
        current = case.package.current
        if current.state not in {PackageState.READY.value, PackageState.APPROVED.value}: raise DomainError(ErrorCode.INVALID_TRANSITION)
        delegation = self._authorize(case, command.acting_actor_id, "approve_spec_package", scope=command.scope, artifact_id=case.package.artifact_id)
        if any(item.artifact_id == case.package.artifact_id and item.artifact_version == current.version and item.artifact_hash == current.semantic_hash and item.scope == command.scope and item.actor_id == command.acting_actor_id for item in case.approvals):
            raise DomainError(ErrorCode.APPROVAL_ALREADY_EXISTS)
        approval = ApprovalState(self.ids.new(), ArtifactKind.SPEC_PACKAGE.value, case.package.artifact_id, current.version, current.semantic_hash, command.scope, command.acting_actor_id, delegation.id if delegation else None, delegation.delegator_id if delegation else None, delegation.later_review_required if delegation else False, self._now())
        case.approvals.append(approval)
        scopes = {item.scope for item in case.approvals if item.artifact_id == case.package.artifact_id and item.artifact_version == current.version and item.artifact_hash == current.semantic_hash}
        if {ApprovalScope.BUSINESS, ApprovalScope.TECHNICAL}.issubset(scopes):
            case.package.versions[-1] = ArtifactVersion(current.version, current.semantic_hash, current.content_schema_version, current.hash_schema_version, current.payload, PackageState.APPROVED.value)
        return self._finish(case, "approve_spec_package", command, fp, ApprovalResult, approval_id=approval.id, scope=approval.scope, artifact_binding=case.package.binding)  # type: ignore[return-value]

    @staticmethod
    def _generation_key(case_id: UUID, target: PlanTarget, item_id: UUID) -> str:
        return f"specops:{str(case_id).lower()}:{target.value.lower()}:{str(item_id).lower()}"

    @staticmethod
    def _repository_valid(value: str) -> bool:
        return re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,37}[a-z0-9])?/[a-z0-9._-]{1,100}", value) is not None

    @staticmethod
    def _item_hash(target: PlanTarget, project_key: str | None, item: ProjectionItem) -> str:
        return sha256({"schema": "projection-item-v1", "target": target.value, "project_key": project_key, "item": item.model_dump(mode="python", exclude_none=False)})

    def _validate_plan(self, case: CaseState, payload: ProjectionPlanPayload) -> dict[UUID, str]:
        if case.package is None or case.package.current.state != PackageState.APPROVED.value: raise DomainError(ErrorCode.INVALID_PROJECTION_PLAN)
        if payload.package_binding != case.package.binding: raise DomainError(ErrorCode.STALE_ARTIFACT_BINDING)
        if payload.target == PlanTarget.JIRA:
            if payload.project_key is None or re.fullmatch(r"[A-Z][A-Z0-9]{1,9}", payload.project_key) is None or payload.jira_plan_binding is not None: raise DomainError(ErrorCode.INVALID_PROJECTION_PLAN)
        else:
            if payload.project_key is not None or payload.jira_plan_binding is None: raise DomainError(ErrorCode.INVALID_PROJECTION_PLAN)
        items = {item.item_id: item for item in payload.items}
        if len(items) != len(payload.items): raise DomainError(ErrorCode.INVALID_PROJECTION_PLAN)
        package_payload: SpecPackagePayload = case.package.current.payload
        units = {item.unit_id: item for item in [*package_payload.requirements, *package_payload.technical_decisions]}
        checks = {item.check_id: item for item in package_payload.acceptance_checks}
        generations: set[str] = set()
        hashes: dict[UUID, str] = {}
        for item in payload.items:
            expected_key = self._generation_key(case.id, payload.target, item.item_id)
            if item.body.generation_key != expected_key or expected_key in generations: raise DomainError(ErrorCode.INVALID_PROJECTION_PLAN)
            generations.add(expected_key)
            if not item.source_unit_ids or set(item.source_unit_ids) != set(item.body.source_unit_ids) or not set(item.source_unit_ids).issubset(units): raise DomainError(ErrorCode.INVALID_PROJECTION_PLAN)
            if set(item.dependency_item_ids) != set(item.body.dependency_item_ids) or item.item_id in item.dependency_item_ids or not set(item.dependency_item_ids).issubset(items): raise DomainError(ErrorCode.INVALID_PROJECTION_PLAN)
            if item.body.package_id != case.package.artifact_id or item.body.package_version != case.package.current.version or item.body.package_hash != case.package.current.semantic_hash: raise DomainError(ErrorCode.INVALID_PROJECTION_PLAN)
            if not {check.check_id for check in item.body.acceptance_checks}.issubset(checks): raise DomainError(ErrorCode.INVALID_PROJECTION_PLAN)
            domains = {units[unit_id].domain for unit_id in item.source_unit_ids}
            derived = Domain.BUSINESS if domains == {Domain.BUSINESS} else Domain.TECHNICAL if domains == {Domain.TECHNICAL} else Domain.CROSS_DOMAIN
            if item.domain != derived: raise DomainError(ErrorCode.INVALID_PROJECTION_PLAN)
            provisional = any(isinstance(units[unit_id], TechnicalDecision) and units[unit_id].provisional for unit_id in item.source_unit_ids)
            if item.body.provisional != provisional: raise DomainError(ErrorCode.INVALID_PROJECTION_PLAN)
            if payload.target == PlanTarget.JIRA:
                if not isinstance(item.kind, JiraKind) or item.primary_jira_item_id is not None or item.body.jira_key is not None: raise DomainError(ErrorCode.INVALID_PROJECTION_PLAN)
                if item.kind == JiraKind.EPIC:
                    if item.parent_item_id is not None or item.implementation_required: raise DomainError(ErrorCode.INVALID_PROJECTION_PLAN)
                elif item.kind in {JiraKind.STORY, JiraKind.TASK}:
                    if item.parent_item_id not in items or items[item.parent_item_id].kind != JiraKind.EPIC: raise DomainError(ErrorCode.INVALID_PROJECTION_PLAN)
                elif item.parent_item_id not in items or items[item.parent_item_id].kind not in {JiraKind.STORY, JiraKind.TASK}: raise DomainError(ErrorCode.INVALID_PROJECTION_PLAN)
                if (item.repository is not None) != item.implementation_required: raise DomainError(ErrorCode.INVALID_PROJECTION_PLAN)
                if item.repository is not None and not self._repository_valid(item.repository): raise DomainError(ErrorCode.INVALID_PROJECTION_PLAN)
            else:
                jira_root = case.plans.get(PlanTarget.JIRA)
                if jira_root is None or payload.jira_plan_binding != jira_root.binding or jira_root.current.state not in {PlanState.APPROVED.value, PlanState.APPLYING.value, PlanState.APPLIED.value}: raise DomainError(ErrorCode.INVALID_PROJECTION_PLAN)
                if not isinstance(item.kind, GitHubKind) or item.kind != GitHubKind.ISSUE or item.implementation_required or item.repository is None or not self._repository_valid(item.repository) or item.parent_item_id is not None or item.primary_jira_item_id is None: raise DomainError(ErrorCode.INVALID_PROJECTION_PLAN)
                jira_items = {value.item_id: value for value in jira_root.current.payload.items}
                primary = jira_items.get(item.primary_jira_item_id)
                if primary is None or not primary.implementation_required or set(primary.source_unit_ids) != set(item.source_unit_ids): raise DomainError(ErrorCode.INVALID_PROJECTION_PLAN)
                binding = case.metadata.get("external_bindings", {}).get(item.primary_jira_item_id)
                jira_key = binding.get("key") if isinstance(binding, dict) else None
                if jira_key is None or item.body.jira_key != jira_key or re.fullmatch(r"[A-Z][A-Z0-9]{1,9}-[1-9][0-9]{0,17}", jira_key) is None: raise DomainError(ErrorCode.INVALID_PROJECTION_PLAN)
            render_structured_body(item.body, {}, jira_key=item.body.jira_key, sizing=True)
            hashes[item.item_id] = self._item_hash(payload.target, payload.project_key, item)
        if payload.target == PlanTarget.JIRA:
            mapped = {unit for item in payload.items for unit in item.source_unit_ids}
            required = {item.unit_id for item in units.values() if item.delivery_required}
            if not required.issubset(mapped): raise DomainError(ErrorCode.INVALID_PROJECTION_PLAN)
            for item in payload.items:
                ancestor = item.parent_item_id
                while ancestor is not None:
                    parent = items[ancestor]
                    if item.implementation_required and parent.implementation_required: raise DomainError(ErrorCode.INVALID_PROJECTION_PLAN)
                    ancestor = parent.parent_item_id
        else:
            jira_items = case.plans[PlanTarget.JIRA].current.payload.items
            leaves = {item.item_id for item in jira_items if item.implementation_required}
            primaries = [item.primary_jira_item_id for item in payload.items]
            if len(primaries) != len(set(primaries)) or set(primaries) != leaves: raise DomainError(ErrorCode.INVALID_PROJECTION_PLAN)
        visiting: set[UUID] = set(); visited: set[UUID] = set()
        def visit(item_id: UUID) -> None:
            if item_id in visiting: raise DomainError(ErrorCode.INVALID_PROJECTION_PLAN)
            if item_id in visited: return
            visiting.add(item_id)
            for dependency in items[item_id].dependency_item_ids: visit(dependency)
            visiting.remove(item_id); visited.add(item_id)
        for item_id in items: visit(item_id)
        return hashes

    def _normalized_plan_payload(self, payload: ProjectionPlanPayload) -> dict[str, Any]:
        value = payload.model_dump(mode="python", exclude_none=False)
        value["items"] = sorted(value["items"], key=lambda row: UUID(str(row["item_id"])).bytes)
        for item in value["items"]:
            item["source_unit_ids"] = sorted(item["source_unit_ids"], key=lambda unit: UUID(str(unit)).bytes)
            item["dependency_item_ids"] = sorted(item["dependency_item_ids"], key=lambda unit: UUID(str(unit)).bytes)
            item["body"]["source_unit_ids"] = sorted(item["body"]["source_unit_ids"], key=lambda unit: UUID(str(unit)).bytes)
            item["body"]["dependency_item_ids"] = sorted(item["body"]["dependency_item_ids"], key=lambda unit: UUID(str(unit)).bytes)
            item["body"]["acceptance_checks"] = sorted(item["body"]["acceptance_checks"], key=lambda row: UUID(str(row["check_id"])).bytes)
        return value

    def create_projection_plan(self, command: CreateProjectionPlanCommand) -> ProjectionPlanResult:
        case, replay, fp = self._begin("create_projection_plan", command)
        if replay: return replay  # type: ignore[return-value]
        self._authorize(case, command.acting_actor_id, "create_projection_plan", artifact_id=command.plan_id)
        if command.payload.target in case.plans: raise DomainError(ErrorCode.INVALID_PROJECTION_PLAN)
        hashes = self._validate_plan(case, command.payload)
        digest = artifact_hash(ArtifactKind.PROJECTION_PLAN, command.content_schema_version, command.hash_schema_version, self._normalized_plan_payload(command.payload))
        root = ArtifactRoot(ArtifactKind.PROJECTION_PLAN, command.plan_id)
        root.versions.append(ArtifactVersion(1, digest, command.content_schema_version, command.hash_schema_version, command.payload, PlanState.READY.value))
        case.plans[command.payload.target] = root
        case.metadata.setdefault("item_hashes", {})[(command.plan_id, 1)] = hashes
        return self._finish(case, "create_projection_plan", command, fp, ProjectionPlanResult, binding=root.binding, target=command.payload.target, state=PlanState.READY, derived_intent_ids=[])  # type: ignore[return-value]

    def revise_projection_plan(self, command: ReviseProjectionPlanCommand) -> ProjectionPlanResult:
        case, replay, fp = self._begin("revise_projection_plan", command)
        if replay: return replay  # type: ignore[return-value]
        root = case.plans.get(command.payload.target)
        if root is None: raise DomainError(ErrorCode.RECORD_NOT_FOUND)
        self._authorize(case, command.acting_actor_id, "revise_projection_plan", artifact_id=root.artifact_id)
        root.assert_binding(command.expected_artifact_id, command.expected_artifact_version, command.expected_artifact_hash)
        new_hashes = self._validate_plan(case, command.payload)
        digest = artifact_hash(ArtifactKind.PROJECTION_PLAN, command.content_schema_version, command.hash_schema_version, self._normalized_plan_payload(command.payload))
        prior_items = {item.item_id: item for item in root.current.payload.items}; new_items = {item.item_id: item for item in command.payload.items}
        prior_hashes = case.metadata["item_hashes"][(root.artifact_id, root.current.version)]
        reconciliation = {}
        for item_id in set(prior_items) | set(new_items):
            if item_id not in prior_items: reconciliation[item_id] = Action.CREATE
            elif item_id not in new_items: reconciliation[item_id] = Action.RETIRE if item_id in case.metadata.get("external_bindings", {}) else None
            elif prior_hashes[item_id] == new_hashes[item_id]: reconciliation[item_id] = None
            else: reconciliation[item_id] = Action.UPDATE
        root.revise(ArtifactVersion(root.current.version + 1, digest, command.content_schema_version, command.hash_schema_version, command.payload, PlanState.READY.value))
        case.metadata.setdefault("item_hashes", {})[(root.artifact_id, root.current.version)] = new_hashes
        case.metadata.setdefault("reconciliation", {})[(root.artifact_id, root.current.version)] = reconciliation
        if command.payload.target == PlanTarget.JIRA and PlanTarget.GITHUB in case.plans:
            gh = case.plans[PlanTarget.GITHUB]; current = gh.current; gh.versions[-1] = ArtifactVersion(current.version, current.semantic_hash, current.content_schema_version, current.hash_schema_version, current.payload, PlanState.STALE.value)
        return self._finish(case, "revise_projection_plan", command, fp, ProjectionPlanResult, binding=root.binding, target=command.payload.target, state=PlanState.READY, derived_intent_ids=[])  # type: ignore[return-value]

    def approve_projection_plan(self, command: ApproveProjectionPlanCommand) -> ApprovalResult:
        case, replay, fp = self._begin("approve_projection_plan", command)
        if replay: return replay  # type: ignore[return-value]
        root = next((plan for plan in case.plans.values() if plan.artifact_id == command.expected_artifact_id), None)
        if root is None: raise DomainError(ErrorCode.RECORD_NOT_FOUND)
        root.assert_binding(command.expected_artifact_id, command.expected_artifact_version, command.expected_artifact_hash)
        if root.current.state not in {PlanState.READY.value, PlanState.APPROVED.value}: raise DomainError(ErrorCode.INVALID_TRANSITION)
        payload: ProjectionPlanPayload = root.current.payload
        required = {ApprovalScope.TECHNICAL} if payload.target == PlanTarget.GITHUB else ({ApprovalScope.BUSINESS} if any(item.domain in {Domain.BUSINESS, Domain.CROSS_DOMAIN} for item in payload.items) else set()) | ({ApprovalScope.TECHNICAL} if any(item.domain in {Domain.TECHNICAL, Domain.CROSS_DOMAIN} for item in payload.items) else set())
        if command.scope not in required: raise DomainError(ErrorCode.APPROVAL_BINDING_MISMATCH)
        delegation = self._authorize(case, command.acting_actor_id, "approve_projection_plan", scope=command.scope, artifact_id=root.artifact_id)
        if any(item.artifact_id == root.artifact_id and item.artifact_version == root.current.version and item.artifact_hash == root.current.semantic_hash and item.scope == command.scope and item.actor_id == command.acting_actor_id for item in case.approvals): raise DomainError(ErrorCode.APPROVAL_ALREADY_EXISTS)
        approval = ApprovalState(self.ids.new(), ArtifactKind.PROJECTION_PLAN.value, root.artifact_id, root.current.version, root.current.semantic_hash, command.scope, command.acting_actor_id, delegation.id if delegation else None, delegation.delegator_id if delegation else None, delegation.later_review_required if delegation else False, self._now())
        case.approvals.append(approval)
        scopes = {item.scope for item in case.approvals if item.artifact_id == root.artifact_id and item.artifact_version == root.current.version and item.artifact_hash == root.current.semantic_hash}
        if required.issubset(scopes):
            current = root.current; root.versions[-1] = ArtifactVersion(current.version, current.semantic_hash, current.content_schema_version, current.hash_schema_version, current.payload, PlanState.APPROVED.value)
        return self._finish(case, "approve_projection_plan", command, fp, ApprovalResult, approval_id=approval.id, scope=approval.scope, artifact_binding=root.binding)  # type: ignore[return-value]

    def _operation_result(self, operation: OperationState, receipt: Receipt) -> OperationResult:
        return OperationResult(operation_id=operation.id, intent_id=operation.intent.intent_id, system=operation.intent.system, action=operation.intent.action, status=operation.status, attempt=operation.attempt, failure_code=operation.failure_code, confirmation=operation.confirmation, confirmed_snapshot_sequence=operation.confirmed_snapshot_sequence, receipt=receipt)

    def start_external_operation(self, command: StartExternalOperationCommand) -> OperationResult:
        case, replay, fp = self._begin("start_external_operation", command)
        if replay: return replay  # type: ignore[return-value]
        self._authorize(case, command.acting_actor_id, "start_external_operation")
        intent: OperationIntent | None = case.metadata.get("intents", {}).get(command.intent_id)
        if intent is None: raise DomainError(ErrorCode.RECORD_NOT_FOUND)
        idempotent = next((item for item in case.operations.values() if item.idempotency_key == command.idempotency_key), None)
        by_intent = next((item for item in case.operations.values() if item.intent.intent_id == command.intent_id), None)
        operation = case.operations.get(command.operation_id)
        if idempotent is not None and idempotent.id != command.operation_id: raise DomainError(ErrorCode.IDEMPOTENCY_CONFLICT)
        if by_intent is not None and by_intent.id != command.operation_id: raise DomainError(ErrorCode.IDEMPOTENCY_CONFLICT)
        now = self._now()
        if operation is None:
            operation = OperationState(command.operation_id, intent, command.idempotency_key, OperationStatus.PENDING, 1, now, now)
            case.operations[operation.id] = operation
        else:
            if operation.intent.intent_id != command.intent_id or operation.idempotency_key != command.idempotency_key or operation.intent.fingerprint != intent.fingerprint: raise DomainError(ErrorCode.IDEMPOTENCY_CONFLICT)
            if operation.status in {OperationStatus.PENDING, OperationStatus.UNKNOWN}: raise DomainError(ErrorCode.OPERATION_RETRY_BLOCKED)
            if operation.status == OperationStatus.SUCCEEDED and operation.last_result is not None: return operation.last_result
            operation.status = OperationStatus.PENDING; operation.attempt += 1; operation.failure_code = None; operation.updated_at = now
        case.revision += 1
        receipt = Receipt(case_id=case.id, revision=case.revision, command_id=command.command_id, occurred_at=now)
        result = self._operation_result(operation, receipt); operation.last_result = result
        case.command_results[command.command_id] = (fp, result); self._command_case[command.command_id] = case.id
        return result

    @staticmethod
    def _identity_key(system: System, identity: ExternalIdentity) -> str:
        if system == System.JIRA and isinstance(identity, JiraIdentity): return identity.key
        if system == System.GITHUB and isinstance(identity, GitHubIdentity): return identity.node_id
        raise DomainError(ErrorCode.UNCONFIRMED_EXTERNAL_ID)

    def _matching_observation(self, case: CaseState, operation: OperationState, observation: RemoteObservation) -> BindingState:
        intent = operation.intent
        if observation.observation_kind != ObservationKind.FOUND: raise DomainError(ErrorCode.UNKNOWN_EXTERNAL_RESULT)
        if observation.system != intent.system or observation.generation_key != intent.request.get("generation_key") or observation.package_binding != intent.package_binding or observation.plan_binding != intent.plan_binding or observation.jira_plan_binding != intent.jira_plan_binding or observation.status_policy_binding != intent.status_policy_binding:
            raise DomainError(ErrorCode.UNCONFIRMED_EXTERNAL_ID)
        if sha256(observation.owned_content) != intent.request_owned_content_hash: raise DomainError(ErrorCode.UNKNOWN_EXTERNAL_RESULT)
        identity_key = self._identity_key(intent.system, observation.external_identity)
        item_id = UUID(str(intent.item_ref["item_id"]))
        existing = next((item for item in case.bindings.values() if item.system == intent.system and item.plan_id == intent.plan_binding.artifact_id and item.item_id == item_id), None)
        if intent.action == Action.CREATE and existing is not None: raise DomainError(ErrorCode.UNCONFIRMED_EXTERNAL_ID)
        if intent.action != Action.CREATE and existing is None: raise DomainError(ErrorCode.UNCONFIRMED_EXTERNAL_ID)
        if existing is not None:
            if self._identity_key(intent.system, existing.external_identity) != identity_key: raise DomainError(ErrorCode.UNCONFIRMED_EXTERNAL_ID)
            latest = next((snap for snap in reversed(existing.snapshots) if snap.remote_revision is not None), None)
            expected = latest.remote_revision if latest else None
            if observation.expected_previous_remote_revision != expected: raise DomainError(ErrorCode.REMOTE_VERSION_MISMATCH)
            if any(snap.remote_revision == observation.remote_revision for snap in existing.snapshots): raise DomainError(ErrorCode.INVALID_TRANSITION)
            return existing
        if observation.expected_previous_remote_revision is not None: raise DomainError(ErrorCode.REMOTE_VERSION_MISMATCH)
        if any(self._identity_key(item.system, item.external_identity) == identity_key for item in case.bindings.values() if item.system == intent.system): raise DomainError(ErrorCode.IDEMPOTENCY_CONFLICT)
        binding = BindingState(self.ids.new(), case.id, intent.system, intent.plan_binding.artifact_id, item_id, observation.generation_key, observation.external_identity, intent.plan_binding.version, 0, self._now())
        case.bindings[binding.id] = binding
        return binding

    def _store_found(self, case: CaseState, operation: OperationState, observation: RemoteObservation) -> BindingState:
        binding = self._matching_observation(case, operation, observation)
        binding.snapshots.append(observation); binding.current_observation_sequence += 1; binding.current_plan_version = observation.plan_binding.version
        entry = {"binding_id": binding.id, "key": observation.external_identity.key} if isinstance(observation.external_identity, JiraIdentity) else {"binding_id": binding.id, "identity": observation.external_identity}
        case.metadata.setdefault("external_bindings", {})[binding.item_id] = entry
        return binding

    def record_operation_result(self, command: RecordOperationResultCommand) -> OperationResult:
        case, replay, fp = self._begin("record_operation_result", command)
        if replay: return replay  # type: ignore[return-value]
        self._authorize(case, command.acting_actor_id, "record_operation_result")
        operation = case.operations.get(command.operation_id)
        if operation is None: raise DomainError(ErrorCode.RECORD_NOT_FOUND)
        if operation.status != OperationStatus.PENDING or operation.attempt != command.attempt: raise DomainError(ErrorCode.INVALID_TRANSITION)
        now = self._now()
        if isinstance(command.outcome, ExplicitFailure):
            operation.status = OperationStatus.FAILED; operation.failure_code = command.outcome.failure_code
        elif command.outcome.read_back is None:
            operation.status = OperationStatus.UNKNOWN
        else:
            try:
                binding = self._store_found(case, operation, command.outcome.read_back)
            except DomainError:
                operation.status = OperationStatus.UNKNOWN
            else:
                operation.status = OperationStatus.SUCCEEDED; operation.confirmation = command.outcome.read_back; operation.confirmed_snapshot_sequence = binding.current_observation_sequence
        operation.updated_at = now
        case.revision += 1; receipt = Receipt(case_id=case.id, revision=case.revision, command_id=command.command_id, occurred_at=now)
        result = self._operation_result(operation, receipt); operation.last_result = result
        case.command_results[command.command_id] = (fp, result); self._command_case[command.command_id] = case.id
        return result

    def reconcile_operation(self, command: ReconcileOperationCommand) -> OperationResult:
        case, replay, fp = self._begin("reconcile_operation", command)
        if replay: return replay  # type: ignore[return-value]
        self._authorize(case, command.acting_actor_id, "reconcile_operation")
        operation = case.operations.get(command.operation_id)
        if operation is None: raise DomainError(ErrorCode.RECORD_NOT_FOUND)
        if operation.attempt != command.attempt or operation.status in {OperationStatus.PENDING, OperationStatus.FAILED}: raise DomainError(ErrorCode.INVALID_TRANSITION)
        if command.observation.observation_kind == ObservationKind.NOT_FOUND:
            if operation.status != OperationStatus.UNKNOWN: raise DomainError(ErrorCode.INVALID_TRANSITION)
            operation.status = OperationStatus.FAILED; operation.failure_code = "REMOTE_NOT_FOUND"
        else:
            binding = self._store_found(case, operation, command.observation)
            operation.status = OperationStatus.SUCCEEDED; operation.failure_code = None; operation.confirmation = command.observation; operation.confirmed_snapshot_sequence = binding.current_observation_sequence
        now = self._now(); operation.updated_at = now
        case.revision += 1; receipt = Receipt(case_id=case.id, revision=case.revision, command_id=command.command_id, occurred_at=now)
        result = self._operation_result(operation, receipt); operation.last_result = result
        case.command_results[command.command_id] = (fp, result); self._command_case[command.command_id] = case.id
        return result

    def _validate_policy(self, case: CaseState, payload: StatusPolicyPayload) -> None:
        pairs = [(item.system, item.native_status) for item in payload.mappings]
        if len(pairs) != len(set(pairs)): raise DomainError(ErrorCode.INVALID_TRANSITION)
        mapping_ids = [item.mapping_id for item in payload.mappings]; rule_ids = [item.rule_id for item in payload.rules]
        if len(mapping_ids) != len(set(mapping_ids)) or len(rule_ids) != len(set(rule_ids)): raise DomainError(ErrorCode.INVALID_TRANSITION)
        current_ids = {item.item_id for root in case.plans.values() for item in root.current.payload.items}
        for mapping in payload.mappings:
            if mapping.system == System.JIRA and not isinstance(mapping.normalized_status, JiraStatus): raise DomainError(ErrorCode.INVALID_TRANSITION)
            if mapping.system == System.GITHUB and not isinstance(mapping.normalized_status, GitHubStatus): raise DomainError(ErrorCode.INVALID_TRANSITION)
        for rule in payload.rules:
            if (rule.selector == Selector.ITEM_IDS) != bool(rule.item_ids) or not set(rule.item_ids).issubset(current_ids): raise DomainError(ErrorCode.INVALID_TRANSITION)
            if rule.target_system == System.JIRA and not isinstance(rule.target_normalized_status, JiraStatus): raise DomainError(ErrorCode.INVALID_TRANSITION)
            if rule.target_system == System.GITHUB and not isinstance(rule.target_normalized_status, GitHubStatus): raise DomainError(ErrorCode.INVALID_TRANSITION)

    @staticmethod
    def _normalized_policy_payload(payload: StatusPolicyPayload) -> dict[str, Any]:
        value = payload.model_dump(mode="python", exclude_none=False)
        value["mappings"] = sorted(value["mappings"], key=lambda row: UUID(str(row["mapping_id"])).bytes)
        value["rules"] = sorted(value["rules"], key=lambda row: UUID(str(row["rule_id"])).bytes)
        for rule in value["rules"]:
            rule["item_ids"] = sorted(rule["item_ids"], key=lambda item: UUID(str(item)).bytes)
        return value

    def create_status_policy(self, command: CreateStatusPolicyCommand) -> StatusPolicyResult:
        case, replay, fp = self._begin("create_status_policy", command)
        if replay: return replay  # type: ignore[return-value]
        if command.acting_actor_id not in {case.pm_actor_id, case.dev_lead_actor_id}: raise DomainError(ErrorCode.AUTHORITY_REQUIRED)
        if case.policy is not None: raise DomainError(ErrorCode.INVALID_TRANSITION)
        self._validate_policy(case, command.payload)
        digest = artifact_hash(ArtifactKind.STATUS_POLICY, command.content_schema_version, command.hash_schema_version, self._normalized_policy_payload(command.payload))
        root = ArtifactRoot(ArtifactKind.STATUS_POLICY, command.policy_id); root.versions.append(ArtifactVersion(1, digest, command.content_schema_version, command.hash_schema_version, command.payload, PolicyState.READY.value)); case.policy = root
        return self._finish(case, "create_status_policy", command, fp, StatusPolicyResult, binding=root.binding, state=PolicyState.READY)  # type: ignore[return-value]

    def revise_status_policy(self, command: ReviseStatusPolicyCommand) -> StatusPolicyResult:
        case, replay, fp = self._begin("revise_status_policy", command)
        if replay: return replay  # type: ignore[return-value]
        if command.acting_actor_id not in {case.pm_actor_id, case.dev_lead_actor_id}: raise DomainError(ErrorCode.AUTHORITY_REQUIRED)
        if case.policy is None: raise DomainError(ErrorCode.RECORD_NOT_FOUND)
        if any(item.status in {OperationStatus.PENDING, OperationStatus.UNKNOWN} for item in case.operations.values()): raise DomainError(ErrorCode.INVALID_TRANSITION)
        case.policy.assert_binding(command.expected_artifact_id, command.expected_artifact_version, command.expected_artifact_hash); self._validate_policy(case, command.payload)
        digest = artifact_hash(ArtifactKind.STATUS_POLICY, command.content_schema_version, command.hash_schema_version, self._normalized_policy_payload(command.payload))
        case.policy.revise(ArtifactVersion(case.policy.current.version + 1, digest, command.content_schema_version, command.hash_schema_version, command.payload, PolicyState.READY.value))
        case.metadata["intents"] = {}
        return self._finish(case, "revise_status_policy", command, fp, StatusPolicyResult, binding=case.policy.binding, state=PolicyState.READY)  # type: ignore[return-value]

    def approve_status_policy(self, command: ApproveStatusPolicyCommand) -> ApprovalResult:
        case, replay, fp = self._begin("approve_status_policy", command)
        if replay: return replay  # type: ignore[return-value]
        if case.policy is None: raise DomainError(ErrorCode.RECORD_NOT_FOUND)
        case.policy.assert_binding(command.expected_artifact_id, command.expected_artifact_version, command.expected_artifact_hash)
        if case.policy.current.state not in {PolicyState.READY.value, PolicyState.APPROVED.value}: raise DomainError(ErrorCode.INVALID_TRANSITION)
        delegation = self._authorize(case, command.acting_actor_id, "approve_status_policy", scope=command.scope, artifact_id=case.policy.artifact_id)
        if any(item.artifact_id == case.policy.artifact_id and item.artifact_version == case.policy.current.version and item.artifact_hash == case.policy.current.semantic_hash and item.scope == command.scope and item.actor_id == command.acting_actor_id for item in case.approvals): raise DomainError(ErrorCode.APPROVAL_ALREADY_EXISTS)
        approval = ApprovalState(self.ids.new(), ArtifactKind.STATUS_POLICY.value, case.policy.artifact_id, case.policy.current.version, case.policy.current.semantic_hash, command.scope, command.acting_actor_id, delegation.id if delegation else None, delegation.delegator_id if delegation else None, delegation.later_review_required if delegation else False, self._now()); case.approvals.append(approval)
        scopes = {item.scope for item in case.approvals if item.artifact_id == case.policy.artifact_id and item.artifact_version == case.policy.current.version and item.artifact_hash == case.policy.current.semantic_hash}
        if {ApprovalScope.BUSINESS, ApprovalScope.TECHNICAL}.issubset(scopes):
            current = case.policy.current; case.policy.versions[-1] = ArtifactVersion(current.version, current.semantic_hash, current.content_schema_version, current.hash_schema_version, current.payload, PolicyState.APPROVED.value); self._derive_content_intents(case)
        return self._finish(case, "approve_status_policy", command, fp, ApprovalResult, approval_id=approval.id, scope=approval.scope, artifact_binding=case.policy.binding)  # type: ignore[return-value]

    def _derive_content_intents(self, case: CaseState) -> None:
        if case.policy is None or case.policy.current.state != PolicyState.APPROVED.value or case.package is None: return
        intents: dict[UUID, OperationIntent] = {}
        for target, root in case.plans.items():
            if root.current.state not in {PlanState.APPROVED.value, PlanState.APPLYING.value, PlanState.APPLIED.value}: continue
            for item in root.current.payload.items:
                binding_meta = case.metadata.get("external_bindings", {}).get(item.item_id)
                action = Action.CREATE if binding_meta is None else case.metadata.get("reconciliation", {}).get((root.artifact_id, root.current.version), {}).get(item.item_id)
                if action is None: continue
                system = System(target.value); generation = self._generation_key(case.id, target, item.item_id)
                labels = ["specops", "specops-retired"] if action == Action.RETIRE else ["specops"]
                if system == System.JIRA:
                    request = {"project_key": root.current.payload.project_key, "issue_type": item.kind.value, "summary": item.title, "rendered_description": render_structured_body(item.body, {}, sizing=True), "parent_key": None, "dependency_keys": [], "labels": labels, "generation_key": generation, "package_binding": case.package.binding.model_dump(mode="json"), "plan_binding": root.binding.model_dump(mode="json")}
                    jira_binding = None
                else:
                    jira_root = case.plans[PlanTarget.JIRA]
                    request = {"repository": item.repository, "title": item.title, "rendered_body": render_structured_body(item.body, {}, jira_key=item.body.jira_key, sizing=True), "labels": labels, "generation_key": generation, "jira_key": item.body.jira_key, "package_binding": case.package.binding.model_dump(mode="json"), "jira_plan_binding": jira_root.binding.model_dump(mode="json"), "plan_binding": root.binding.model_dump(mode="json")}
                    jira_binding = jira_root.binding
                owned_hash = sha256(request)
                item_ref = {"kind": "CURRENT", "item_id": item.item_id, "plan_id": root.artifact_id, "plan_version": root.current.version, "plan_hash": root.current.semantic_hash}
                fingerprint = sha256({"schema": "operation-request-v1", "case_id": case.id, "system": system, "action": action, "generation_key": generation, "item_ref": item_ref, "package_binding": case.package.binding, "plan_binding": root.binding, "status_policy_binding": case.policy.binding, "request_owned_content_hash": owned_hash, "external_identity": None, "expected_remote_revision": None, "target_normalized_status": None, "contributing_rule_ids": []})
                intent_id = uuid5(INTENT_NAMESPACE, fingerprint)
                intents[intent_id] = OperationIntent(intent_id=intent_id, system=system, item_ref=item_ref, action=action, request=request, package_binding=case.package.binding, plan_binding=root.binding, jira_plan_binding=jira_binding, status_policy_binding=case.policy.binding, request_owned_content_hash=owned_hash, fingerprint=fingerprint)
        case.metadata["intents"] = intents

    def _mapping_for(self, case: CaseState, system: System, native_status: str) -> JiraStatus | GitHubStatus | None:
        if case.policy is None or case.policy.current.state != PolicyState.APPROVED.value: return None
        return next((item.normalized_status for item in case.policy.current.payload.mappings if item.system == system and item.native_status == native_status), None)

    @staticmethod
    def _lifecycle(system: System, normalized: JiraStatus | GitHubStatus | None, owned: dict[str, Any] | None) -> Lifecycle:
        labels = (owned or {}).get("labels", [])
        if "specops-retired" in labels and ((system == System.JIRA and normalized == JiraStatus.CANCELLED) or (system == System.GITHUB and normalized == GitHubStatus.CLOSED)): return Lifecycle.RETIRED
        if "specops-retired" not in labels and normalized is not None and normalized.value != "UNKNOWN": return Lifecycle.ACTIVE
        return Lifecycle.UNKNOWN

    def _finding(self, case: CaseState, category: FindingCategory, components: list[Any], expected: Any, observed: Any) -> UUID:
        name = canonical_data({"case_id": case.id, "category": category.value, "components": components, "schema": "drift-finding-v1"})
        finding_id = uuid5(DRIFT_NAMESPACE, str(name))
        case.metadata.setdefault("findings", {})[finding_id] = {"category": category, "expected": expected, "observed": observed, "active": True, "created_at": self._now()}
        return finding_id

    def submit_remote_snapshot(self, command: SubmitRemoteSnapshotCommand) -> SnapshotResult:
        case, replay, fp = self._begin("submit_remote_snapshot", command)
        if replay: return replay  # type: ignore[return-value]
        self._authorize(case, command.acting_actor_id, "submit_remote_snapshot")
        if case.policy is None or case.policy.current.state != PolicyState.APPROVED.value: raise DomainError(ErrorCode.STATUS_POLICY_NOT_APPROVED)
        observation = command.observation
        if observation.status_policy_binding != case.policy.binding: raise DomainError(ErrorCode.STALE_ARTIFACT_BINDING)
        identity_key = self._identity_key(observation.system, observation.external_identity)
        binding = next((item for item in case.bindings.values() if item.system == observation.system and self._identity_key(item.system, item.external_identity) == identity_key and item.generation_key == observation.generation_key), None)
        if binding is None: raise DomainError(ErrorCode.RECORD_NOT_FOUND)
        latest = next((item for item in reversed(binding.snapshots) if item.remote_revision is not None), None); expected = latest.remote_revision if latest else None
        if observation.expected_previous_remote_revision != expected: raise DomainError(ErrorCode.REMOTE_VERSION_MISMATCH)
        binding.snapshots.append(observation); binding.current_observation_sequence += 1
        active: list[UUID] = []
        if observation.observation_kind == ObservationKind.NOT_FOUND:
            active.append(self._finding(case, FindingCategory.MISSING_REMOTE_ITEM, [binding.id], "FOUND", "NOT_FOUND"))
        else:
            normalized = self._mapping_for(case, observation.system, observation.native_status)
            if normalized is None: active.append(self._finding(case, FindingCategory.UNMAPPED_REMOTE_STATUS, [case.policy.binding.model_dump(mode="json"), binding.id, observation.native_status], "mapped status", observation.native_status))
            lifecycle = self._lifecycle(observation.system, normalized, observation.owned_content)
            if lifecycle == Lifecycle.UNKNOWN: active.append(self._finding(case, FindingCategory.STATUS_CONFLICT, [binding.id, "INVALID_LIFECYCLE"], "valid lifecycle", "UNKNOWN"))
            desired = next((intent for intent in case.metadata.get("intents", {}).values() if UUID(str(intent.item_ref["item_id"])) == binding.item_id), None)
            if desired is not None and sha256(observation.owned_content) != desired.request_owned_content_hash:
                active.append(self._finding(case, FindingCategory.CONTENT_DRIFT, [observation.plan_binding.model_dump(mode="json"), binding.item_id, binding.id], desired.request_owned_content_hash, sha256(observation.owned_content)))
        case.revision += 1; receipt = Receipt(case_id=case.id, revision=case.revision, command_id=command.command_id, occurred_at=self._now())
        result = SnapshotResult(observation_kind="FOUND_ACCEPTED" if observation.observation_kind == ObservationKind.FOUND else "NOT_FOUND_ACCEPTED", observation=observation, binding_id=binding.id, observation_sequence=binding.current_observation_sequence, active_finding_ids=sorted(active, key=lambda item: item.bytes), receipt=receipt)
        case.command_results[command.command_id] = (fp, result); self._command_case[command.command_id] = case.id
        return result

    def get_case_state(self, case_id: UUID) -> CaseState:
        """Internal test/repository bridge; never returns through a public read method."""
        return self._case(case_id)
