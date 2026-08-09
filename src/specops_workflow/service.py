from __future__ import annotations

import copy
import functools
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid5

from pydantic import BaseModel

from .artifacts import ArtifactRoot, ArtifactVersion
from .canonical import canonical_data, canonical_json, default_registry, sha256
from .enums import *  # noqa: F403
from .errors import DomainError, ErrorCode
from .models import *  # noqa: F403
from .ports import Clock, RandomUuidGenerator, SystemClock, UuidGenerator
from .renderer import SENTINEL_JIRA_KEY, render_structured_body
from .state import AmbiguityState, ApprovalState, BindingState, CaseState, DelegationState, OperationState

RESOLUTION_NAMESPACE = UUID("4a5cbf7e-bcff-5a85-b670-c9a38145704d")
DRIFT_NAMESPACE = UUID("08f33a1b-f1af-56d7-8a0d-8ab2a87787f8")
INTENT_NAMESPACE = UUID("b91c74fb-902f-57cb-8dc3-aa89dd91702c")
TRACE_NAMESPACE = UUID("fd829f7d-1009-5ec8-83a2-7ae5f4a28609")
LOGGER = logging.getLogger("specops_workflow")
LOGGER.addHandler(logging.NullHandler())
ALLOWED_COMMANDS = {
    "create_case", "add_participant", "grant_delegation", "revoke_delegation", "register_source_artifact",
    "record_ambiguity_finding", "resolve_ambiguity_finding", "create_spec_package", "revise_spec_package",
    "mark_spec_package_ready", "approve_spec_package", "create_projection_plan", "revise_projection_plan",
    "approve_projection_plan", "create_status_policy", "revise_status_policy", "approve_status_policy",
    "start_external_operation", "record_operation_result", "reconcile_operation", "submit_remote_snapshot",
}
DELEGATABLE_COMMANDS = {
    "register_source_artifact", "record_ambiguity_finding", "resolve_ambiguity_finding",
    "create_spec_package", "revise_spec_package", "mark_spec_package_ready", "approve_spec_package",
    "create_projection_plan", "revise_projection_plan", "approve_projection_plan", "approve_status_policy",
}


class WorkflowService:
    """Only public behavior boundary. Persistence is injected in feature 10."""

    def __init__(self, *, clock: Clock | None = None, ids: UuidGenerator | None = None, database_url: str | None = None) -> None:
        self.clock = clock or SystemClock()
        self.ids = ids or RandomUuidGenerator()
        self.registry = default_registry()
        self._store = None
        database_url = database_url or os.environ.get("SPECOPS_DATABASE_URL")
        if database_url is not None:
            from .persistence import SqlAlchemyStore
            self._store = SqlAlchemyStore(database_url, registry=self.registry)
        self._cases: dict[UUID, CaseState] = self._store.load_cases() if self._store else {}
        self._command_case: dict[UUID, UUID] = {
            command_id: case.id
            for case in self._cases.values()
            for command_id in case.command_results
        }

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
            return case, ReplayResult(stored_result=prior[1]), fingerprint
        if command.expected_case_revision != case.revision: raise DomainError(ErrorCode.STALE_ARTIFACT_BINDING)
        return copy.deepcopy(case), None, fingerprint

    def _finish(self, case: CaseState, name: str, command: BaseModel, fingerprint: str, result_type: type[BaseModel], **values: Any) -> BaseModel:
        case.revision += 1
        receipt = Receipt(case_id=case.id, revision=case.revision, command_id=command.command_id, occurred_at=self._now())
        result = result_type(receipt=receipt, **values)
        case.command_results[command.command_id] = (fingerprint, result)
        self._persist_result(case, name, command, fingerprint, result)
        return result

    def _persist_result(self, case: CaseState, name: str, command: BaseModel, fingerprint: str, result: BaseModel) -> None:
        # Persist the complete deterministic current trace after every mutation.
        # Historical version-scoped rows remain append-only in the relational store.
        self._refresh_core_findings(case)
        edges = self._trace_edges(case)
        if len(edges) > 200_000 or len(self._active_findings(case)) > 200_000:
            code = ErrorCode.INVALID_PROJECTION_PLAN if name in {"create_projection_plan", "revise_projection_plan"} else ErrorCode.INVALID_TRANSITION
            raise DomainError(code)
        case.metadata["trace_edges"] = {edge.id: edge for edge in edges}
        if self._store:
            self._store.save_case_and_audit(case, event_id=self.ids.new(), command_name=name, command_id=command.command_id, fingerprint=fingerprint, actor=command.acting_actor_id, occurred_at=result.receipt.occurred_at, result=result)
        self._cases[case.id] = case
        self._command_case[command.command_id] = case.id

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
        candidates = [item for item in case.delegations.values() if item.delegate_id == actor]
        if not candidates and any(
            item.delegate_id == actor and command_name in item.command_names
            for other_case in self._cases.values()
            if other_case.id != case.id
            for item in other_case.delegations.values()
        ):
            raise DomainError(ErrorCode.DELEGATION_SCOPE_MISMATCH)
        now = self._now()
        active = [item for item in candidates if item.revoked_at is None and item.valid_from <= now <= item.valid_until]
        if candidates and not active: raise DomainError(ErrorCode.DELEGATION_NOT_ACTIVE)
        expected_kind = {
            "create_spec_package": ArtifactKind.SPEC_PACKAGE.value,
            "revise_spec_package": ArtifactKind.SPEC_PACKAGE.value,
            "mark_spec_package_ready": ArtifactKind.SPEC_PACKAGE.value,
            "approve_spec_package": ArtifactKind.SPEC_PACKAGE.value,
            "create_projection_plan": ArtifactKind.PROJECTION_PLAN.value,
            "revise_projection_plan": ArtifactKind.PROJECTION_PLAN.value,
            "approve_projection_plan": ArtifactKind.PROJECTION_PLAN.value,
            "approve_status_policy": ArtifactKind.STATUS_POLICY.value,
        }.get(command_name)
        for item in active:
            if command_name not in item.command_names: continue
            if scope is not None and item.domain.value != scope.value: continue
            if item.artifact_kind is not None and item.artifact_kind != expected_kind: continue
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
            return ReplayResult(stored_result=prior[1])  # type: ignore[return-value]
        if command.acting_actor_id != command.pm_actor_id: raise DomainError(ErrorCode.AUTHORITY_REQUIRED)
        if command.pm_actor_id == command.dev_lead_actor_id: raise DomainError(ErrorCode.AUTHORITY_SLOT_OCCUPIED)
        if command.case_id in self._cases: raise DomainError(ErrorCode.AUTHORITY_SLOT_OCCUPIED)
        case = CaseState(command.case_id, command.pm_actor_id, command.dev_lead_actor_id, self._now())
        case.participants.update({command.pm_actor_id, command.dev_lead_actor_id})
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
        if command.valid_from > command.valid_until or any(name not in DELEGATABLE_COMMANDS for name in command.command_names): raise DomainError(ErrorCode.INVALID_TRANSITION)
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
        scopes = self._required_scopes(finding.domain)
        actor_scope = self._authority_scope(case, command.acting_actor_id) if command.acting_actor_id != "SYSTEM" else None
        delegation = None
        if actor_scope not in scopes:
            candidates = [
                d for d in case.delegations.values()
                if d.delegate_id == command.acting_actor_id
                and "resolve_ambiguity_finding" in d.command_names
                and d.domain.value in {scope.value for scope in scopes}
            ]
            if not candidates:
                self._authorize(case, command.acting_actor_id, "resolve_ambiguity_finding", scope=next(iter(scopes)))
                raise DomainError(ErrorCode.DELEGATION_SCOPE_MISMATCH)
            actor_scope = ApprovalScope(candidates[0].domain.value)
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
            record = ResolutionRecord(resolution_id=uuid5(RESOLUTION_NAMESPACE, canonical_json(name).decode("utf-8")), scope=actor_scope, actor_id=command.acting_actor_id, delegation_id=delegation.id if delegation else None, delegator_id=delegation.delegator_id if delegation else None, later_review_required=delegation.later_review_required if delegation else False, resolution_text=command.resolution_text, resolution_source_refs=command.resolution_source_refs, recorded_at=now)
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

    def _validate_package_shape(self, case: CaseState, payload: SpecPackagePayload, *, ready: bool, package_id: UUID | None = None) -> None:
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
                now = self._now()
                if (
                    delegation is None
                    or delegation.domain != Domain.TECHNICAL
                    or not delegation.later_review_required
                    or delegation.revoked_at is not None
                    or not delegation.valid_from <= now <= delegation.valid_until
                    or (delegation.artifact_id is not None and delegation.artifact_id != package_id)
                    or (delegation.artifact_kind is not None and delegation.artifact_kind != ArtifactKind.SPEC_PACKAGE.value)
                ):
                    raise DomainError(ErrorCode.INVALID_TRANSITION)
        if ready and any(item.status == FindingStatus.OPEN and item.severity == Severity.BLOCKING.value for item in case.ambiguities.values()):
            raise DomainError(ErrorCode.BLOCKING_FINDING)

    def create_spec_package(self, command: CreateSpecPackageCommand) -> SpecPackageResult:
        case, replay, fp = self._begin("create_spec_package", command)
        if replay: return replay  # type: ignore[return-value]
        self._authorize(case, command.acting_actor_id, "create_spec_package", artifact_id=command.package_id)
        if case.package is not None: raise DomainError(ErrorCode.INVALID_TRANSITION)
        self._validate_package_shape(case, command.payload, ready=False, package_id=command.package_id)
        digest = self.registry.hash(ArtifactKind.SPEC_PACKAGE, command.content_schema_version, command.hash_schema_version, self._normalized_package_payload(command.payload))
        root = ArtifactRoot(ArtifactKind.SPEC_PACKAGE, command.package_id)
        root.versions.append(ArtifactVersion(1, digest, command.content_schema_version, command.hash_schema_version, copy.deepcopy(command.payload), PackageState.DRAFT.value))
        case.package = root
        return self._finish(case, "create_spec_package", command, fp, SpecPackageResult, binding=root.binding, state=PackageState.DRAFT)  # type: ignore[return-value]

    def revise_spec_package(self, command: ReviseSpecPackageCommand) -> SpecPackageResult:
        case, replay, fp = self._begin("revise_spec_package", command)
        if replay: return replay  # type: ignore[return-value]
        if case.package is None: raise DomainError(ErrorCode.RECORD_NOT_FOUND)
        self._authorize(case, command.acting_actor_id, "revise_spec_package", artifact_id=case.package.artifact_id)
        case.package.assert_binding(command.expected_artifact_id, command.expected_artifact_version, command.expected_artifact_hash)
        if any(item.status in {OperationStatus.PENDING, OperationStatus.UNKNOWN} for item in case.operations.values()):
            raise DomainError(ErrorCode.INVALID_TRANSITION)
        prior_package_binding = case.package.binding
        self._validate_package_shape(case, command.payload, ready=False, package_id=case.package.artifact_id)
        digest = self.registry.hash(ArtifactKind.SPEC_PACKAGE, command.content_schema_version, command.hash_schema_version, self._normalized_package_payload(command.payload))
        case.package.revise(ArtifactVersion(case.package.current.version + 1, digest, command.content_schema_version, command.hash_schema_version, copy.deepcopy(command.payload), PackageState.DRAFT.value))
        for plan in case.plans.values():
            current = plan.current; plan.versions[-1] = ArtifactVersion(current.version, current.semantic_hash, current.content_schema_version, current.hash_schema_version, current.payload, PlanState.STALE.value)
            self._finding(case, FindingCategory.STALE_BINDING, [prior_package_binding.model_dump(mode="json"), case.package.binding.model_dump(mode="json")], BindingValue(value=prior_package_binding), BindingValue(value=case.package.binding), BindingValue(value=prior_package_binding))
        return self._finish(case, "revise_spec_package", command, fp, SpecPackageResult, binding=case.package.binding, state=PackageState.DRAFT)  # type: ignore[return-value]

    def mark_spec_package_ready(self, command: MarkSpecPackageReadyCommand) -> SpecPackageResult:
        case, replay, fp = self._begin("mark_spec_package_ready", command)
        if replay: return replay  # type: ignore[return-value]
        if case.package is None: raise DomainError(ErrorCode.RECORD_NOT_FOUND)
        self._authorize(case, command.acting_actor_id, "mark_spec_package_ready", artifact_id=case.package.artifact_id)
        case.package.assert_binding(command.expected_artifact_id, command.expected_artifact_version, command.expected_artifact_hash)
        current = case.package.current
        if current.state != PackageState.DRAFT.value: raise DomainError(ErrorCode.INVALID_TRANSITION)
        self._validate_package_shape(case, current.payload, ready=True, package_id=case.package.artifact_id)
        case.package.versions[-1] = ArtifactVersion(current.version, current.semantic_hash, current.content_schema_version, current.hash_schema_version, current.payload, PackageState.READY.value)
        return self._finish(case, "mark_spec_package_ready", command, fp, SpecPackageResult, binding=case.package.binding, state=PackageState.READY)  # type: ignore[return-value]

    def approve_spec_package(self, command: ApproveSpecPackageCommand) -> ApprovalResult:
        case, replay, fp = self._begin("approve_spec_package", command)
        if replay: return replay  # type: ignore[return-value]
        if case.package is None: raise DomainError(ErrorCode.RECORD_NOT_FOUND)
        case.package.assert_binding(command.expected_artifact_id, command.expected_artifact_version, command.expected_artifact_hash)
        current = case.package.current
        if any(item.artifact_id == case.package.artifact_id and item.artifact_version == current.version and item.artifact_hash == current.semantic_hash and item.scope == command.scope and item.actor_id == command.acting_actor_id for item in case.approvals):
            raise DomainError(ErrorCode.APPROVAL_ALREADY_EXISTS)
        if any(item.status == FindingStatus.OPEN and item.severity == Severity.BLOCKING.value for item in case.ambiguities.values()):
            raise DomainError(ErrorCode.BLOCKING_FINDING)
        if current.state not in {PackageState.READY.value, PackageState.APPROVED.value}: raise DomainError(ErrorCode.INVALID_TRANSITION)
        delegation = self._authorize(case, command.acting_actor_id, "approve_spec_package", scope=command.scope, artifact_id=case.package.artifact_id)
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
        value = item.model_dump(mode="python", exclude_none=False)
        value["source_unit_ids"] = sorted(value["source_unit_ids"], key=lambda unit: unit.bytes)
        value["dependency_item_ids"] = sorted(value["dependency_item_ids"], key=lambda unit: unit.bytes)
        value["body"]["source_unit_ids"] = sorted(value["body"]["source_unit_ids"], key=lambda unit: unit.bytes)
        value["body"]["dependency_item_ids"] = sorted(value["body"]["dependency_item_ids"], key=lambda unit: unit.bytes)
        value["body"]["acceptance_checks"] = sorted(value["body"]["acceptance_checks"], key=lambda row: row["check_id"].bytes)
        return sha256({"schema": "projection-item-v1", "target": target.value, "project_key": project_key, "item": value})

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
                primary_binding = next(
                    (
                        value for value in case.bindings.values()
                        if value.system == System.JIRA
                        and value.plan_id == jira_root.artifact_id
                        and value.item_id == item.primary_jira_item_id
                    ),
                    None,
                )
                binding = case.metadata.get("external_bindings", {}).get(item.primary_jira_item_id)
                jira_key = binding.get("key") if isinstance(binding, dict) else None
                if primary_binding is None or primary_binding.current_plan_version != jira_root.current.version:
                    raise DomainError(ErrorCode.STALE_ARTIFACT_BINDING)
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
        digest = self.registry.hash(ArtifactKind.PROJECTION_PLAN, command.content_schema_version, command.hash_schema_version, self._normalized_plan_payload(command.payload))
        root = ArtifactRoot(ArtifactKind.PROJECTION_PLAN, command.plan_id)
        root.versions.append(ArtifactVersion(1, digest, command.content_schema_version, command.hash_schema_version, copy.deepcopy(command.payload), PlanState.READY.value))
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
        if any(item.status in {OperationStatus.PENDING, OperationStatus.UNKNOWN} and item.intent.plan_binding.artifact_id == root.artifact_id for item in case.operations.values()):
            raise DomainError(ErrorCode.INVALID_TRANSITION)
        prior_plan_binding = root.binding
        prior_package_binding = root.current.payload.package_binding
        new_hashes = self._validate_plan(case, command.payload)
        digest = self.registry.hash(ArtifactKind.PROJECTION_PLAN, command.content_schema_version, command.hash_schema_version, self._normalized_plan_payload(command.payload))
        prior_items = {item.item_id: item for item in root.current.payload.items}; new_items = {item.item_id: item for item in command.payload.items}
        prior_hashes = case.metadata["item_hashes"][(root.artifact_id, root.current.version)]
        prior_tombstones = case.metadata.get("tombstones", {}).get(
            (root.artifact_id, root.current.version), {}
        )
        tombstones = copy.deepcopy(prior_tombstones)
        for item_id in set(tombstones) & set(new_items):
            tombstones.pop(item_id)
        reconciliation = {item_id: Action.RETIRE for item_id in tombstones}
        for item_id in set(prior_items) | set(new_items):
            if item_id not in prior_items: reconciliation[item_id] = Action.CREATE
            elif item_id not in new_items:
                if item_id in case.metadata.get("external_bindings", {}):
                    reconciliation[item_id] = Action.RETIRE
                    tombstones[item_id] = {
                        "item": copy.deepcopy(prior_items[item_id]),
                        "prior_plan_version": root.current.version,
                        "prior_plan_hash": root.current.semantic_hash,
                    }
                else:
                    reconciliation[item_id] = None
            elif prior_hashes[item_id] == new_hashes[item_id]: reconciliation[item_id] = None
            else: reconciliation[item_id] = Action.UPDATE
        root.revise(ArtifactVersion(root.current.version + 1, digest, command.content_schema_version, command.hash_schema_version, copy.deepcopy(command.payload), PlanState.READY.value))
        case.metadata.setdefault("item_hashes", {})[(root.artifact_id, root.current.version)] = new_hashes
        case.metadata.setdefault("reconciliation", {})[(root.artifact_id, root.current.version)] = reconciliation
        case.metadata.setdefault("tombstones", {})[(root.artifact_id, root.current.version)] = tombstones
        case.metadata["intents"] = {
            intent_id: intent
            for intent_id, intent in case.metadata.get("intents", {}).items()
            if intent.plan_binding.artifact_id != root.artifact_id
        }
        if prior_package_binding != case.package.binding:
            self._resolve_finding(case, FindingCategory.STALE_BINDING, [prior_package_binding.model_dump(mode="json"), case.package.binding.model_dump(mode="json")])
        if command.payload.target == PlanTarget.JIRA and PlanTarget.GITHUB in case.plans:
            gh = case.plans[PlanTarget.GITHUB]; current = gh.current; gh.versions[-1] = ArtifactVersion(current.version, current.semantic_hash, current.content_schema_version, current.hash_schema_version, current.payload, PlanState.STALE.value)
            self._finding(case, FindingCategory.STALE_BINDING, [prior_plan_binding.model_dump(mode="json"), root.binding.model_dump(mode="json")], BindingValue(value=prior_plan_binding), BindingValue(value=root.binding), BindingValue(value=prior_plan_binding))
        return self._finish(case, "revise_projection_plan", command, fp, ProjectionPlanResult, binding=root.binding, target=command.payload.target, state=PlanState.READY, derived_intent_ids=[])  # type: ignore[return-value]

    def approve_projection_plan(self, command: ApproveProjectionPlanCommand) -> ApprovalResult:
        case, replay, fp = self._begin("approve_projection_plan", command)
        if replay: return replay  # type: ignore[return-value]
        root = next((plan for plan in case.plans.values() if plan.artifact_id == command.expected_artifact_id), None)
        if root is None: raise DomainError(ErrorCode.RECORD_NOT_FOUND)
        root.assert_binding(command.expected_artifact_id, command.expected_artifact_version, command.expected_artifact_hash)
        payload: ProjectionPlanPayload = root.current.payload
        required = {ApprovalScope.TECHNICAL} if payload.target == PlanTarget.GITHUB else ({ApprovalScope.BUSINESS} if any(item.domain in {Domain.BUSINESS, Domain.CROSS_DOMAIN} for item in payload.items) else set()) | ({ApprovalScope.TECHNICAL} if any(item.domain in {Domain.TECHNICAL, Domain.CROSS_DOMAIN} for item in payload.items) else set())
        if command.scope not in required: raise DomainError(ErrorCode.APPROVAL_BINDING_MISMATCH)
        if any(item.artifact_id == root.artifact_id and item.artifact_version == root.current.version and item.artifact_hash == root.current.semantic_hash and item.scope == command.scope and item.actor_id == command.acting_actor_id for item in case.approvals): raise DomainError(ErrorCode.APPROVAL_ALREADY_EXISTS)
        pending_direct_review = any(
            item.artifact_id == root.artifact_id
            and item.artifact_version == root.current.version
            and item.artifact_hash == root.current.semantic_hash
            and item.scope == command.scope
            and item.delegator_id == command.acting_actor_id
            and item.later_review_required
            for item in case.approvals
        )
        if root.current.state not in {PlanState.READY.value, PlanState.APPROVED.value} and not pending_direct_review:
            raise DomainError(ErrorCode.INVALID_TRANSITION)
        delegation = self._authorize(case, command.acting_actor_id, "approve_projection_plan", scope=command.scope, artifact_id=root.artifact_id)
        approval = ApprovalState(self.ids.new(), ArtifactKind.PROJECTION_PLAN.value, root.artifact_id, root.current.version, root.current.semantic_hash, command.scope, command.acting_actor_id, delegation.id if delegation else None, delegation.delegator_id if delegation else None, delegation.later_review_required if delegation else False, self._now())
        case.approvals.append(approval)
        scopes = {item.scope for item in case.approvals if item.artifact_id == root.artifact_id and item.artifact_version == root.current.version and item.artifact_hash == root.current.semantic_hash}
        if required.issubset(scopes) and root.current.state in {PlanState.READY.value, PlanState.APPROVED.value}:
            current = root.current; root.versions[-1] = ArtifactVersion(current.version, current.semantic_hash, current.content_schema_version, current.hash_schema_version, current.payload, PlanState.APPROVED.value)
            if case.policy is not None and case.policy.current.state == PolicyState.APPROVED.value:
                self._derive_content_intents(case)
        return self._finish(case, "approve_projection_plan", command, fp, ApprovalResult, approval_id=approval.id, scope=approval.scope, artifact_binding=root.binding)  # type: ignore[return-value]

    def _operation_result(self, operation: OperationState, receipt: Receipt) -> OperationResult:
        return OperationResult(operation_id=operation.id, intent_id=operation.intent.intent_id, system=operation.intent.system, action=operation.intent.action, status=operation.status, attempt=operation.attempt, failure_code=operation.failure_code, confirmation=operation.confirmation, confirmed_snapshot_sequence=operation.confirmed_snapshot_sequence, receipt=receipt)

    @staticmethod
    def _set_plan_state(case: CaseState, plan_id: UUID, state: PlanState) -> None:
        root = next((value for value in case.plans.values() if value.artifact_id == plan_id), None)
        if root is None or root.current.state == PlanState.STALE.value:
            return
        current = root.current
        root.versions[-1] = ArtifactVersion(current.version, current.semantic_hash, current.content_schema_version, current.hash_schema_version, current.payload, state.value)

    def _refresh_plan_state(self, case: CaseState, plan_id: UUID) -> None:
        intents = [
            intent for intent in case.metadata.get("intents", {}).values()
            if intent.plan_binding.artifact_id == plan_id and intent.action != Action.TRANSITION_STATUS
        ]
        if not intents:
            return
        succeeded = {
            operation.intent.intent_id for operation in case.operations.values()
            if operation.status == OperationStatus.SUCCEEDED
        }
        if all(intent.intent_id in succeeded for intent in intents):
            self._set_plan_state(case, plan_id, PlanState.APPLIED)
        elif any(operation.intent.plan_binding.artifact_id == plan_id for operation in case.operations.values()):
            self._set_plan_state(case, plan_id, PlanState.APPLYING)

    def start_external_operation(self, command: StartExternalOperationCommand) -> OperationResult:
        case, replay, fp = self._begin("start_external_operation", command)
        if replay: return replay  # type: ignore[return-value]
        self._authorize(case, command.acting_actor_id, "start_external_operation")
        existing_operation = case.operations.get(command.operation_id)
        if existing_operation is not None and existing_operation.status in {OperationStatus.PENDING, OperationStatus.UNKNOWN}:
            raise DomainError(ErrorCode.OPERATION_RETRY_BLOCKED)
        active = self._active_findings(case)
        if any(value["category"] == FindingCategory.UNMAPPED_REMOTE_STATUS for value in active.values()):
            raise DomainError(ErrorCode.UNMAPPED_REMOTE_STATUS)
        if any(value["category"] != FindingCategory.STATUS_SYNC_REQUIRED for value in active.values()):
            raise DomainError(ErrorCode.BLOCKING_FINDING)
        intent: OperationIntent | None = case.metadata.get("intents", {}).get(command.intent_id)
        if intent is None: raise DomainError(ErrorCode.RECORD_NOT_FOUND)
        global_idempotent = next(
            (
                operation
                for stored_case in [case, *(value for value in self._cases.values() if value.id != case.id)]
                for operation in stored_case.operations.values()
                if operation.idempotency_key == command.idempotency_key
            ),
            None,
        )
        if global_idempotent is not None and (
            global_idempotent.id != command.operation_id
            or global_idempotent.intent.intent_id != command.intent_id
            or global_idempotent.intent.fingerprint != intent.fingerprint
        ):
            raise DomainError(ErrorCode.IDEMPOTENCY_CONFLICT)
        idempotent = next((item for item in case.operations.values() if item.idempotency_key == command.idempotency_key), None)
        by_intent = next((item for item in case.operations.values() if item.intent.intent_id == command.intent_id), None)
        operation = case.operations.get(command.operation_id)
        if idempotent is not None and idempotent.id != command.operation_id: raise DomainError(ErrorCode.IDEMPOTENCY_CONFLICT)
        if by_intent is not None and by_intent.id != command.operation_id: raise DomainError(ErrorCode.IDEMPOTENCY_CONFLICT)
        now = self._now()
        if operation is None:
            operation = OperationState(command.operation_id, intent, command.idempotency_key, OperationStatus.PENDING, 1, now, now, attempt_started_at=now)
            case.operations[operation.id] = operation
        else:
            if operation.intent.intent_id != command.intent_id or operation.idempotency_key != command.idempotency_key or operation.intent.fingerprint != intent.fingerprint: raise DomainError(ErrorCode.IDEMPOTENCY_CONFLICT)
            if operation.status in {OperationStatus.PENDING, OperationStatus.UNKNOWN}: raise DomainError(ErrorCode.OPERATION_RETRY_BLOCKED)
            if operation.status == OperationStatus.SUCCEEDED and operation.last_result is not None:
                case.command_results[command.command_id] = (fp, operation.last_result)
                self._cases[case.id] = case
                self._command_case[command.command_id] = case.id
                return ReplayResult(stored_result=operation.last_result)  # type: ignore[return-value]
            operation.status = OperationStatus.PENDING; operation.attempt += 1; operation.failure_code = None; operation.updated_at = now; operation.attempt_started_at = now
        if intent.action != Action.TRANSITION_STATUS:
            self._set_plan_state(case, intent.plan_binding.artifact_id, PlanState.APPLYING)
        case.revision += 1
        receipt = Receipt(case_id=case.id, revision=case.revision, command_id=command.command_id, occurred_at=now)
        result = self._operation_result(operation, receipt); operation.last_result = result
        case.command_results[command.command_id] = (fp, result)
        self._persist_result(case, "start_external_operation", command, fp, result)
        return result

    @staticmethod
    def _identity_key(system: System, identity: ExternalIdentity) -> str:
        if system == System.JIRA and isinstance(identity, JiraIdentity): return identity.key
        if system == System.GITHUB and isinstance(identity, GitHubIdentity): return identity.node_id
        raise DomainError(ErrorCode.UNCONFIRMED_EXTERNAL_ID)

    def _matching_observation(self, case: CaseState, operation: OperationState, observation: RemoteObservation) -> BindingState:
        intent = operation.intent
        if observation.observation_kind != ObservationKind.FOUND: raise DomainError(ErrorCode.UNKNOWN_EXTERNAL_RESULT)
        item_id = UUID(str(intent.item_ref["item_id"]))
        existing = next((item for item in case.bindings.values() if item.system == intent.system and item.plan_id == intent.plan_binding.artifact_id and item.item_id == item_id), None)
        if existing is not None:
            latest = next((snap for snap in reversed(existing.snapshots) if snap.remote_revision is not None), None)
            expected = latest.remote_revision if latest else None
            if observation.expected_previous_remote_revision != expected: raise DomainError(ErrorCode.REMOTE_VERSION_MISMATCH)
        if observation.system != intent.system or observation.generation_key != intent.request.get("generation_key") or observation.package_binding != intent.package_binding or observation.plan_binding != intent.plan_binding or observation.jira_plan_binding != intent.jira_plan_binding or observation.status_policy_binding != intent.status_policy_binding:
            raise DomainError(ErrorCode.UNCONFIRMED_EXTERNAL_ID)
        if observation.system == System.GITHUB:
            jira_root = case.plans.get(PlanTarget.JIRA)
            if jira_root is None or observation.jira_plan_binding != jira_root.binding:
                raise DomainError(ErrorCode.UNCONFIRMED_EXTERNAL_ID)
        if sha256(observation.owned_content) != intent.request_owned_content_hash: raise DomainError(ErrorCode.UNKNOWN_EXTERNAL_RESULT)
        recomputed = sha256({"schema": "operation-request-v1", "case_id": case.id, "system": intent.system, "action": intent.action, "generation_key": observation.generation_key, "item_ref": intent.item_ref, "package_binding": intent.package_binding, "plan_binding": intent.plan_binding, "status_policy_binding": intent.status_policy_binding, "request_owned_content_hash": intent.request_owned_content_hash, "external_identity": intent.existing, "expected_remote_revision": intent.expected, "target_normalized_status": intent.target_normalized_status, "contributing_rule_ids": intent.contributing_rule_ids})
        if recomputed != intent.fingerprint:
            raise DomainError(ErrorCode.UNKNOWN_EXTERNAL_RESULT)
        normalized = self._mapping_for(case, observation.system, observation.native_status)
        lifecycle = self._lifecycle(observation.system, normalized, observation.owned_content)
        if intent.action in {Action.CREATE, Action.UPDATE} and lifecycle != Lifecycle.ACTIVE:
            raise DomainError(ErrorCode.UNKNOWN_EXTERNAL_RESULT)
        if intent.action == Action.RETIRE and lifecycle != Lifecycle.RETIRED:
            raise DomainError(ErrorCode.UNKNOWN_EXTERNAL_RESULT)
        if intent.action == Action.TRANSITION_STATUS and normalized != intent.target_normalized_status:
            raise DomainError(ErrorCode.UNKNOWN_EXTERNAL_RESULT)
        identity_key = self._identity_key(intent.system, observation.external_identity)
        if (
            intent.action == Action.CREATE
            and existing is not None
            and operation.status != OperationStatus.SUCCEEDED
        ):
            raise DomainError(ErrorCode.UNCONFIRMED_EXTERNAL_ID)
        if intent.action != Action.CREATE and existing is None: raise DomainError(ErrorCode.UNCONFIRMED_EXTERNAL_ID)
        if existing is not None:
            if self._identity_key(intent.system, existing.external_identity) != identity_key: raise DomainError(ErrorCode.UNCONFIRMED_EXTERNAL_ID)
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
        components = [operation.id, operation.attempt]
        if operation.status == OperationStatus.UNKNOWN:
            self._finding(case, FindingCategory.UNKNOWN_EXTERNAL_RESULT, components, EntityValue(entity_kind=EntityKind.EXTERNAL_OPERATION, id=operation.id), OperationStateValue(value=OperationStatus.SUCCEEDED), OperationStateValue(value=OperationStatus.UNKNOWN))
        else:
            self._resolve_finding(case, FindingCategory.UNKNOWN_EXTERNAL_RESULT, components)
        operation.updated_at = now
        if operation.status == OperationStatus.SUCCEEDED:
            self._derive_content_intents(case)
        self._refresh_plan_state(case, operation.intent.plan_binding.artifact_id)
        case.revision += 1; receipt = Receipt(case_id=case.id, revision=case.revision, command_id=command.command_id, occurred_at=now)
        result = self._operation_result(operation, receipt); operation.last_result = result
        case.command_results[command.command_id] = (fp, result)
        self._persist_result(case, "record_operation_result", command, fp, result)
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
            observation = command.observation
            intent = operation.intent
            if observation.system != intent.system or observation.generation_key != intent.request.get("generation_key") or observation.package_binding != intent.package_binding or observation.plan_binding != intent.plan_binding or observation.jira_plan_binding != intent.jira_plan_binding or observation.status_policy_binding != intent.status_policy_binding:
                raise DomainError(ErrorCode.UNCONFIRMED_EXTERNAL_ID)
            identity_key = self._identity_key(observation.system, observation.external_identity)
            binding = next((item for item in case.bindings.values() if item.system == observation.system and item.generation_key == observation.generation_key and self._identity_key(item.system, item.external_identity) == identity_key), None)
            if binding is None: raise DomainError(ErrorCode.UNCONFIRMED_EXTERNAL_ID)
            latest = next((snapshot for snapshot in reversed(binding.snapshots) if snapshot.remote_revision is not None), None)
            expected = latest.remote_revision if latest else None
            if observation.expected_previous_remote_revision != expected: raise DomainError(ErrorCode.REMOTE_VERSION_MISMATCH)
            binding.snapshots.append(observation); binding.current_observation_sequence += 1
            self._finding(case, FindingCategory.MISSING_REMOTE_ITEM, [binding.id], EntityValue(entity_kind=EntityKind.EXTERNAL_BINDING, id=binding.id), ExternalValue(value=binding.external_identity), MissingValue())
            operation.status = OperationStatus.FAILED; operation.failure_code = "REMOTE_NOT_FOUND"
        else:
            binding = self._store_found(case, operation, command.observation)
            operation.status = OperationStatus.SUCCEEDED; operation.failure_code = None; operation.confirmation = command.observation; operation.confirmed_snapshot_sequence = binding.current_observation_sequence
        self._resolve_finding(case, FindingCategory.UNKNOWN_EXTERNAL_RESULT, [operation.id, operation.attempt])
        now = self._now(); operation.updated_at = now
        if operation.status == OperationStatus.SUCCEEDED:
            self._derive_content_intents(case)
        self._refresh_plan_state(case, operation.intent.plan_binding.artifact_id)
        case.revision += 1; receipt = Receipt(case_id=case.id, revision=case.revision, command_id=command.command_id, occurred_at=now)
        result = self._operation_result(operation, receipt); operation.last_result = result
        case.command_results[command.command_id] = (fp, result)
        self._persist_result(case, "reconcile_operation", command, fp, result)
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
        condition_order = {kind: index for index, kind in enumerate(ConditionKind)}
        jira_order = {status: index for index, status in enumerate(JiraStatus)}
        github_order = {status: index for index, status in enumerate(GitHubStatus)}
        for rule in value["rules"]:
            rule["item_ids"] = sorted(rule["item_ids"], key=lambda item: UUID(str(item)).bytes)
            for condition in rule["all_of"]:
                condition["jira_states"] = sorted(condition["jira_states"], key=jira_order.__getitem__)
                condition["github_states"] = sorted(condition["github_states"], key=github_order.__getitem__)
            rule["all_of"] = sorted(rule["all_of"], key=lambda condition: condition_order[condition["kind"]])
        return value

    def create_status_policy(self, command: CreateStatusPolicyCommand) -> StatusPolicyResult:
        case, replay, fp = self._begin("create_status_policy", command)
        if replay: return replay  # type: ignore[return-value]
        if command.acting_actor_id not in {case.pm_actor_id, case.dev_lead_actor_id}: raise DomainError(ErrorCode.AUTHORITY_REQUIRED)
        if case.policy is not None: raise DomainError(ErrorCode.INVALID_TRANSITION)
        self._validate_policy(case, command.payload)
        digest = self.registry.hash(ArtifactKind.STATUS_POLICY, command.content_schema_version, command.hash_schema_version, self._normalized_policy_payload(command.payload))
        root = ArtifactRoot(ArtifactKind.STATUS_POLICY, command.policy_id); root.versions.append(ArtifactVersion(1, digest, command.content_schema_version, command.hash_schema_version, copy.deepcopy(command.payload), PolicyState.READY.value)); case.policy = root
        return self._finish(case, "create_status_policy", command, fp, StatusPolicyResult, binding=root.binding, state=PolicyState.READY)  # type: ignore[return-value]

    def revise_status_policy(self, command: ReviseStatusPolicyCommand) -> StatusPolicyResult:
        case, replay, fp = self._begin("revise_status_policy", command)
        if replay: return replay  # type: ignore[return-value]
        if command.acting_actor_id not in {case.pm_actor_id, case.dev_lead_actor_id}: raise DomainError(ErrorCode.AUTHORITY_REQUIRED)
        if case.policy is None: raise DomainError(ErrorCode.RECORD_NOT_FOUND)
        if any(item.status in {OperationStatus.PENDING, OperationStatus.UNKNOWN} for item in case.operations.values()): raise DomainError(ErrorCode.INVALID_TRANSITION)
        case.policy.assert_binding(command.expected_artifact_id, command.expected_artifact_version, command.expected_artifact_hash); self._validate_policy(case, command.payload)
        digest = self.registry.hash(ArtifactKind.STATUS_POLICY, command.content_schema_version, command.hash_schema_version, self._normalized_policy_payload(command.payload))
        case.policy.revise(ArtifactVersion(case.policy.current.version + 1, digest, command.content_schema_version, command.hash_schema_version, copy.deepcopy(command.payload), PolicyState.READY.value))
        case.metadata["intents"] = {}
        return self._finish(case, "revise_status_policy", command, fp, StatusPolicyResult, binding=case.policy.binding, state=PolicyState.READY)  # type: ignore[return-value]

    def approve_status_policy(self, command: ApproveStatusPolicyCommand) -> ApprovalResult:
        case, replay, fp = self._begin("approve_status_policy", command)
        if replay: return replay  # type: ignore[return-value]
        if case.policy is None: raise DomainError(ErrorCode.RECORD_NOT_FOUND)
        case.policy.assert_binding(command.expected_artifact_id, command.expected_artifact_version, command.expected_artifact_hash)
        if any(item.artifact_id == case.policy.artifact_id and item.artifact_version == case.policy.current.version and item.artifact_hash == case.policy.current.semantic_hash and item.scope == command.scope and item.actor_id == command.acting_actor_id for item in case.approvals): raise DomainError(ErrorCode.APPROVAL_ALREADY_EXISTS)
        if case.policy.current.state not in {PolicyState.READY.value, PolicyState.APPROVED.value}: raise DomainError(ErrorCode.INVALID_TRANSITION)
        delegation = self._authorize(case, command.acting_actor_id, "approve_status_policy", scope=command.scope, artifact_id=case.policy.artifact_id)
        approval = ApprovalState(self.ids.new(), ArtifactKind.STATUS_POLICY.value, case.policy.artifact_id, case.policy.current.version, case.policy.current.semantic_hash, command.scope, command.acting_actor_id, delegation.id if delegation else None, delegation.delegator_id if delegation else None, delegation.later_review_required if delegation else False, self._now()); case.approvals.append(approval)
        scopes = {item.scope for item in case.approvals if item.artifact_id == case.policy.artifact_id and item.artifact_version == case.policy.current.version and item.artifact_hash == case.policy.current.semantic_hash}
        if {ApprovalScope.BUSINESS, ApprovalScope.TECHNICAL}.issubset(scopes):
            current = case.policy.current; case.policy.versions[-1] = ArtifactVersion(current.version, current.semantic_hash, current.content_schema_version, current.hash_schema_version, current.payload, PolicyState.APPROVED.value); self._derive_content_intents(case)
        return self._finish(case, "approve_status_policy", command, fp, ApprovalResult, approval_id=approval.id, scope=approval.scope, artifact_binding=case.policy.binding)  # type: ignore[return-value]

    def _derive_content_intents(self, case: CaseState) -> None:
        if case.policy is None or case.policy.current.state != PolicyState.APPROVED.value or case.package is None: return
        previous_intents = case.metadata.get("intents", {})
        intents: dict[UUID, OperationIntent] = {}
        for target, root in case.plans.items():
            if root.current.state not in {PlanState.APPROVED.value, PlanState.APPLYING.value, PlanState.APPLIED.value}: continue
            current_items = {item.item_id: item for item in root.current.payload.items}
            tombstones = case.metadata.get("tombstones", {}).get(
                (root.artifact_id, root.current.version), {}
            )
            reconciliation = case.metadata.get("reconciliation", {}).get((root.artifact_id, root.current.version), {})
            candidate_ids = set(current_items) | set(tombstones)
            for item_id in sorted(candidate_ids, key=lambda value: value.bytes):
                tombstone = tombstones.get(item_id)
                item = current_items.get(item_id) or (tombstone["item"] if tombstone else None)
                if item is None:
                    continue
                binding = next((value for value in case.bindings.values() if value.plan_id == root.artifact_id and value.item_id == item_id), None)
                action = Action.CREATE if root.current.version == 1 and binding is None else reconciliation.get(item_id)
                if action is None:
                    previous = next(
                        (value for value in previous_intents.values() if UUID(str(value.item_ref["item_id"])) == item_id and value.system == System(target.value) and value.action != Action.TRANSITION_STATUS),
                        None,
                    )
                    if previous is not None:
                        intents[previous.intent_id] = previous
                    continue
                if action in {Action.UPDATE, Action.RETIRE} and binding is None:
                    continue
                system = System(target.value); generation = self._generation_key(case.id, target, item.item_id)
                labels = ["specops", "specops-retired"] if action == Action.RETIRE else ["specops"]
                dependency_keys: dict[UUID, str] = {}
                unresolved = False
                for dependency_id in item.dependency_item_ids:
                    lookup_id = dependency_id
                    if system == System.GITHUB:
                        dependency_item = current_items.get(dependency_id)
                        lookup_id = dependency_item.primary_jira_item_id if dependency_item else None
                    metadata = case.metadata.get("external_bindings", {}).get(lookup_id) if lookup_id else None
                    key = metadata.get("key") if isinstance(metadata, dict) else None
                    if key is None:
                        unresolved = True
                        break
                    dependency_keys[dependency_id] = key
                if unresolved:
                    continue
                parent_key = None
                if system == System.JIRA and item.parent_item_id is not None:
                    metadata = case.metadata.get("external_bindings", {}).get(item.parent_item_id)
                    parent_key = metadata.get("key") if isinstance(metadata, dict) else None
                    if parent_key is None:
                        continue
                existing = binding.external_identity if binding else None
                latest = next((snapshot for snapshot in reversed(binding.snapshots) if snapshot.remote_revision is not None), None) if binding else None
                expected_revision = latest.remote_revision if latest else None
                if (
                    action == Action.RETIRE
                    and latest is not None
                    and self._lifecycle(
                        system,
                        self._mapping_for(case, system, latest.native_status),
                        latest.owned_content,
                    )
                    == Lifecycle.RETIRED
                ):
                    continue
                if system == System.JIRA:
                    jira_key = existing.key if isinstance(existing, JiraIdentity) else None
                    request = {"project_key": root.current.payload.project_key, "issue_type": item.kind.value, "summary": item.title, "rendered_description": render_structured_body(item.body, dependency_keys, jira_key=jira_key), "parent_key": parent_key, "dependency_keys": [dependency_keys[value] for value in sorted(dependency_keys, key=lambda value: value.bytes)], "labels": labels, "generation_key": generation, "package_binding": case.package.binding.model_dump(mode="json"), "plan_binding": root.binding.model_dump(mode="json")}
                    jira_binding = None
                else:
                    jira_root = case.plans[PlanTarget.JIRA]
                    request = {"repository": item.repository, "title": item.title, "rendered_body": render_structured_body(item.body, dependency_keys, jira_key=item.body.jira_key), "labels": labels, "generation_key": generation, "jira_key": item.body.jira_key, "package_binding": case.package.binding.model_dump(mode="json"), "jira_plan_binding": jira_root.binding.model_dump(mode="json"), "plan_binding": root.binding.model_dump(mode="json")}
                    jira_binding = jira_root.binding
                owned_hash = sha256(request)
                if action == Action.RETIRE:
                    item_ref = {"kind": "TOMBSTONE", "item_id": item.item_id, "plan_id": root.artifact_id, "plan_version": root.current.version, "plan_hash": root.current.semantic_hash, "prior_plan_id": root.artifact_id, "prior_plan_version": tombstone["prior_plan_version"], "prior_plan_hash": tombstone["prior_plan_hash"], "stable_binding_id": binding.id}
                else:
                    item_ref = {"kind": "CURRENT", "item_id": item.item_id, "plan_id": root.artifact_id, "plan_version": root.current.version, "plan_hash": root.current.semantic_hash}
                fingerprint = sha256({"schema": "operation-request-v1", "case_id": case.id, "system": system, "action": action, "generation_key": generation, "item_ref": item_ref, "package_binding": case.package.binding, "plan_binding": root.binding, "status_policy_binding": case.policy.binding, "request_owned_content_hash": owned_hash, "external_identity": existing, "expected_remote_revision": expected_revision, "target_normalized_status": None, "contributing_rule_ids": []})
                intent_id = uuid5(INTENT_NAMESPACE, fingerprint)
                intents[intent_id] = OperationIntent(intent_id=intent_id, system=system, item_ref=item_ref, action=action, request=request, package_binding=case.package.binding, plan_binding=root.binding, jira_plan_binding=jira_binding, status_policy_binding=case.policy.binding, request_owned_content_hash=owned_hash, fingerprint=fingerprint, existing=existing, expected=expected_revision)
        case.metadata["intents"] = intents
        self._derive_status_intents(case)

    def _derive_status_intents(self, case: CaseState) -> None:
        """Derive deterministic status-sync findings and TRANSITION_STATUS intents."""
        if case.policy is None or case.policy.current.state != PolicyState.APPROVED.value or case.package is None:
            return
        intents = {
            intent_id: intent
            for intent_id, intent in case.metadata.get("intents", {}).items()
            if intent.action != Action.TRANSITION_STATUS
        }
        policy_binding = case.policy.binding
        for target, root in case.plans.items():
            if root.current.state not in {PlanState.APPROVED.value, PlanState.APPLYING.value, PlanState.APPLIED.value}:
                continue
            system = System(target.value)
            items = {item.item_id: item for item in root.current.payload.items}
            targets_by_item: dict[UUID, list[tuple[JiraStatus | GitHubStatus, UUID]]] = {}
            for rule in case.policy.current.payload.rules:
                if rule.target_system != system:
                    continue
                if rule.selector == Selector.ALL_PROJECTED_ITEMS:
                    selected = set(items)
                elif rule.selector == Selector.IMPLEMENTATION_ITEMS:
                    selected = {item.item_id for item in items.values() if item.implementation_required}
                else:
                    selected = set(rule.item_ids)
                for item_id in selected & set(items):
                    binding = next((value for value in case.bindings.values() if value.plan_id == root.artifact_id and value.item_id == item_id), None)
                    latest = binding.snapshots[-1] if binding and binding.snapshots else None
                    if latest is not None and latest.observation_kind != ObservationKind.FOUND:
                        latest = None
                    if binding is None or latest is None:
                        continue
                    matches = True
                    for condition in rule.all_of:
                        if condition.kind == ConditionKind.PACKAGE_CURRENT_APPROVED:
                            matches = matches and case.package.current.state == PackageState.APPROVED.value
                        elif condition.kind == ConditionKind.NO_BLOCKING_FINDINGS:
                            matches = matches and not any(value.get("active") for value in case.metadata.get("findings", {}).values() if value.get("category") not in {FindingCategory.STATUS_SYNC_REQUIRED})
                        elif condition.kind == ConditionKind.JIRA_STATE_IN:
                            if system == System.JIRA:
                                normalized = self._mapping_for(case, system, latest.native_status)
                            else:
                                primary = items[item_id].primary_jira_item_id
                                jira_binding = next((value for value in case.bindings.values() if value.system == System.JIRA and value.item_id == primary), None)
                                jira_latest = next((snapshot for snapshot in reversed(jira_binding.snapshots) if snapshot.observation_kind == ObservationKind.FOUND), None) if jira_binding else None
                                normalized = self._mapping_for(case, System.JIRA, jira_latest.native_status) if jira_latest else None
                            matches = matches and normalized in condition.jira_states
                        elif condition.kind == ConditionKind.GITHUB_STATE_IN:
                            if system == System.GITHUB:
                                normalized = self._mapping_for(case, system, latest.native_status)
                            else:
                                github_item = next((value for value in case.plans.get(PlanTarget.GITHUB, ArtifactRoot(ArtifactKind.PROJECTION_PLAN, UUID(int=0))).current.payload.items if value.primary_jira_item_id == item_id), None) if PlanTarget.GITHUB in case.plans else None
                                github_binding = next((value for value in case.bindings.values() if github_item and value.system == System.GITHUB and value.item_id == github_item.item_id), None)
                                github_latest = next((snapshot for snapshot in reversed(github_binding.snapshots) if snapshot.observation_kind == ObservationKind.FOUND), None) if github_binding else None
                                normalized = self._mapping_for(case, System.GITHUB, github_latest.native_status) if github_latest else None
                            matches = matches and normalized in condition.github_states
                    if matches:
                        targets_by_item.setdefault(item_id, []).append((rule.target_normalized_status, rule.rule_id))

            for item_id, targets in targets_by_item.items():
                binding = next(value for value in case.bindings.values() if value.plan_id == root.artifact_id and value.item_id == item_id)
                latest = next(snapshot for snapshot in reversed(binding.snapshots) if snapshot.observation_kind == ObservationKind.FOUND)
                normalized = self._mapping_for(case, system, latest.native_status)
                if normalized is None:
                    continue
                distinct = {target for target, _ in targets}
                rule_ids = sorted({rule_id for _, rule_id in targets}, key=lambda value: value.bytes)
                if len(distinct) > 1:
                    components = [policy_binding.model_dump(mode="json"), StatusConflictSubtype.MULTIPLE_TARGETS.value, system.value, item_id, rule_ids]
                    ordered_statuses = sorted(distinct, key=lambda value: list(JiraStatus if system == System.JIRA else GitHubStatus).index(value))
                    self._finding(case, FindingCategory.STATUS_CONFLICT, components, EntityValue(entity_kind=EntityKind.PROJECTION_ITEM, id=item_id), MissingValue(), StatusSetValue(system=system, values=ordered_statuses))
                    continue
                target_status = next(iter(distinct))
                components = [policy_binding.model_dump(mode="json"), system.value, item_id, binding.id, target_status.value]
                if normalized == target_status:
                    self._resolve_finding(case, FindingCategory.STATUS_SYNC_REQUIRED, components)
                    continue
                self._finding(case, FindingCategory.STATUS_SYNC_REQUIRED, components, EntityValue(entity_kind=EntityKind.PROJECTION_ITEM, id=item_id), StatusValue(system=system, value=target_status), StatusValue(system=system, value=normalized))
                if self._blocker_findings(case):
                    continue
                plan_binding = root.binding
                request = latest.owned_content
                request_hash = sha256(request)
                item_ref = {"kind": "CURRENT", "item_id": item_id, "plan_id": root.artifact_id, "plan_version": root.current.version, "plan_hash": root.current.semantic_hash}
                jira_binding = case.plans[PlanTarget.JIRA].binding if system == System.GITHUB and PlanTarget.JIRA in case.plans else None
                fingerprint = sha256({"schema": "operation-request-v1", "case_id": case.id, "system": system, "action": Action.TRANSITION_STATUS, "generation_key": binding.generation_key, "item_ref": item_ref, "package_binding": case.package.binding, "plan_binding": plan_binding, "status_policy_binding": policy_binding, "request_owned_content_hash": request_hash, "external_identity": binding.external_identity, "expected_remote_revision": latest.remote_revision, "target_normalized_status": target_status, "contributing_rule_ids": rule_ids})
                intent_id = uuid5(INTENT_NAMESPACE, fingerprint)
                intents[intent_id] = OperationIntent(intent_id=intent_id, system=system, item_ref=item_ref, action=Action.TRANSITION_STATUS, request=request, package_binding=case.package.binding, plan_binding=plan_binding, jira_plan_binding=jira_binding, status_policy_binding=policy_binding, request_owned_content_hash=request_hash, fingerprint=fingerprint, existing=binding.external_identity, expected=latest.remote_revision, target_normalized_status=target_status, contributing_rule_ids=rule_ids)
        case.metadata["intents"] = intents

    def _mapping_for(self, case: CaseState, system: System, native_status: str) -> JiraStatus | GitHubStatus | None:
        if case.policy is None or case.policy.current.state != PolicyState.APPROVED.value: return None
        return next((item.normalized_status for item in case.policy.current.payload.mappings if item.system == system and item.native_status == native_status), None)

    @staticmethod
    def _lifecycle(system: System, normalized: JiraStatus | GitHubStatus | None, owned: dict[str, Any] | None) -> Lifecycle:
        labels = (owned or {}).get("labels", [])
        if labels == ["specops", "specops-retired"] and ((system == System.JIRA and normalized == JiraStatus.CANCELLED) or (system == System.GITHUB and normalized == GitHubStatus.CLOSED)): return Lifecycle.RETIRED
        if labels == ["specops"] and normalized is not None and normalized.value != "UNKNOWN": return Lifecycle.ACTIVE
        return Lifecycle.UNKNOWN

    def _finding(self, case: CaseState, category: FindingCategory, components: list[Any], affected: FindingValue, expected: FindingValue, observed: FindingValue) -> UUID:
        name = {"case_id": case.id, "category": category.value, "components": components, "schema": "drift-finding-v1"}
        finding_id = uuid5(DRIFT_NAMESPACE, canonical_json(name).decode("utf-8"))
        existing = case.metadata.setdefault("findings", {}).get(finding_id)
        created_at = existing["created_at"] if existing else self._now()
        case.metadata["findings"][finding_id] = {"category": category, "expected": expected, "observed": observed, "active": True, "created_at": created_at, "resolved_at": None, "affected": affected}
        return finding_id

    def _resolve_finding(self, case: CaseState, category: FindingCategory, components: list[Any]) -> None:
        finding_id = uuid5(DRIFT_NAMESPACE, canonical_json({"case_id": case.id, "category": category.value, "components": components, "schema": "drift-finding-v1"}).decode("utf-8"))
        item = case.metadata.get("findings", {}).get(finding_id)
        if item and item["active"]: item["active"] = False; item["resolved_at"] = self._now()

    def _refresh_core_findings(self, case: CaseState) -> None:
        for ambiguity in case.ambiguities.values():
            components = [ambiguity.id]
            if ambiguity.status == FindingStatus.OPEN and ambiguity.severity == Severity.BLOCKING.value:
                self._finding(case, FindingCategory.AMBIGUITY_UNRESOLVED, components, EntityValue(entity_kind=EntityKind.AMBIGUITY_FINDING, id=ambiguity.id), FindingStateValue(value=FindingStatus.RESOLVED), FindingStateValue(value=FindingStatus.OPEN))
            else:
                self._resolve_finding(case, FindingCategory.AMBIGUITY_UNRESOLVED, components)
        for approval in case.approvals:
            components = [approval.id]
            direct = any(
                item.artifact_id == approval.artifact_id
                and item.artifact_version == approval.artifact_version
                and item.artifact_hash == approval.artifact_hash
                and item.scope == approval.scope
                and item.actor_id == approval.delegator_id
                for item in case.approvals
            )
            if approval.later_review_required and not direct:
                binding = ArtifactBinding(artifact_kind=ArtifactKind(approval.artifact_kind), artifact_id=approval.artifact_id, version=approval.artifact_version, semantic_hash=approval.artifact_hash)
                delegated_value = ApprovalValue(binding=binding, scope=approval.scope, actor_id=approval.actor_id, delegation_id=approval.delegation_id)
                expected_value = ApprovalValue(binding=binding, scope=approval.scope, actor_id=approval.delegator_id, delegation_id=None)
                self._finding(case, FindingCategory.LATER_REVIEW_REQUIRED, components, delegated_value, expected_value, delegated_value)
            else:
                self._resolve_finding(case, FindingCategory.LATER_REVIEW_REQUIRED, components)
        for ambiguity in case.ambiguities.values():
            for resolution in ambiguity.resolutions:
                components = [resolution.resolution_id]
                if resolution.later_review_required and resolution.reviewed_at is None:
                    observed = ResolutionReviewValue(finding_id=ambiguity.id, scope=resolution.scope, delegation_id=resolution.delegation_id, reviewed=False)
                    expected = observed.model_copy(update={"reviewed": True})
                    self._finding(case, FindingCategory.LATER_REVIEW_REQUIRED, components, observed, expected, observed)
                else:
                    self._resolve_finding(case, FindingCategory.LATER_REVIEW_REQUIRED, components)

    def submit_remote_snapshot(self, command: SubmitRemoteSnapshotCommand) -> SnapshotResult:
        case, replay, fp = self._begin("submit_remote_snapshot", command)
        if replay: return replay  # type: ignore[return-value]
        self._authorize(case, command.acting_actor_id, "submit_remote_snapshot")
        if case.policy is None or case.policy.current.state != PolicyState.APPROVED.value: raise DomainError(ErrorCode.STATUS_POLICY_NOT_APPROVED)
        observation = command.observation
        if observation.status_policy_binding != case.policy.binding: raise DomainError(ErrorCode.STALE_ARTIFACT_BINDING)
        if case.package is None or observation.package_binding != case.package.binding:
            raise DomainError(ErrorCode.STALE_ARTIFACT_BINDING)
        plan = case.plans.get(PlanTarget(observation.system.value))
        if plan is None or observation.plan_binding != plan.binding:
            raise DomainError(ErrorCode.STALE_ARTIFACT_BINDING)
        if observation.system == System.GITHUB:
            jira = case.plans.get(PlanTarget.JIRA)
            if jira is None or observation.jira_plan_binding != jira.binding:
                raise DomainError(ErrorCode.STALE_ARTIFACT_BINDING)
        elif observation.jira_plan_binding is not None:
            raise DomainError(ErrorCode.STALE_ARTIFACT_BINDING)
        identity_key = self._identity_key(observation.system, observation.external_identity)
        binding = next((item for item in case.bindings.values() if item.system == observation.system and self._identity_key(item.system, item.external_identity) == identity_key and item.generation_key == observation.generation_key), None)
        if binding is None: raise DomainError(ErrorCode.RECORD_NOT_FOUND)
        latest = next((item for item in reversed(binding.snapshots) if item.remote_revision is not None), None); expected = latest.remote_revision if latest else None
        if observation.expected_previous_remote_revision != expected: raise DomainError(ErrorCode.REMOTE_VERSION_MISMATCH)
        if observation.remote_revision is not None and any(item.remote_revision == observation.remote_revision for item in binding.snapshots):
            raise DomainError(ErrorCode.INVALID_TRANSITION)
        binding.snapshots.append(observation); binding.current_observation_sequence += 1
        active: list[UUID] = []
        if observation.observation_kind == ObservationKind.NOT_FOUND:
            active.append(self._finding(case, FindingCategory.MISSING_REMOTE_ITEM, [binding.id], EntityValue(entity_kind=EntityKind.EXTERNAL_BINDING, id=binding.id), ExternalValue(value=binding.external_identity), MissingValue()))
        else:
            self._resolve_finding(case, FindingCategory.MISSING_REMOTE_ITEM, [binding.id])
            normalized = self._mapping_for(case, observation.system, observation.native_status)
            unmapped_components = [case.policy.binding.model_dump(mode="json"), binding.id, observation.native_status]
            if normalized is None: active.append(self._finding(case, FindingCategory.UNMAPPED_REMOTE_STATUS, unmapped_components, EntityValue(entity_kind=EntityKind.EXTERNAL_BINDING, id=binding.id), NativeMappingKeyValue(system=observation.system, native_status=observation.native_status), MissingValue()))
            else: self._resolve_finding(case, FindingCategory.UNMAPPED_REMOTE_STATUS, unmapped_components)
            lifecycle = self._lifecycle(observation.system, normalized, observation.owned_content)
            lifecycle_components = [case.policy.binding.model_dump(mode="json"), StatusConflictSubtype.INVALID_LIFECYCLE.value, observation.system.value, binding.item_id, []]
            expected_lifecycle = Lifecycle.RETIRED if all(item.item_id != binding.item_id for item in plan.current.payload.items) else Lifecycle.ACTIVE
            if lifecycle == Lifecycle.UNKNOWN: active.append(self._finding(case, FindingCategory.STATUS_CONFLICT, lifecycle_components, EntityValue(entity_kind=EntityKind.EXTERNAL_BINDING, id=binding.id), LifecycleValue(value=expected_lifecycle), LifecycleValue(value=lifecycle)))
            else: self._resolve_finding(case, FindingCategory.STATUS_CONFLICT, lifecycle_components)
            desired_candidates = [
                intent
                for intent in [
                    *case.metadata.get("intents", {}).values(),
                    *(operation.intent for operation in case.operations.values()),
                ]
                if UUID(str(intent.item_ref["item_id"])) == binding.item_id
                and intent.action != Action.TRANSITION_STATUS
                and intent.plan_binding == observation.plan_binding
            ]
            desired = desired_candidates[-1] if desired_candidates else None
            if desired is not None and sha256(observation.owned_content) != desired.request_owned_content_hash:
                active.append(self._finding(case, FindingCategory.CONTENT_DRIFT, [observation.plan_binding.model_dump(mode="json"), binding.item_id, binding.id], EntityValue(entity_kind=EntityKind.EXTERNAL_BINDING, id=binding.id), ContentValue(value=desired.request_owned_content_hash), ContentValue(value=sha256(observation.owned_content))))
            elif desired is not None:
                self._resolve_finding(case, FindingCategory.CONTENT_DRIFT, [observation.plan_binding.model_dump(mode="json"), binding.item_id, binding.id])
        self._derive_content_intents(case)
        active = sorted(self._active_findings(case), key=lambda item: item.bytes)
        case.revision += 1; receipt = Receipt(case_id=case.id, revision=case.revision, command_id=command.command_id, occurred_at=self._now())
        result = SnapshotResult(observation_kind="FOUND_ACCEPTED" if observation.observation_kind == ObservationKind.FOUND else "NOT_FOUND_ACCEPTED", observation=observation, binding_id=binding.id, observation_sequence=binding.current_observation_sequence, active_finding_ids=active, receipt=receipt)
        case.command_results[command.command_id] = (fp, result)
        self._persist_result(case, "submit_remote_snapshot", command, fp, result)
        return result

    def _authorize_read(self, case: CaseState, actor: Actor) -> None:
        if actor != "SYSTEM" and actor not in case.participants: raise DomainError(ErrorCode.AUTHORITY_REQUIRED)

    def _active_findings(self, case: CaseState) -> dict[UUID, dict[str, Any]]:
        return {key: value for key, value in case.metadata.get("findings", {}).items() if value.get("active")}

    def _blocker_findings(self, case: CaseState) -> dict[UUID, dict[str, Any]]:
        return {
            key: value
            for key, value in self._active_findings(case).items()
            if value.get("category") != FindingCategory.STATUS_SYNC_REQUIRED
        }

    def _pending_later_review(self, case: CaseState) -> list[UUID]:
        pending: list[UUID] = []
        for approval in case.approvals:
            if approval.later_review_required:
                direct = any(item.artifact_id == approval.artifact_id and item.artifact_version == approval.artifact_version and item.artifact_hash == approval.artifact_hash and item.scope == approval.scope and item.actor_id == approval.delegator_id for item in case.approvals)
                if not direct: pending.append(approval.id)
        for finding in case.ambiguities.values():
            pending.extend(record.resolution_id for record in finding.resolutions if record.later_review_required and record.reviewed_at is None)
        return sorted(pending, key=lambda item: item.bytes)

    def _setup_stage(self, case: CaseState) -> SetupStage:
        if case.package is None: return SetupStage.INTAKE
        if case.package.current.state != PackageState.APPROVED.value: return SetupStage.PACKAGE_REVIEW
        jira = case.plans.get(PlanTarget.JIRA)
        if jira is None or jira.current.state not in {PlanState.APPROVED.value, PlanState.APPLYING.value, PlanState.APPLIED.value}: return SetupStage.JIRA_REVIEW
        if case.policy is None or case.policy.current.state != PolicyState.APPROVED.value: return SetupStage.STATUS_REVIEW
        jira_intents = [item for item in case.metadata.get("intents", {}).values() if item.system == System.JIRA and item.action != Action.TRANSITION_STATUS]
        if any(not any(op.intent.intent_id == item.intent_id and op.status == OperationStatus.SUCCEEDED for op in case.operations.values()) for item in jira_intents): return SetupStage.JIRA_APPLY
        github = case.plans.get(PlanTarget.GITHUB)
        if github is None or github.current.state not in {PlanState.APPROVED.value, PlanState.APPLYING.value, PlanState.APPLIED.value}: return SetupStage.GITHUB_REVIEW
        github_intents = [item for item in case.metadata.get("intents", {}).values() if item.system == System.GITHUB and item.action != Action.TRANSITION_STATUS]
        if any(not any(op.intent.intent_id == item.intent_id and op.status == OperationStatus.SUCCEEDED for op in case.operations.values()) for item in github_intents): return SetupStage.GITHUB_APPLY
        return SetupStage.LINKED

    def _trace_edges(self, case: CaseState) -> list[TraceEdge]:
        if case.package is None: return []
        relations: list[tuple[TraceEdgeType, TraceEndpoint, TraceEndpoint]] = []
        payload: SpecPackagePayload = case.package.current.payload
        package_endpoint = TraceEndpoint(kind=TraceNodeKind.SPEC_PACKAGE, id=case.package.artifact_id, version=case.package.current.version)
        unit_kinds = {
            **{item.unit_id: TraceNodeKind.REQUIREMENT for item in payload.requirements},
            **{item.unit_id: TraceNodeKind.TECHNICAL_DECISION for item in payload.technical_decisions},
        }
        for item, node_kind in [
            *((item, TraceNodeKind.REQUIREMENT) for item in payload.requirements),
            *((item, TraceNodeKind.TECHNICAL_DECISION) for item in payload.technical_decisions),
        ]:
            unit = TraceEndpoint(kind=node_kind, id=item.unit_id, version=case.package.current.version)
            relations.append((TraceEdgeType.PACKAGE_TO_UNIT, package_endpoint, unit))
            for ref in item.source_refs:
                relations.append((TraceEdgeType.SOURCE_TO_UNIT, TraceEndpoint(kind=TraceNodeKind.SOURCE_ARTIFACT, id=ref.artifact_id, version=ref.version), unit))
        for check in payload.acceptance_checks:
            endpoint = TraceEndpoint(kind=TraceNodeKind.ACCEPTANCE_CHECK, id=check.check_id, version=case.package.current.version)
            relations.append((TraceEdgeType.PACKAGE_TO_UNIT, package_endpoint, endpoint))
            for ref in check.source_refs:
                relations.append((TraceEdgeType.SOURCE_TO_UNIT, TraceEndpoint(kind=TraceNodeKind.SOURCE_ARTIFACT, id=ref.artifact_id, version=ref.version), endpoint))
        jira = case.plans.get(PlanTarget.JIRA)
        if jira:
            plan_endpoint = TraceEndpoint(kind=TraceNodeKind.PROJECTION_PLAN, id=jira.artifact_id, version=jira.current.version)
            for item in jira.current.payload.items:
                target = TraceEndpoint(kind=TraceNodeKind.PROJECTION_ITEM, id=item.item_id, version=jira.current.version)
                for source in item.source_unit_ids:
                    relations.append((TraceEdgeType.UNIT_TO_JIRA_ITEM, TraceEndpoint(kind=unit_kinds[source], id=source, version=case.package.current.version), target))
                if item.parent_item_id is not None:
                    relations.append((TraceEdgeType.JIRA_PARENT, TraceEndpoint(kind=TraceNodeKind.PROJECTION_ITEM, id=item.parent_item_id, version=jira.current.version), target))
                for dependency_id in item.dependency_item_ids:
                    relations.append((TraceEdgeType.JIRA_DEPENDENCY, TraceEndpoint(kind=TraceNodeKind.PROJECTION_ITEM, id=dependency_id, version=jira.current.version), target))
                relations.append((TraceEdgeType.ITEM_TO_PACKAGE, target, package_endpoint))
                relations.append((TraceEdgeType.ITEM_TO_PLAN, target, plan_endpoint))
                binding = next((value for value in case.bindings.values() if value.system == System.JIRA and value.plan_id == jira.artifact_id and value.item_id == item.item_id), None)
                if binding is not None:
                    relations.append((TraceEdgeType.JIRA_ITEM_TO_BINDING, target, TraceEndpoint(kind=TraceNodeKind.EXTERNAL_BINDING, id=binding.id)))
        github = case.plans.get(PlanTarget.GITHUB)
        if github and jira:
            plan_endpoint = TraceEndpoint(kind=TraceNodeKind.PROJECTION_PLAN, id=github.artifact_id, version=github.current.version)
            for item in github.current.payload.items:
                target = TraceEndpoint(kind=TraceNodeKind.PROJECTION_ITEM, id=item.item_id, version=github.current.version)
                jira_binding = next((value for value in case.bindings.values() if value.system == System.JIRA and value.plan_id == jira.artifact_id and value.item_id == item.primary_jira_item_id), None)
                if jira_binding is not None:
                    relations.append((TraceEdgeType.JIRA_BINDING_TO_GITHUB_ITEM, TraceEndpoint(kind=TraceNodeKind.EXTERNAL_BINDING, id=jira_binding.id), target))
                github_binding = next((value for value in case.bindings.values() if value.system == System.GITHUB and value.plan_id == github.artifact_id and value.item_id == item.item_id), None)
                if github_binding is not None:
                    relations.append((TraceEdgeType.GITHUB_ITEM_TO_BINDING, target, TraceEndpoint(kind=TraceNodeKind.EXTERNAL_BINDING, id=github_binding.id)))
                relations.append((TraceEdgeType.ITEM_TO_PACKAGE, target, package_endpoint))
                relations.append((TraceEdgeType.ITEM_TO_PLAN, target, plan_endpoint))
        edges: dict[UUID, TraceEdge] = {}
        for kind, source, target in relations:
            name = {"case_id": case.id, "edge_type": kind, "from": source.model_dump(mode="python", exclude_none=False), "schema": "trace-edge-v1", "to": target.model_dump(mode="python", exclude_none=False)}
            edge = TraceEdge(id=uuid5(TRACE_NAMESPACE, canonical_json(name).decode("utf-8")), edge_type=kind, from_endpoint=source, to_endpoint=target)
            edges[edge.id] = edge
        return sorted(edges.values(), key=lambda item: item.id.bytes)

    def _trace_complete(self, case: CaseState, edges: list[TraceEdge]) -> bool:
        if case.package is None or PlanTarget.JIRA not in case.plans:
            return False
        jira = case.plans[PlanTarget.JIRA]
        edge_types = {(edge.edge_type, edge.from_endpoint.id, edge.to_endpoint.id) for edge in edges}
        delivery_units = {
            item.unit_id
            for item in [*case.package.current.payload.requirements, *case.package.current.payload.technical_decisions]
            if item.delivery_required
        }
        projected_units = {source for edge_type, source, _ in edge_types if edge_type == TraceEdgeType.UNIT_TO_JIRA_ITEM}
        if not delivery_units.issubset(projected_units):
            return False
        for item in jira.current.payload.items:
            binding = next((value for value in case.bindings.values() if value.system == System.JIRA and value.plan_id == jira.artifact_id and value.item_id == item.item_id), None)
            if binding is None or (TraceEdgeType.JIRA_ITEM_TO_BINDING, item.item_id, binding.id) not in edge_types:
                return False
        implementation_items = {item.item_id for item in jira.current.payload.items if item.implementation_required}
        if implementation_items:
            github = case.plans.get(PlanTarget.GITHUB)
            if github is None:
                return False
            if {item.primary_jira_item_id for item in github.current.payload.items} != implementation_items:
                return False
            for item in github.current.payload.items:
                jira_binding = next((value for value in case.bindings.values() if value.system == System.JIRA and value.plan_id == jira.artifact_id and value.item_id == item.primary_jira_item_id), None)
                github_binding = next((value for value in case.bindings.values() if value.system == System.GITHUB and value.plan_id == github.artifact_id and value.item_id == item.item_id), None)
                if jira_binding is None or github_binding is None:
                    return False
                if (TraceEdgeType.JIRA_BINDING_TO_GITHUB_ITEM, jira_binding.id, item.item_id) not in edge_types or (TraceEdgeType.GITHUB_ITEM_TO_BINDING, item.item_id, github_binding.id) not in edge_types:
                    return False
        return True

    def get_workflow_view(self, query: QueryOne) -> WorkflowView:
        case = self._case(query.case_id); self._authorize_read(case, query.acting_actor_id)
        stage = self._setup_stage(case); blockers = sorted(self._blocker_findings(case), key=lambda item: item.bytes); pending = self._pending_later_review(case)
        trace_complete = self._trace_complete(case, self._trace_edges(case))
        return WorkflowView(case_id=case.id, revision=case.revision, current_package=case.package.binding if case.package else None, current_jira_plan=case.plans[PlanTarget.JIRA].binding if PlanTarget.JIRA in case.plans else None, current_github_plan=case.plans[PlanTarget.GITHUB].binding if PlanTarget.GITHUB in case.plans else None, current_status_policy=case.policy.binding if case.policy else None, setup_stage=stage, foundation_ready=stage == SetupStage.LINKED and trace_complete and not blockers and not pending, workflow_health=WorkflowHealth.BLOCKED if blockers else WorkflowHealth.HEALTHY, blocker_ids=blockers, pending_later_review_ids=pending)

    def get_traceability_map(self, query: QueryOne) -> TraceabilityMap:
        case = self._case(query.case_id); self._authorize_read(case, query.acting_actor_id); edges = self._trace_edges(case)
        return TraceabilityMap(case_id=case.id, complete=self._trace_complete(case, edges) and not any(item["category"] == FindingCategory.TRACEABILITY_GAP for item in self._active_findings(case).values()), edges=edges)

    def get_delivery_view(self, query: QueryOne) -> DeliveryView:
        case = self._case(query.case_id); self._authorize_read(case, query.acting_actor_id); items: list[DeliveryItem] = []
        for target, root in case.plans.items():
            visible_items = list(root.current.payload.items)
            tombstones = case.metadata.get("tombstones", {}).get(
                (root.artifact_id, root.current.version), {}
            )
            if tombstones:
                current_ids = {item.item_id for item in visible_items}
                for item_id, tombstone in tombstones.items():
                    prior_item = tombstone["item"]
                    if item_id in current_ids:
                        continue
                    prior_binding = next(
                        (
                            value
                            for value in case.bindings.values()
                            if value.plan_id == root.artifact_id
                            and value.item_id == prior_item.item_id
                        ),
                        None,
                    )
                    prior_latest = (
                        next(
                            (
                                snapshot
                                for snapshot in reversed(prior_binding.snapshots)
                                if snapshot.observation_kind == ObservationKind.FOUND
                            ),
                            None,
                        )
                        if prior_binding
                        else None
                    )
                    prior_normalized = (
                        self._mapping_for(case, System(target.value), prior_latest.native_status)
                        if prior_latest
                        else None
                    )
                    if self._lifecycle(
                        System(target.value),
                        prior_normalized,
                        prior_latest.owned_content if prior_latest else None,
                    ) != Lifecycle.RETIRED:
                        visible_items.append(prior_item)
            for plan_item in visible_items:
                binding = next((value for value in case.bindings.values() if value.plan_id == root.artifact_id and value.item_id == plan_item.item_id), None)
                latest = binding.snapshots[-1] if binding and binding.snapshots else None
                normalized = self._mapping_for(case, System(target.value), latest.native_status) if latest and latest.native_status else None
                if normalized is None: normalized = JiraStatus.UNKNOWN if target == PlanTarget.JIRA else GitHubStatus.UNKNOWN
                lifecycle = self._lifecycle(System(target.value), normalized, latest.owned_content if latest else None)
                pending = [intent.intent_id for intent in case.metadata.get("intents", {}).values() if UUID(str(intent.item_ref["item_id"])) == plan_item.item_id and not any(op.intent.intent_id == intent.intent_id and op.status == OperationStatus.SUCCEEDED for op in case.operations.values())]
                items.append(DeliveryItem(system=System(target.value), plan_id=root.artifact_id, plan_version=root.current.version, item_id=plan_item.item_id, external_identity=binding.external_identity if binding else None, normalized_status=normalized, lifecycle_state=lifecycle, remote_revision=latest.remote_revision if latest else None, pending_intent_ids=sorted(pending, key=lambda item: item.bytes)))
        items.sort(key=lambda item: (0 if item.system == System.JIRA else 1, item.item_id.bytes))
        if any(item.normalized_status.value == "UNKNOWN" or item.lifecycle_state == Lifecycle.UNKNOWN for item in items): state = DeliveryState.UNKNOWN
        elif self._blocker_findings(case) or any(item.system == System.JIRA and item.normalized_status in {JiraStatus.BLOCKED, JiraStatus.CANCELLED} for item in items): state = DeliveryState.BLOCKED
        else:
            jira_impl = {item.item_id for item in case.plans.get(PlanTarget.JIRA).current.payload.items if item.implementation_required} if PlanTarget.JIRA in case.plans else set()
            github_by_jira = {
                item.primary_jira_item_id: item.item_id
                for item in case.plans.get(PlanTarget.GITHUB).current.payload.items
            } if PlanTarget.GITHUB in case.plans else {}
            delivery_by_id = {(item.system, item.item_id): item for item in items}
            done = bool(jira_impl) and all(
                delivery_by_id.get((System.JIRA, item_id)) is not None
                and delivery_by_id[(System.JIRA, item_id)].normalized_status == JiraStatus.DONE
                and github_by_jira.get(item_id) is not None
                and delivery_by_id.get((System.GITHUB, github_by_jira[item_id])) is not None
                and delivery_by_id[(System.GITHUB, github_by_jira[item_id])].normalized_status == GitHubStatus.CLOSED
                for item_id in jira_impl
            )
            state = DeliveryState.DONE if done and self.get_workflow_view(query).foundation_ready else DeliveryState.IN_PROGRESS if any(item.normalized_status.value in {"IN_PROGRESS", "DONE", "CLOSED"} for item in items) else DeliveryState.NOT_STARTED
        return DeliveryView(delivery_state=state, delivery_complete=state == DeliveryState.DONE, items=items)

    def list_drift_findings(self, query: UUIDListQuery) -> FindingPage:
        case = self._case(query.case_id); self._authorize_read(case, query.acting_actor_id)
        values = []
        for finding_id, item in sorted(case.metadata.get("findings", {}).items(), key=lambda pair: pair[0].bytes):
            if query.after_cursor and finding_id.bytes <= query.after_cursor.bytes: continue
            values.append(DriftFinding(id=finding_id, case_id=case.id, category=item["category"], affected=item.get("affected", {}), expected_value=item["expected"], observed_value=item["observed"], active=item["active"], created_at=item["created_at"], resolved_at=item.get("resolved_at")))
        page = values[:query.limit]; return FindingPage(items=page, next_cursor=page[-1].id if len(values) > query.limit else None)

    def list_pending_operation_intents(self, query: UUIDListQuery) -> IntentPage:
        case = self._case(query.case_id); self._authorize_read(case, query.acting_actor_id)
        if self._blocker_findings(case):
            return IntentPage(items=[], next_cursor=None)
        values = [item for item in case.metadata.get("intents", {}).values() if (query.after_cursor is None or item.intent_id.bytes > query.after_cursor.bytes) and not any(op.intent.intent_id == item.intent_id and op.status in {OperationStatus.PENDING, OperationStatus.UNKNOWN, OperationStatus.SUCCEEDED} for op in case.operations.values())]
        values.sort(key=lambda item: item.intent_id.bytes); page = values[:query.limit]; return IntentPage(items=page, next_cursor=page[-1].intent_id if len(values) > query.limit else None)

    def list_approval_records(self, query: UUIDListQuery) -> ApprovalPage:
        case = self._case(query.case_id); self._authorize_read(case, query.acting_actor_id)
        values = [
            ApprovalRecord(
                approval_id=item.id,
                artifact_binding=ArtifactBinding(
                    artifact_kind=ArtifactKind(item.artifact_kind),
                    artifact_id=item.artifact_id,
                    version=item.artifact_version,
                    semantic_hash=item.artifact_hash,
                ),
                scope=item.scope,
                actor_id=item.actor_id,
                delegation_id=item.delegation_id,
                delegator_id=item.delegator_id,
                later_review_required=item.later_review_required,
                approved_at=item.approved_at,
            )
            for item in case.approvals
            if query.after_cursor is None or item.id.bytes > query.after_cursor.bytes
        ]
        values.sort(key=lambda item: item.approval_id.bytes)
        page = values[:query.limit]
        return ApprovalPage(items=page, next_cursor=page[-1].approval_id if len(values) > query.limit else None)

    def list_external_operation_attempts(self, query: OperationAttemptQuery) -> OperationAttemptPage:
        case = self._case(query.case_id); self._authorize_read(case, query.acting_actor_id)
        if self._store is not None:
            values = self._store.list_operation_attempts(case.id)
        else:
            values = [
                OperationAttemptRecord(
                    operation_id=item.id,
                    attempt=item.attempt,
                    intent_id=item.intent.intent_id,
                    system=item.intent.system,
                    action=item.intent.action,
                    idempotency_key=item.idempotency_key,
                    request=item.intent.request,
                    expected_remote_revision=item.intent.expected,
                    status=item.status,
                    failure_code=item.failure_code,
                    result=item.last_result,
                    started_at=item.attempt_started_at or item.created_at,
                    completed_at=item.updated_at if item.status != OperationStatus.PENDING else None,
                )
                for item in case.operations.values()
            ]
        if query.after_cursor is not None:
            cursor_id, cursor_attempt = query.after_cursor.rsplit(":", 1)
            cursor = (UUID(cursor_id).bytes, int(cursor_attempt))
            values = [item for item in values if (item.operation_id.bytes, item.attempt) > cursor]
        values.sort(key=lambda item: (item.operation_id.bytes, item.attempt))
        page = values[:query.limit]
        next_cursor = f"{page[-1].operation_id}:{page[-1].attempt}" if len(values) > query.limit else None
        return OperationAttemptPage(items=page, next_cursor=next_cursor)

    def list_audit_events(self, query: AuditQuery) -> AuditPage:
        case = self._case(query.case_id); self._authorize_read(case, query.acting_actor_id)
        if self._store is None: return AuditPage(items=[], next_cursor=None)
        values = [item for item in self._store.list_audit(case.id) if query.after_cursor is None or item.case_sequence > query.after_cursor]
        page = values[:query.limit]
        return AuditPage(items=page, next_cursor=page[-1].case_sequence if len(values) > query.limit else None)

def _logged_mutation(name: str, method):
    """Apply the closed mutation logging contract without changing public DTOs."""
    @functools.wraps(method)
    def wrapped(self: WorkflowService, command):
        operation_id = getattr(command, "operation_id", None)
        base = {
            "case_id": command.case_id,
            "command_id": command.command_id,
            "operation_id": operation_id,
        }
        LOGGER.info("command.started", extra={**base, "event_code": "command.started", "outcome": "STARTED", "error_code": None})
        try:
            result = method(self, command)
        except DomainError as error:
            LOGGER.warning("command.rejected", extra={**base, "event_code": "command.rejected", "outcome": "REJECTED", "error_code": error.code.value})
            raise
        except Exception:
            LOGGER.error("transaction.rollback", extra={**base, "event_code": "transaction.rollback", "outcome": "ROLLED_BACK", "error_code": "INTERNAL_ERROR"})
            raise
        operation_event = {
            "start_external_operation": "operation.started",
            "record_operation_result": "operation.completed",
            "reconcile_operation": "operation.reconciled",
        }.get(name)
        if operation_event:
            LOGGER.info(operation_event, extra={**base, "event_code": operation_event, "outcome": "SUCCEEDED", "error_code": None})
        LOGGER.info("command.completed", extra={**base, "event_code": "command.completed", "outcome": "SUCCEEDED", "error_code": None})
        return result
    return wrapped


for _command_name in ALLOWED_COMMANDS:
    setattr(WorkflowService, _command_name, _logged_mutation(_command_name, getattr(WorkflowService, _command_name)))
