from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, model_validator

from specops_workflow.enums import AmbiguityCategory, Domain, Severity
from specops_workflow.models import SourceRef

from ..analyzer import (
    AcceptanceCheckProposal,
    AnalyzerRequest,
    AnalyzerTurnResult,
    CompletePackageProposal,
    ControlIntent,
    FindingProposal,
    ProposalDisposition,
    RequirementProposal,
    SpecPackageItemProposal,
    TechnicalDecisionProposal,
)
from ..contracts import ConversationPhase, WorkshopModel
from ..privacy_egress import TerraEgressEnvelope


class TerraCacheWarmResult(WorkshopModel):
    status: Literal["ready"]


class TerraFindingPlan(WorkshopModel):
    domain: Literal[Domain.TECHNICAL, Domain.CROSS_DOMAIN]
    evidence_refs: Annotated[list[SourceRef], Field(min_length=1, max_length=5)]
    clarification_question: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def focused_question(self):
        value = self.clarification_question
        if (
            len(value.split()) > 25
            or not value.rstrip().endswith("?")
            or value.count("?") != 1
        ):
            raise ValueError("finding question must be one focused interrogative of at most 25 words")
        return self


class TerraDecisionPlan(WorkshopModel):
    schema_version: Literal[1]
    turn_source_ref: SourceRef
    findings: Annotated[list[TerraFindingPlan], Field(min_length=1, max_length=1)]


class TerraFinishAuditPlan(WorkshopModel):
    schema_version: Literal[1]
    turn_source_ref: SourceRef
    findings: Annotated[list[TerraFindingPlan], Field(max_length=1)]


class TerraGroundedText(WorkshopModel):
    statement: str = Field(min_length=1, max_length=2000)
    evidence_refs: Annotated[list[SourceRef], Field(min_length=1, max_length=5)]


class TerraPackageOutline(WorkshopModel):
    schema_version: Literal[1]
    turn_source_ref: SourceRef
    item_title: str = Field(min_length=1, max_length=240)
    business_requirement: TerraGroundedText
    technical_decision: TerraGroundedText
    acceptance_check: TerraGroundedText


def assemble_analyzer_result(
    request: AnalyzerRequest,
    envelope: TerraEgressEnvelope,
    plan: TerraDecisionPlan | TerraFinishAuditPlan,
    outline: TerraPackageOutline | None,
) -> AnalyzerTurnResult:
    expected_ref = request.final_turn.final_source_ref
    if plan.turn_source_ref != expected_ref:
        raise ValueError("Terra decision plan turn SourceRef mismatch")
    package_change_required = requires_package_outline(request)
    pm_actor_id = envelope.authority.actors[0].actor_id
    dev_lead_actor_id = envelope.authority.actors[1].actor_id

    def owners(domain: Domain) -> list[UUID]:
        if domain == Domain.BUSINESS:
            return [pm_actor_id]
        if domain == Domain.TECHNICAL:
            return [dev_lead_actor_id]
        return [pm_actor_id, dev_lead_actor_id]

    finding_proposals = [
        FindingProposal(
            proposal_key=f"finding-{index:03d}",
            existing_finding_id=None,
            item_proposal_key="item-001",
            category=(
                AmbiguityCategory.MISSING_TECH_DECISION
                if value.domain in {Domain.TECHNICAL, Domain.CROSS_DOMAIN}
                else AmbiguityCategory.MISSING_OUTCOME
            ),
            domain=value.domain,
            severity=Severity.BLOCKING,
            evidence_refs=value.evidence_refs,
            clarification_question=value.clarification_question,
            owner_actor_ids=owners(value.domain),
            disposition=ProposalDisposition.OPEN,
        )
        for index, value in enumerate(plan.findings, start=1)
    ]

    package = None
    if package_change_required:
        if outline is None:
            raise ValueError("package change requires a complete package outline")
        if outline.turn_source_ref != expected_ref:
            raise ValueError("Terra package outline turn SourceRef mismatch")
        committed = envelope.committed_package
        if len(committed.items) > 1:
            raise ValueError("Workshop v0 provider decomposition supports one governed item")
        if (
            len(committed.requirements) > 1
            or len(committed.technical_decisions) > 1
            or len(committed.acceptance_checks) > 1
        ):
            raise ValueError("Workshop v0 provider decomposition supports one unit of each kind")

        requirements = [RequirementProposal(
            proposal_key="requirement-001",
            existing_unit_id=(
                committed.requirements[0].existing_unit_id
                if committed.requirements else None
            ),
            statement=outline.business_requirement.statement,
            domain=Domain.BUSINESS,
            delivery_required=True,
            source_refs=outline.business_requirement.evidence_refs,
        )]
        technical_decisions = [TechnicalDecisionProposal(
            proposal_key="technical-decision-001",
            existing_unit_id=(
                committed.technical_decisions[0].existing_unit_id
                if committed.technical_decisions else None
            ),
            statement=outline.technical_decision.statement,
            domain=Domain.TECHNICAL,
            delivery_required=True,
            source_refs=outline.technical_decision.evidence_refs,
        )]
        unit_keys = [
            value.proposal_key for value in [*requirements, *technical_decisions]
        ]
        acceptance_checks = [AcceptanceCheckProposal(
            proposal_key="acceptance-check-001",
            existing_check_id=(
                committed.acceptance_checks[0].existing_check_id
                if committed.acceptance_checks else None
            ),
            statement=outline.acceptance_check.statement,
            domain=Domain.CROSS_DOMAIN,
            related_unit_proposal_keys=unit_keys,
            source_refs=outline.acceptance_check.evidence_refs,
        )]
        package = CompletePackageProposal(
            proposal_key="package-001",
            existing_package_id=committed.existing_package_id,
            requirements=requirements,
            technical_decisions=technical_decisions,
            acceptance_checks=acceptance_checks,
            items=[SpecPackageItemProposal(
                proposal_key="item-001",
                existing_item_id=(
                    committed.items[0].existing_item_id if committed.items else None
                ),
                title=outline.item_title,
                requirement_proposal_keys=[value.proposal_key for value in requirements],
                technical_decision_proposal_keys=[
                    value.proposal_key for value in technical_decisions
                ],
                acceptance_check_proposal_keys=[
                    value.proposal_key for value in acceptance_checks
                ],
                dependency_item_proposal_keys=[],
            )],
        )

    return AnalyzerTurnResult(
        schema_version=1,
        turn_source_ref=expected_ref,
        finding_proposals=finding_proposals,
        complete_package_proposal=package,
        control_intent=ControlIntent.NONE,
        control_target=None,
        target_proposal_ref=None,
        edit_instruction=None,
        acknowledgement=(
            "Audit complete" if request.purpose == "FINISH_AUDIT" else "Evidence analyzed"
        ),
        next_question=(
            plan.findings[0].clarification_question
            if plan.findings
            else ("Should we confirm this package?" if package is not None else None)
        ),
    )


def requires_package_outline(request: AnalyzerRequest) -> bool:
    return request.purpose == "TURN" and request.phase == ConversationPhase.WORKSHOP
