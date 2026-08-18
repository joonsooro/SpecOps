"""Task 28 channel-neutral contracts for the text-authoritative Workshop.

These DTOs are application contracts.  They intentionally do not change the
frozen Workshop Protocol 1.0.0 models owned by Tasks 25--27.
"""

from __future__ import annotations

import unicodedata
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator
from specops_contracts import workshop_v1 as c
from specops_workflow.models import SourceRef


MAX_INT = 9_223_372_036_854_775_807
PositiveInt = Annotated[int, Field(strict=True, ge=1, le=MAX_INT)]
Revision = Annotated[int, Field(strict=True, ge=0, le=MAX_INT)]
ShortText = Annotated[
    str, StringConstraints(strict=True, min_length=1, max_length=255, strip_whitespace=False)
]
Hash = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]


class ContractModel(BaseModel):
    # JSON transports UUIDs and enums as strings; scalar fields that must not
    # coerce (revisions, hashes, text) carry their own strict constraints.
    model_config = ConfigDict(extra="forbid", frozen=True, strict=False)


def normalize_response_text(value: str) -> str:
    if type(value) is not str:
        raise TypeError("response text must be a string")
    normalized = unicodedata.normalize("NFC", value.strip())
    if not normalized or len(normalized) > 100_000:
        raise ValueError("response text must contain 1..100000 code points")
    if "\x00" in normalized or any(
        unicodedata.category(character) in {"Cc", "Cf"}
        for character in normalized
    ):
        raise ValueError("response text contains a forbidden control character")
    return normalized


class InputChannel(StrEnum):
    CHAT = "CHAT"
    VOICE_CONFIRMED = "VOICE_CONFIRMED"


class ProposalBinding(ContractModel):
    proposal_ref: ShortText
    proposal_version: PositiveInt
    base_case_revision: Revision
    payload_hash: Hash


class SubmitTypedResponseIntent(ContractModel):
    client_submission_id: UUID
    question_id: UUID
    expected_question_version: PositiveInt
    text: str
    correction_of_response_id: UUID | None
    edit_target: ProposalBinding | None

    @field_validator("text")
    @classmethod
    def valid_text(cls, value: str) -> str:
        return normalize_response_text(value)


class CommittedParticipantTurn(ContractModel):
    client_submission_id: UUID
    question_id: UUID
    expected_question_version: PositiveInt
    normalized_text: str
    correction_of_response_id: UUID | None
    edit_target: ProposalBinding | None
    input_channel: InputChannel
    channel_confirmation_receipt_id: UUID | None

    @field_validator("normalized_text")
    @classmethod
    def valid_text(cls, value: str) -> str:
        return normalize_response_text(value)


class FoundationAdmittedQuestion(ContractModel):
    question_id: UUID
    question_version: PositiveInt
    exact_text: str
    reason: str
    dependencies: tuple[c.AdmittedGuidanceDependency, ...]


class TypedResponseSnapshot(ContractModel):
    response_id: UUID
    session_id: UUID
    question_id: UUID
    question_version: PositiveInt
    turn_sequence: PositiveInt
    response_version: PositiveInt
    normalized_text: str
    content_hash: Hash
    final_source_ref: SourceRef
    client_submission_id: UUID
    correction_of_response_id: UUID | None
    input_channel: InputChannel
    channel_confirmation_receipt_id: UUID | None
    created_at: datetime


class ParticipantTurnReceipt(ContractModel):
    snapshot: TypedResponseSnapshot
    analyzer_job_id: UUID
    recovery_code: Literal["ANALYSIS_QUEUED", "ANALYSIS_FAILED"]
    replayed: bool


class CommittedQuestionResponseTurn(ContractModel):
    question: FoundationAdmittedQuestion
    response: TypedResponseSnapshot


class QuestionRunway(ContractModel):
    questions: Annotated[tuple[FoundationAdmittedQuestion, ...], Field(max_length=6)]
    runway_depth: Annotated[int, Field(strict=True, ge=0, le=6)]


class ProposalInteractionStatus(StrEnum):
    PENDING = "PENDING"
    EDIT_REQUESTED = "EDIT_REQUESTED"
    SUPERSEDED = "SUPERSEDED"
    REJECTED = "REJECTED"
    COMMITTED = "COMMITTED"


class ProposalStatusProjection(ContractModel):
    binding: ProposalBinding
    status: ProposalInteractionStatus
    view: c.DecisionBatchReviewView


class WorkshopConversationContext(ContractModel):
    session_id: UUID
    case_revision: Revision
    workshop_state: Literal["NOT_STARTED", "ACTIVE", "FINISHING", "COMPLETED", "BLOCKED"]
    conversation_phase: Literal["WORKSHOP", "HANDOFF_READY", "COMPLETE"]
    session_card: c.VoiceSessionCard
    committed_turns: Annotated[tuple[CommittedQuestionResponseTurn, ...], Field(max_length=500)]
    question_runway: QuestionRunway
    turn_submission_status: Literal["READY", "ANALYSIS_PENDING", "ANALYSIS_FAILED"]
    proposal_statuses: Annotated[tuple[ProposalStatusProjection, ...], Field(max_length=100)]
    completion_status: Literal[
        "FINISHING_ANALYSIS", "HANDOFF_READY", "FINISH_FAILED"
    ] | None
    generated_at: datetime


class VisualProposalActionIntent(ContractModel):
    client_action_id: UUID
    binding: ProposalBinding


class VisualProposalActionReceipt(ContractModel):
    client_action_id: UUID
    binding: ProposalBinding
    status: ProposalInteractionStatus
    replayed: bool


class FinishWorkshopIntent(ContractModel):
    client_action_id: UUID
    expected_case_revision: Revision


class ExactTextPlaybackIntent(ContractModel):
    question_id: UUID
    question_version: PositiveInt
    exact_text: str


class ExactTextPlaybackReceipt(ContractModel):
    exact_text: str
    accepted: Literal[True] = True


class ChatbotQuestionRef(ContractModel):
    question_id: UUID
    question_version: PositiveInt


class ChatbotGuidanceRequest(ContractModel):
    request_identity: ShortText
    session_id: UUID
    case_revision: Revision
    readiness: c.Readiness
    review_obligation: c.ReviewObligation
    askable_question_refs: Annotated[tuple[ChatbotQuestionRef, ...], Field(min_length=1, max_length=100)]
    consumed_question_refs: Annotated[tuple[ChatbotQuestionRef, ...], Field(max_length=500)]
    latest_response_id: UUID | None
    latest_question_ref: ChatbotQuestionRef | None


class ChatbotGuidanceSelection(ContractModel):
    recommended_question_ref: ChatbotQuestionRef
    safe_alternate_refs: Annotated[tuple[ChatbotQuestionRef, ...], Field(max_length=5)]
    do_not_ask_question_refs: Annotated[tuple[ChatbotQuestionRef, ...], Field(max_length=50)]
    acknowledgement_suggestion: c.Statement
