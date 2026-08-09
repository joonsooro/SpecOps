from __future__ import annotations

import json
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from specops_workflow.enums import (
    ApprovalScope,
    Domain,
    ItemReadiness,
    PackageReadiness,
    ReviewObligation,
)
from specops_workflow.models import (
    SourceRef,
    SpecPackageContentView,
    SpecPackageGovernanceView,
)

from .analyzer import AnalyzerRequest
from .contracts import ConversationPhase, WorkshopModel


TERRA_EGRESS_POLICY_VERSION = "terra-v0-minimum-v1"


class TerraActorIdentity(WorkshopModel):
    role: Literal["PM", "DEV_LEAD"]
    actor_id: UUID


class TerraDelegatedAuthority(WorkshopModel):
    delegator_role: Literal["DEV_LEAD"] = "DEV_LEAD"
    delegate_role: Literal["PM"] = "PM"
    domain: Literal["TECHNICAL"]
    command_scope: tuple[str, ...]
    active: Literal[True] = True
    later_review_required: Literal[True]


class TerraAuthorityProjection(WorkshopModel):
    actors: tuple[TerraActorIdentity, TerraActorIdentity]
    technical_delegation: TerraDelegatedAuthority

    @model_validator(mode="after")
    def exact_actor_directory(self):
        if tuple(actor.role for actor in self.actors) != ("PM", "DEV_LEAD"):
            raise ValueError("Terra authority projection requires ordered PM and Dev Lead identities")
        if self.actors[0].actor_id == self.actors[1].actor_id:
            raise ValueError("Terra authority actors must be distinct")
        return self


class TerraSourceDocument(WorkshopModel):
    source_name: Literal["PM_SPECS", "DEV_LEAD_TECHNICAL_SPEC"]
    artifact_id: UUID
    version: Literal[1]
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    content: str = Field(min_length=1, max_length=250_000)


class TerraCommittedItem(WorkshopModel):
    existing_item_id: UUID
    title: str = Field(min_length=1, max_length=255)
    requirement_ids: tuple[UUID, ...]
    technical_decision_ids: tuple[UUID, ...]
    acceptance_check_ids: tuple[UUID, ...]
    dependency_item_ids: tuple[UUID, ...]
    domain: Domain
    readiness: ItemReadiness
    review_obligation: ReviewObligation
    complete: bool
    approval_scopes: tuple[ApprovalScope, ...]


class TerraCommittedRequirement(WorkshopModel):
    existing_unit_id: UUID
    statement: str = Field(min_length=1, max_length=20_000)
    domain: Domain
    delivery_required: bool
    source_refs: tuple[SourceRef, ...]


class TerraCommittedAcceptanceCheck(WorkshopModel):
    existing_check_id: UUID
    statement: str = Field(min_length=1, max_length=20_000)
    domain: Domain
    related_unit_ids: tuple[UUID, ...]
    source_refs: tuple[SourceRef, ...]


class TerraCommittedPackageState(WorkshopModel):
    existing_package_id: UUID | None
    package_readiness: PackageReadiness | None
    requirements: tuple[TerraCommittedRequirement, ...]
    technical_decisions: tuple[TerraCommittedRequirement, ...]
    acceptance_checks: tuple[TerraCommittedAcceptanceCheck, ...]
    items: tuple[TerraCommittedItem, ...]
    edit_instruction: str | None = Field(default=None, max_length=20_000)


class TerraFinalTranscriptEvidence(WorkshopModel):
    turn_sequence: int = Field(ge=1)
    version: int = Field(ge=1)
    text: str = Field(min_length=1, max_length=100_000)
    source_ref: SourceRef
    corrects_version: int | None = Field(default=None, ge=1)


class TerraCurrentFinalTurn(WorkshopModel):
    turn_sequence: int = Field(ge=1)
    version: int = Field(ge=1)


class TerraEgressEnvelope(WorkshopModel):
    phase: ConversationPhase
    authority: TerraAuthorityProjection
    documents: tuple[TerraSourceDocument, TerraSourceDocument]
    committed_package: TerraCommittedPackageState
    final_transcripts: tuple[TerraFinalTranscriptEvidence, ...]
    current_final_turn: TerraCurrentFinalTurn


class TerraEgressBlockSummary(WorkshopModel):
    label: str = Field(pattern=r"^[A-Z_]+$")
    byte_count: int = Field(ge=0)
    cacheable: bool


class TerraEgressManifest(WorkshopModel):
    destination: Literal["OPENAI_TERRA"] = "OPENAI_TERRA"
    policy_version: Literal["terra-v0-minimum-v1"] = TERRA_EGRESS_POLICY_VERSION
    blocks: tuple[TerraEgressBlockSummary, ...]


class TerraPrivacyEgressGateway:
    """Fail-closed V0 projection for the sole authorized Terra destination."""

    labels = (
        "INSTRUCTIONS_AND_PHASE",
        "AUTHORITY_METADATA",
        "PM_SPECS_DOCUMENT",
        "DEV_LEAD_TECHNICAL_SPEC_DOCUMENT",
        "COMMITTED_PACKAGE_STATE",
        "FINAL_TRANSCRIPT_EVIDENCE",
        "CURRENT_FINAL_TURN",
    )
    cacheable_labels = frozenset(labels[:4])

    def project(self, request: AnalyzerRequest) -> TerraEgressEnvelope:
        context = request.source_context
        if context is None:
            raise ValueError("Terra analysis requires the closed authorized source context")
        if tuple(document.source_name for document in context.documents) != (
            "PM_SPECS",
            "DEV_LEAD_TECHNICAL_SPEC",
        ):
            raise ValueError("Terra source context must contain the closed ordered document set")

        committed = json.loads(request.committed_context_json)
        if not isinstance(committed, dict) or set(committed) != {
            "content", "governance", "edit_instruction"
        }:
            raise ValueError("Terra committed context must match the closed V0 gateway contract")
        governance_value = committed["governance"]
        governance = (
            None
            if governance_value is None
            else SpecPackageGovernanceView.model_validate_json(
                json.dumps(governance_value, sort_keys=True, separators=(",", ":"))
            )
        )
        content_value = committed["content"]
        content = (
            None
            if content_value is None
            else SpecPackageContentView.model_validate_json(
                json.dumps(content_value, sort_keys=True, separators=(",", ":"))
            )
        )
        if (governance is None) != (content is None):
            raise ValueError("Terra committed content and governance must be present together")
        if governance is not None and content is not None:
            if governance.package_binding != content.package_binding:
                raise ValueError("Terra committed content and governance bindings must match")
            governance_items = {item.binding.item_id: item for item in governance.items}
            content_items = {item.item_id: item for item in content.payload.items}
            if set(governance_items) != set(content_items):
                raise ValueError("Terra committed item content and governance must match")
            if any(
                governance_items[item_id].title != content_items[item_id].title
                for item_id in content_items
            ):
                raise ValueError("Terra committed item titles must match")
        else:
            governance_items = {}
        package = TerraCommittedPackageState(
            existing_package_id=(
                None if governance is None else governance.package_binding.artifact_id
            ),
            package_readiness=(
                None if governance is None else governance.package_readiness
            ),
            requirements=() if content is None else tuple(
                TerraCommittedRequirement(
                    existing_unit_id=value.unit_id,
                    statement=value.statement,
                    domain=value.domain,
                    delivery_required=value.delivery_required,
                    source_refs=tuple(value.source_refs),
                )
                for value in content.payload.requirements
            ),
            technical_decisions=() if content is None else tuple(
                TerraCommittedRequirement(
                    existing_unit_id=value.unit_id,
                    statement=value.statement,
                    domain=value.domain,
                    delivery_required=value.delivery_required,
                    source_refs=tuple(value.source_refs),
                )
                for value in content.payload.technical_decisions
            ),
            acceptance_checks=() if content is None else tuple(
                TerraCommittedAcceptanceCheck(
                    existing_check_id=value.check_id,
                    statement=value.statement,
                    domain=value.domain,
                    related_unit_ids=tuple(value.related_unit_ids),
                    source_refs=tuple(value.source_refs),
                )
                for value in content.payload.acceptance_checks
            ),
            items=() if content is None else tuple(
                TerraCommittedItem(
                    existing_item_id=value.item_id,
                    title=value.title,
                    requirement_ids=tuple(value.requirement_ids),
                    technical_decision_ids=tuple(value.technical_decision_ids),
                    acceptance_check_ids=tuple(value.acceptance_check_ids),
                    dependency_item_ids=tuple(value.dependency_item_ids),
                    domain=governance_items[value.item_id].domain,
                    readiness=governance_items[value.item_id].readiness,
                    review_obligation=governance_items[value.item_id].review_obligation,
                    complete=governance_items[value.item_id].complete,
                    approval_scopes=tuple(governance_items[value.item_id].approval_scopes),
                )
                for value in content.payload.items
            ),
            edit_instruction=committed["edit_instruction"],
        )
        transcripts = tuple(
            TerraFinalTranscriptEvidence(
                turn_sequence=snapshot.turn_sequence,
                version=snapshot.version,
                text=snapshot.normalized_text,
                source_ref=snapshot.final_source_ref,
                corrects_version=snapshot.correction_of_version,
            )
            for snapshot in request.final_transcript_snapshots
        )
        if not transcripts:
            raise ValueError("Terra analysis requires provider-final transcript evidence")
        if not any(
            value.turn_sequence == request.final_turn.turn_sequence
            and value.version == request.final_turn.version
            and value.source_ref == request.final_turn.final_source_ref
            and value.text == request.final_turn.normalized_text
            for value in transcripts
        ):
            raise ValueError("the current final turn must be present in transcript evidence")

        return TerraEgressEnvelope(
            phase=request.phase,
            authority=TerraAuthorityProjection(
                actors=(
                    TerraActorIdentity(
                        role="PM",
                        actor_id=context.authority.pm_actor_id,
                    ),
                    TerraActorIdentity(
                        role="DEV_LEAD",
                        actor_id=context.authority.dev_lead_actor_id,
                    ),
                ),
                technical_delegation=TerraDelegatedAuthority(
                    domain=context.authority.delegation_domain,
                    command_scope=context.authority.delegation_command_scope,
                    later_review_required=context.authority.later_review_required,
                ),
            ),
            documents=tuple(
                TerraSourceDocument(
                    source_name=document.source_name,
                    artifact_id=document.artifact_id,
                    version=document.version,
                    content_hash=document.content_hash,
                    content=document.content,
                )
                for document in context.documents
            ),
            committed_package=package,
            final_transcripts=transcripts,
            current_final_turn=TerraCurrentFinalTurn(
                turn_sequence=request.final_turn.turn_sequence,
                version=request.final_turn.version,
            ),
        )

    def content_blocks(
        self,
        envelope: TerraEgressEnvelope,
        *,
        cache_static_prefix: bool = False,
    ) -> tuple[dict[str, object], ...]:
        values = (
            (
                "Analyze this provider-final PM Workshop turn. Return only the strict analyzer schema. "
                "Never invent evidence, identifiers, or Jira/GitHub actions. Cite exact registered "
                "SourceRefs from the supplied document identities and line ranges. During WORKSHOP, "
                "return a complete package proposal rather than a partial merge when formulation changes. "
                "Identify at least one unresolved or newly resolved decision from the Dev Lead decision "
                "agenda when the current PM turn addresses it, retaining its exact technical source pointer.\n"
                f"Phase: {envelope.phase.value}"
            ),
            envelope.authority.model_dump(mode="json"),
            envelope.documents[0].model_dump(mode="json"),
            envelope.documents[1].model_dump(mode="json"),
            envelope.committed_package.model_dump(mode="json"),
            [value.model_dump(mode="json") for value in envelope.final_transcripts],
            envelope.current_final_turn.model_dump(mode="json"),
        )
        blocks = tuple(
            {
                "type": "input_text",
                "text": label + "\n" + (
                    value
                    if isinstance(value, str)
                    else json.dumps(value, sort_keys=True, separators=(",", ":"))
                ),
            }
            for label, value in zip(self.labels, values, strict=True)
        )
        if not cache_static_prefix:
            return blocks
        mutable = [dict(block) for block in blocks]
        mutable[3]["prompt_cache_breakpoint"] = {"mode": "explicit"}
        return tuple(mutable)

    def cache_warm_blocks(
        self,
        envelope: TerraEgressEnvelope,
    ) -> tuple[dict[str, object], ...]:
        blocks = self.content_blocks(envelope, cache_static_prefix=True)
        source_ref = envelope.final_transcripts[-1].source_ref
        return blocks[:4] + ({
            "type": "input_text",
            "text": (
                "CACHE_WARMUP_CONTEXT\n"
                "Initialize only the reusable V0 source prefix. Return the strict schema with the "
                "supplied turn_source_ref, empty finding_proposals, null complete_package_proposal, "
                "NONE control, and every nullable control, acknowledgement, and question field null.\n"
                "TURN_SOURCE_REF\n"
                + source_ref.model_dump_json()
            ),
        },)

    def manifest(self, blocks: tuple[dict[str, object], ...]) -> TerraEgressManifest:
        if len(blocks) != len(self.labels):
            raise ValueError("Terra egress block count does not match the closed policy")
        summaries = []
        for index, (expected, block) in enumerate(zip(self.labels, blocks, strict=True)):
            expected_fields = {"type", "text"}
            if index == 3:
                expected_fields.add("prompt_cache_breakpoint")
            if set(block) != expected_fields or block["type"] != "input_text":
                raise ValueError("Terra egress blocks must be closed input_text values")
            if index == 3 and block["prompt_cache_breakpoint"] != {"mode": "explicit"}:
                raise ValueError("Terra egress cache breakpoint must close the static source prefix")
            text = block["text"]
            if not isinstance(text, str):
                raise ValueError("Terra egress block text must be a string")
            label, separator, _ = text.partition("\n")
            if not separator or label != expected:
                raise ValueError("Terra egress block order does not match the closed policy")
            summaries.append(TerraEgressBlockSummary(
                label=label,
                byte_count=len(text.encode("utf-8")),
                cacheable=label in self.cacheable_labels,
            ))
        return TerraEgressManifest(blocks=tuple(summaries))
