from __future__ import annotations

import asyncio
import re
from datetime import timezone
from enum import StrEnum
from typing import Annotated, Literal, Protocol
from uuid import UUID, uuid5

from pydantic import Field, model_validator

from specops_workflow.enums import AmbiguityCategory, Domain, Severity
from specops_workflow.models import SourceRef

from .contracts import ConversationPhase, TranscriptSnapshot, WorkshopModel


PROPOSAL_NAMESPACE = UUID("99ebaa1a-2bed-5df3-92b5-9c577fdcad77")
ProposalKey = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")]


class ControlIntent(StrEnum):
    NONE = "NONE"; CONFIRM = "CONFIRM"; EDIT = "EDIT"; REJECT = "REJECT"; FINISH = "FINISH"
class ControlTarget(StrEnum): WORKSHOP_PATCH = "WORKSHOP_PATCH"
class ProposalDisposition(StrEnum): OPEN = "OPEN"; RESOLVED = "RESOLVED"


class FindingProposal(WorkshopModel):
    proposal_key: ProposalKey
    existing_finding_id: UUID | None
    item_proposal_key: ProposalKey
    category: AmbiguityCategory
    domain: Domain
    severity: Severity
    evidence_refs: Annotated[list[SourceRef], Field(min_length=1, max_length=100)]
    clarification_question: str = Field(min_length=1, max_length=2000)
    owner_actor_ids: Annotated[list[UUID], Field(min_length=1, max_length=2)]
    disposition: ProposalDisposition


class RequirementProposal(WorkshopModel):
    proposal_key: ProposalKey; existing_unit_id: UUID | None
    statement: str = Field(min_length=1, max_length=2000)
    domain: Domain; delivery_required: bool
    source_refs: Annotated[list[SourceRef], Field(min_length=1, max_length=100)]
class TechnicalDecisionProposal(RequirementProposal):
    domain: Literal[Domain.TECHNICAL, Domain.CROSS_DOMAIN]
class AcceptanceCheckProposal(WorkshopModel):
    proposal_key: ProposalKey; existing_check_id: UUID | None
    statement: str = Field(min_length=1, max_length=2000); domain: Domain
    related_unit_proposal_keys: Annotated[list[ProposalKey], Field(min_length=1, max_length=100)]
    source_refs: Annotated[list[SourceRef], Field(min_length=1, max_length=100)]
class SpecPackageItemProposal(WorkshopModel):
    proposal_key: ProposalKey; existing_item_id: UUID | None; title: str = Field(min_length=1, max_length=240)
    requirement_proposal_keys: Annotated[list[ProposalKey], Field(min_length=1, max_length=500)]
    technical_decision_proposal_keys: Annotated[list[ProposalKey], Field(max_length=500)]
    acceptance_check_proposal_keys: Annotated[list[ProposalKey], Field(min_length=1, max_length=500)]
    dependency_item_proposal_keys: Annotated[list[ProposalKey], Field(max_length=99)]


class CompletePackageProposal(WorkshopModel):
    proposal_key: ProposalKey; existing_package_id: UUID | None
    requirements: Annotated[list[RequirementProposal], Field(min_length=1, max_length=500)]
    technical_decisions: Annotated[list[TechnicalDecisionProposal], Field(min_length=1, max_length=500)]
    acceptance_checks: Annotated[list[AcceptanceCheckProposal], Field(min_length=1, max_length=500)]
    items: Annotated[list[SpecPackageItemProposal], Field(min_length=1, max_length=100)]

    @model_validator(mode="after")
    def closed_keys(self):
        entities = [self.proposal_key]
        entities += [value.proposal_key for group in (self.requirements, self.technical_decisions, self.acceptance_checks, self.items) for value in group]
        if len(entities) != len(set(entities)):
            raise ValueError("proposal keys must be globally unique")
        units = {value.proposal_key for value in [*self.requirements, *self.technical_decisions]}
        checks = {value.proposal_key for value in self.acceptance_checks}
        item_keys = {value.proposal_key for value in self.items}
        owned_units = [key for item in self.items for key in [*item.requirement_proposal_keys, *item.technical_decision_proposal_keys]]
        owned_checks = [key for item in self.items for key in item.acceptance_check_proposal_keys]
        if set(owned_units) != units or len(owned_units) != len(units) or set(owned_checks) != checks or len(owned_checks) != len(checks):
            raise ValueError("proposal content must belong to exactly one item")
        if any(not set(value.related_unit_proposal_keys).issubset(units) for value in self.acceptance_checks):
            raise ValueError("acceptance check references unknown proposal units")
        if any(not set(value.dependency_item_proposal_keys).issubset(item_keys) for value in self.items):
            raise ValueError("item dependency references unknown proposal item")
        return self


class AnalyzerTurnResult(WorkshopModel):
    schema_version: Literal[1]
    turn_source_ref: SourceRef
    finding_proposals: Annotated[list[FindingProposal], Field(max_length=100)]
    complete_package_proposal: CompletePackageProposal | None
    control_intent: ControlIntent
    control_target: ControlTarget | None
    target_proposal_ref: str | None = Field(max_length=240)
    edit_instruction: str | None = Field(max_length=2000)
    acknowledgement: str | None = Field(max_length=240)
    next_question: str | None = Field(max_length=2000)

    @model_validator(mode="after")
    def exact_control_shape(self):
        if self.acknowledgement and len(self.acknowledgement.split()) > 5:
            raise ValueError("acknowledgement exceeds five words")
        if self.next_question:
            if len(self.next_question.split()) > 25 or not self.next_question.rstrip().endswith("?") or self.next_question.count("?") != 1:
                raise ValueError("next question must be one focused interrogative of at most 25 words")
        details = (self.control_target, self.target_proposal_ref, self.edit_instruction)
        if self.control_intent in {ControlIntent.NONE, ControlIntent.FINISH} and any(value is not None for value in details):
            raise ValueError("NONE/FINISH cannot carry control details")
        if self.control_intent in {ControlIntent.CONFIRM, ControlIntent.REJECT} and not (
            self.control_target == ControlTarget.WORKSHOP_PATCH and self.target_proposal_ref and self.edit_instruction is None
        ):
            raise ValueError("CONFIRM/REJECT require only a Workshop proposal reference")
        if self.control_intent == ControlIntent.EDIT and not (
            self.control_target == ControlTarget.WORKSHOP_PATCH and self.target_proposal_ref and self.edit_instruction
        ):
            raise ValueError("EDIT requires a Workshop proposal reference and instruction")
        return self


class AnalyzerRequest(WorkshopModel):
    request_id: UUID; session_id: UUID; effort: Literal["low", "medium"]
    phase: ConversationPhase; final_turn: TranscriptSnapshot
    committed_context_json: str


class SpecAnalyzerProvider(Protocol):
    async def analyze(self, request: AnalyzerRequest) -> AnalyzerTurnResult: ...


class GroundingChecker(Protocol):
    def supports(self, claim: str, evidence_refs: tuple[SourceRef, ...]) -> bool: ...


def proposal_id(session_id: UUID, entity_kind: str, proposal_key: str) -> UUID:
    return uuid5(PROPOSAL_NAMESPACE, f"{session_id}:{entity_kind}:{proposal_key}")


def validate_phase(result: AnalyzerTurnResult, phase: ConversationPhase) -> None:
    if phase == ConversationPhase.HANDOFF_READY and result.control_intent != ControlIntent.NONE:
        raise ValueError("HANDOFF_READY permits only NONE control")
    if phase == ConversationPhase.COMPLETE:
        raise ValueError("COMPLETE does not accept analyzer output")
    if phase != ConversationPhase.WORKSHOP and result.complete_package_proposal is not None:
        raise ValueError("package proposals are WORKSHOP-only")
