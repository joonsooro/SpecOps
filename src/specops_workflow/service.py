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
from .state import AmbiguityState, ApprovalState, CaseState, DelegationState

RESOLUTION_NAMESPACE = UUID("4a5cbf7e-bcff-5a85-b670-c9a38145704d")
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

    def get_case_state(self, case_id: UUID) -> CaseState:
        """Internal test/repository bridge; never returns through a public read method."""
        return self._case(case_id)

