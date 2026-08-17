"""Normative Pydantic contracts for Workshop Interaction Protocol 1.0.0.

This module freezes data shapes and local invariants. It intentionally contains no
provider SDK calls, persistence handlers, Foundation mutations, or UI behavior.
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)


PROTOCOL_VERSION = "1.0.0"
INITIAL_RUNWAY_SAFE_ALTERNATE_COUNT = 3
INITIAL_RUNWAY_DEPTH = 1 + INITIAL_RUNWAY_SAFE_ALTERNATE_COUNT
MAX_INT = 9_223_372_036_854_775_807

NonNegativeInt = Annotated[int, Field(strict=True, ge=0, le=MAX_INT)]
PositiveInt = Annotated[int, Field(strict=True, ge=1, le=MAX_INT)]
Probability = Annotated[float, Field(strict=True, ge=0.0, le=1.0)]
ShortText = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=255, strip_whitespace=False),
]
Statement = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=20_000, strip_whitespace=False),
]
SummaryText = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=80_000, strip_whitespace=False),
]
JsonObjectText = Annotated[
    str,
    StringConstraints(strict=True, min_length=2, max_length=2_000_000, strip_whitespace=False),
]
Sha256 = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^sha256:[0-9a-f]{64}$"),
]
CandidateKey = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^[a-z][a-z0-9]*(?:-[a-z0-9]+){0,11}$", max_length=96),
]
IdempotencyKey = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$"),
]
ProviderIdentifier = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,511}$"),
]
QuestionHandle = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^[A-Z]{1,2}$"),
]
SchemaPathSegment = Annotated[
    str,
    StringConstraints(
        strict=True,
        pattern=r"^(?:[a-z][a-z0-9_]{0,63}|[0-9]{1,4}|\$unknown)$",
    ),
]


def validate_json_object_text(value: str, label: str) -> str:
    duplicate = False

    def reject_duplicate(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        nonlocal duplicate
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                duplicate = True
            result[key] = item
        return result

    try:
        parsed = json.loads(value, object_pairs_hook=reject_duplicate)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be valid JSON") from exc
    if duplicate or not isinstance(parsed, dict):
        raise ValueError(f"{label} must be one JSON object with unique keys")
    return value


class ContractModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        validate_default=True,
    )

    @field_validator("*")
    @classmethod
    def reject_unsafe_scalars(cls, value: Any) -> Any:
        if type(value) is str:
            if value != value.strip() or "\x00" in value:
                raise ValueError("strings must be trimmed and contain no NUL")
            if any(ord(character) < 32 and character not in "\n\t" for character in value):
                raise ValueError("strings contain a prohibited control character")
            return unicodedata.normalize("NFC", value)
        if isinstance(value, datetime) and value.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware")
        return value


class SourceRole(StrEnum):
    PM_SPEC = "PM_SPEC"
    TECHNICAL_CONTRACT = "TECHNICAL_CONTRACT"


class ProviderName(StrEnum):
    OPENAI = "OPENAI"


class ReasoningEffort(StrEnum):
    MEDIUM = "medium"


class AnalyzerOperation(StrEnum):
    BOOTSTRAP = "BOOTSTRAP"
    TURN_ANALYSIS = "TURN_ANALYSIS"
    GUIDANCE = "GUIDANCE"
    REVIEW_NARRATION = "REVIEW_NARRATION"
    SPEC_PACKAGE_SYNTHESIS = "SPEC_PACKAGE_SYNTHESIS"
    TECHNICAL_CONTRACT_SYNTHESIS = "TECHNICAL_CONTRACT_SYNTHESIS"


class ContextStatus(StrEnum):
    ACTIVE = "ACTIVE"
    REBUILD_REQUIRED = "REBUILD_REQUIRED"
    REBUILDING = "REBUILDING"
    CLOSED = "CLOSED"
    FAILED = "FAILED"


class SourceLocatorKind(StrEnum):
    SOURCE_LINES = "SOURCE_LINES"
    JSON_POINTER = "JSON_POINTER"
    DOCUMENT_ANCHOR = "DOCUMENT_ANCHOR"
    QUOTE_SEARCH = "QUOTE_SEARCH"


class Readiness(StrEnum):
    READY = "READY"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    BLOCKED = "BLOCKED"
    FORMULATING = "FORMULATING"


class ReviewObligation(StrEnum):
    NONE = "NONE"
    LATER_REVIEW = "LATER_REVIEW"
    DECISION_REQUIRED = "DECISION_REQUIRED"


class Domain(StrEnum):
    PRODUCT = "PRODUCT"
    TECHNICAL = "TECHNICAL"
    CROSS_DOMAIN = "CROSS_DOMAIN"
    POLICY = "POLICY"
    SECURITY = "SECURITY"
    PRIVACY = "PRIVACY"
    DATA = "DATA"
    ACCEPTANCE = "ACCEPTANCE"


class SourceIdentity(ContractModel):
    source_id: UUID
    role: SourceRole
    version: PositiveInt
    payload_hash: Sha256
    canonical_locator: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=4096)]
    filename: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=255)]
    media_type: Literal["text/markdown"]


class ProviderSourceBinding(ContractModel):
    source: SourceIdentity
    provider_file_id: ProviderIdentifier


class SourceSetBinding(ContractModel):
    source_set_hash: Sha256
    ordered_sources: Annotated[tuple[ProviderSourceBinding, ...], Field(min_length=2, max_length=2)]

    @model_validator(mode="after")
    def exact_ordered_roles(self) -> SourceSetBinding:
        roles = tuple(item.source.role for item in self.ordered_sources)
        if roles != (SourceRole.PM_SPEC, SourceRole.TECHNICAL_CONTRACT):
            raise ValueError("sources must be ordered PM_SPEC then TECHNICAL_CONTRACT")
        if len({item.source.source_id for item in self.ordered_sources}) != 2:
            raise ValueError("source IDs must be unique")
        return self


class AnalyzerContractBinding(ContractModel):
    protocol_version: Literal["1.0.0"]
    instruction_set_id: Literal["specops-workshop-analyzer"]
    instruction_set_version: PositiveInt
    instruction_set_hash: Sha256
    semantic_quality_contract_id: Literal["SEMANTIC-QUALITY-CONTRACT"]
    semantic_quality_contract_version: Literal["2.2.0"]
    semantic_quality_contract_hash: Sha256
    provider_schema_version: Literal["1.0.0"]
    model: Literal["gpt-5.6-terra"]
    reasoning_effort: Literal[ReasoningEffort.MEDIUM]


class AnalyzerContextBinding(ContractModel):
    protocol_version: Literal["1.0.0"]
    context_id: UUID
    session_id: UUID
    provider: Literal[ProviderName.OPENAI]
    provider_conversation_id: ProviderIdentifier
    bootstrap_response_id: ProviderIdentifier
    model: Literal["gpt-5.6-terra"]
    reasoning_effort: Literal[ReasoningEffort.MEDIUM]
    conversation_state_persisted: Literal[True]
    response_store_enabled: Literal[True]
    analyzer_contract: AnalyzerContractBinding
    source_set: SourceSetBinding
    status: ContextStatus
    created_at: datetime
    invalidated_at: datetime | None
    invalidation_reason: ShortText | None

    @model_validator(mode="after")
    def closed_lifecycle_shape(self) -> AnalyzerContextBinding:
        invalid = self.status is not ContextStatus.ACTIVE
        if invalid != (self.invalidated_at is not None):
            raise ValueError("non-active context requires invalidated_at")
        if invalid != (self.invalidation_reason is not None):
            raise ValueError("non-active context requires invalidation_reason")
        return self


class ActorAuthenticationMethod(StrEnum):
    VERBAL_SELF_ASSERTION = "VERBAL_SELF_ASSERTION"
    ENTERPRISE_SSO = "ENTERPRISE_SSO"


class AssuranceLevel(StrEnum):
    SELF_ASSERTED = "SELF_ASSERTED"
    VERIFIED = "VERIFIED"


class VerbalSelfAssertion(ContractModel):
    authentication_method: Literal[ActorAuthenticationMethod.VERBAL_SELF_ASSERTION]
    assurance_level: Literal[AssuranceLevel.SELF_ASSERTED]
    actor_id: UUID
    asserted_display_name: ShortText
    claimed_role: ShortText
    assertion_transcript_event_id: UUID


class EnterpriseSsoAuthentication(ContractModel):
    authentication_method: Literal[ActorAuthenticationMethod.ENTERPRISE_SSO]
    assurance_level: Literal[AssuranceLevel.VERIFIED]
    actor_id: UUID
    identity_provider: ShortText
    external_subject: ShortText
    authentication_event_id: UUID


ActorAuthentication = Annotated[
    VerbalSelfAssertion | EnterpriseSsoAuthentication,
    Field(discriminator="authentication_method"),
]


class EventEnvelope(ContractModel):
    protocol_version: Literal["1.0.0"]
    event_id: UUID
    case_id: UUID
    session_id: UUID
    correlation_id: UUID
    causation_id: UUID | None
    event_sequence: PositiveInt
    observed_case_revision: NonNegativeInt
    occurred_at: datetime
    producer: Literal["VOICE", "ANALYZER_ORCHESTRATOR", "FOUNDATION"]


class CommandEnvelope(ContractModel):
    protocol_version: Literal["1.0.0"]
    command_id: UUID
    case_id: UUID
    session_id: UUID
    correlation_id: UUID
    causation_id: UUID | None
    idempotency_key: IdempotencyKey
    issued_at: datetime
    acting_actor_id: UUID | Literal["SYSTEM"]


class StrictRevisionCommandEnvelope(CommandEnvelope):
    expected_case_revision: NonNegativeInt


class ReviewBoundCommandEnvelope(CommandEnvelope):
    observed_case_revision: NonNegativeInt


class TranscriptActor(StrEnum):
    PM = "PM"
    DEV_LEAD = "DEV_LEAD"
    OTHER_PARTICIPANT = "OTHER_PARTICIPANT"
    VOICE_AGENT = "VOICE_AGENT"


class SpeakerAttributionMethod(StrEnum):
    VERBAL_SELF_ASSERTION = "VERBAL_SELF_ASSERTION"
    ENTERPRISE_SSO = "ENTERPRISE_SSO"
    VOICE_SYSTEM = "VOICE_SYSTEM"


class TranscriptFinalizedEvent(EventEnvelope):
    event_type: Literal["TRANSCRIPT_FINALIZED"]
    producer: Literal["VOICE"]
    turn_id: UUID
    transcript_artifact_id: UUID
    transcript_version: PositiveInt
    transcript_hash: Sha256
    actor: TranscriptActor
    speaker_actor_id: UUID | None
    speaker_attribution_method: SpeakerAttributionMethod
    sequence_number: PositiveInt
    text: Statement

    @model_validator(mode="after")
    def valid_speaker_attribution(self) -> TranscriptFinalizedEvent:
        is_voice = self.actor is TranscriptActor.VOICE_AGENT
        if is_voice != (self.speaker_attribution_method is SpeakerAttributionMethod.VOICE_SYSTEM):
            raise ValueError("only VOICE_AGENT uses VOICE_SYSTEM attribution")
        if is_voice != (self.speaker_actor_id is None):
            raise ValueError("participant transcripts require speaker_actor_id")
        return self


class SourceLineLocator(ContractModel):
    locator_kind: Literal[SourceLocatorKind.SOURCE_LINES]
    start_line: PositiveInt
    end_line: PositiveInt

    @model_validator(mode="after")
    def ordered(self) -> SourceLineLocator:
        if self.start_line > self.end_line:
            raise ValueError("start_line must not exceed end_line")
        return self


class JsonPointerLocator(ContractModel):
    locator_kind: Literal[SourceLocatorKind.JSON_POINTER]
    pointer: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=4096)]

    @field_validator("pointer")
    @classmethod
    def valid_pointer(cls, value: str) -> str:
        if not value.startswith("/") or re.fullmatch(r"(?:[^~]|~[01])*", value) is None:
            raise ValueError("pointer must be a valid non-root RFC 6901 JSON Pointer")
        return value


class DocumentAnchorLocator(ContractModel):
    locator_kind: Literal[SourceLocatorKind.DOCUMENT_ANCHOR]
    anchor: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=500)]


class QuoteSearchLocator(ContractModel):
    locator_kind: Literal[SourceLocatorKind.QUOTE_SEARCH]
    exact_quote: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=4000)]
    occurrence: PositiveInt


SourceLocator = Annotated[
    SourceLineLocator | JsonPointerLocator | DocumentAnchorLocator | QuoteSearchLocator,
    Field(discriminator="locator_kind"),
]


class EvidenceCandidate(ContractModel):
    candidate_key: CandidateKey
    source_role: SourceRole
    locator: SourceLocator
    relevance_claim: Statement
    quoted_text_candidate: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=8000)] | None

    @model_validator(mode="after")
    def quote_matches_quote_search_locator(self) -> EvidenceCandidate:
        if isinstance(self.locator, QuoteSearchLocator):
            if self.quoted_text_candidate != self.locator.exact_quote:
                raise ValueError(
                    "QUOTE_SEARCH quoted_text_candidate must exactly equal locator exact_quote"
                )
        return self


class ProblemKind(StrEnum):
    AMBIGUITY = "AMBIGUITY"
    MISSING_DECISION = "MISSING_DECISION"
    CONTRADICTION = "CONTRADICTION"
    RISK = "RISK"
    MISSING_ACCEPTANCE = "MISSING_ACCEPTANCE"
    MISSING_AUTHORITY = "MISSING_AUTHORITY"
    MISSING_EVIDENCE = "MISSING_EVIDENCE"


class Severity(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class ProblemCandidate(ContractModel):
    candidate_key: CandidateKey
    problem_kind: ProblemKind
    domain: Domain
    severity: Severity
    statement: Statement
    consequence: Statement
    evidence_candidate_keys: Annotated[tuple[CandidateKey, ...], Field(min_length=1, max_length=25)]

    @model_validator(mode="after")
    def unique_evidence(self) -> ProblemCandidate:
        if len(self.evidence_candidate_keys) != len(set(self.evidence_candidate_keys)):
            raise ValueError("problem evidence keys must be unique")
        return self


class ClusterCouplingType(StrEnum):
    SHARED_OUTCOME = "SHARED_OUTCOME"
    SHARED_CONSTRAINT = "SHARED_CONSTRAINT"
    DECISION_DEPENDENCY = "DECISION_DEPENDENCY"
    MUTUAL_EXCLUSION = "MUTUAL_EXCLUSION"
    COMMON_EVIDENCE = "COMMON_EVIDENCE"
    WORKFLOW_SEQUENCE = "WORKFLOW_SEQUENCE"


class SuggestedConfirmationMode(StrEnum):
    TOGETHER = "TOGETHER"
    SEPARATE = "SEPARATE"
    CONDITIONAL = "CONDITIONAL"


class ProblemClusterCandidate(ContractModel):
    candidate_key: CandidateKey
    title: ShortText
    problem_keys: Annotated[tuple[CandidateKey, ...], Field(min_length=2, max_length=20)]
    coupling_type: ClusterCouplingType
    coupling_reason: Statement
    suggested_confirmation_mode: SuggestedConfirmationMode

    @model_validator(mode="after")
    def unique_problems(self) -> ProblemClusterCandidate:
        if len(self.problem_keys) != len(set(self.problem_keys)):
            raise ValueError("cluster problem keys must be unique")
        return self


class QuestionShape(StrEnum):
    OPEN_TEXT = "OPEN_TEXT"
    CLOSED_BOOLEAN = "CLOSED_BOOLEAN"
    CLOSED_ENUM = "CLOSED_ENUM"
    CLOSED_INTEGER = "CLOSED_INTEGER"
    CLOSED_DECIMAL = "CLOSED_DECIMAL"
    CLOSED_TEXT = "CLOSED_TEXT"


class CapturePolicy(StrEnum):
    CLARIFICATION_ONLY = "CLARIFICATION_ONLY"
    LOW_RISK_FACT = "LOW_RISK_FACT"
    BINDING_DECISION = "BINDING_DECISION"


class QuestionCandidate(ContractModel):
    candidate_key: CandidateKey
    text: Statement
    rationale: Statement
    question_shape: QuestionShape
    capture_policy: CapturePolicy
    answer_options: Annotated[tuple[ShortText, ...], Field(max_length=20)]
    addresses_problem_keys: Annotated[tuple[CandidateKey, ...], Field(min_length=1, max_length=20)]
    prerequisite_problem_keys: Annotated[tuple[CandidateKey, ...], Field(max_length=20)]
    safe_without_current_turn_interpretation: bool

    @model_validator(mode="after")
    def valid_question_shape(self) -> QuestionCandidate:
        if self.question_shape is QuestionShape.CLOSED_ENUM:
            if len(self.answer_options) < 2:
                raise ValueError("CLOSED_ENUM requires at least two answer options")
        elif self.answer_options:
            raise ValueError("answer_options are allowed only for CLOSED_ENUM")
        if self.question_shape is QuestionShape.OPEN_TEXT and self.capture_policy is CapturePolicy.LOW_RISK_FACT:
            raise ValueError("OPEN_TEXT cannot use LOW_RISK_FACT capture")
        if len(self.addresses_problem_keys) != len(set(self.addresses_problem_keys)):
            raise ValueError("addressed problem keys must be unique")
        if len(self.prerequisite_problem_keys) != len(set(self.prerequisite_problem_keys)):
            raise ValueError("prerequisite problem keys must be unique")
        return self


class QuestionRunwayCandidate(ContractModel):
    recommended_question_key: CandidateKey
    safe_alternate_question_keys: Annotated[
        tuple[CandidateKey, ...],
        Field(
            min_length=INITIAL_RUNWAY_SAFE_ALTERNATE_COUNT,
            max_length=INITIAL_RUNWAY_SAFE_ALTERNATE_COUNT,
        ),
    ]
    do_not_ask_question_keys: Annotated[tuple[CandidateKey, ...], Field(min_length=0, max_length=50)]

    @model_validator(mode="after")
    def unique_and_disjoint(self) -> QuestionRunwayCandidate:
        safe = (self.recommended_question_key, *self.safe_alternate_question_keys)
        if len(safe) != len(set(safe)):
            raise ValueError("safe runway questions must be unique")
        if set(safe).intersection(self.do_not_ask_question_keys):
            raise ValueError("safe and do-not-ask questions must be disjoint")
        return self


class ConfirmationCheckpointCandidate(ContractModel):
    candidate_key: CandidateKey
    cluster_keys: Annotated[tuple[CandidateKey, ...], Field(min_length=1, max_length=10)]
    trigger_description: Statement


class InterviewBriefCandidate(ContractModel):
    protocol_version: Literal["1.0.0"]
    output_type: Literal["INTERVIEW_BRIEF_CANDIDATE"]
    analyzer_run_id: UUID
    context_id: UUID
    request_hash: Sha256
    source_set_hash: Sha256
    based_on_case_revision: NonNegativeInt
    customer_promise_summary: SummaryText
    evidence_candidates: Annotated[tuple[EvidenceCandidate, ...], Field(min_length=1, max_length=500)]
    problems: Annotated[tuple[ProblemCandidate, ...], Field(min_length=1, max_length=500)]
    problem_clusters: Annotated[tuple[ProblemClusterCandidate, ...], Field(max_length=100)]
    questions: Annotated[tuple[QuestionCandidate, ...], Field(min_length=1, max_length=500)]
    initial_runway: QuestionRunwayCandidate
    confirmation_checkpoints: Annotated[tuple[ConfirmationCheckpointCandidate, ...], Field(max_length=100)]

    @model_validator(mode="after")
    def valid_graph(self) -> InterviewBriefCandidate:
        groups = (
            self.evidence_candidates,
            self.problems,
            self.problem_clusters,
            self.questions,
            self.confirmation_checkpoints,
        )
        all_keys = [item.candidate_key for group in groups for item in group]
        if len(all_keys) != len(set(all_keys)):
            raise ValueError("candidate keys must be unique across the brief")
        evidence_keys = {item.candidate_key for item in self.evidence_candidates}
        problem_keys = {item.candidate_key for item in self.problems}
        cluster_keys = {item.candidate_key for item in self.problem_clusters}
        question_keys = {item.candidate_key for item in self.questions}
        if any(not set(item.evidence_candidate_keys).issubset(evidence_keys) for item in self.problems):
            raise ValueError("problem references unknown evidence candidate")
        if any(not set(item.problem_keys).issubset(problem_keys) for item in self.problem_clusters):
            raise ValueError("cluster references unknown problem")
        if any(
            not set((*item.addresses_problem_keys, *item.prerequisite_problem_keys)).issubset(problem_keys)
            for item in self.questions
        ):
            raise ValueError("question references unknown problem")
        runway_keys = {
            self.initial_runway.recommended_question_key,
            *self.initial_runway.safe_alternate_question_keys,
            *self.initial_runway.do_not_ask_question_keys,
        }
        if not runway_keys.issubset(question_keys):
            raise ValueError("runway references unknown question")
        if any(not set(item.cluster_keys).issubset(cluster_keys) for item in self.confirmation_checkpoints):
            raise ValueError("checkpoint references unknown cluster")
        return self


class CandidateEntityRef(ContractModel):
    ref_kind: Literal["CANDIDATE_KEY"]
    candidate_key: CandidateKey


class FoundationEntityRef(ContractModel):
    ref_kind: Literal["FOUNDATION_ID"]
    foundation_id: UUID
    expected_version: PositiveInt


EntityRef = Annotated[CandidateEntityRef | FoundationEntityRef, Field(discriminator="ref_kind")]


class NormalizedScalarType(StrEnum):
    TEXT = "TEXT"
    BOOLEAN = "BOOLEAN"
    INTEGER = "INTEGER"
    DECIMAL = "DECIMAL"
    ENUM = "ENUM"


class LowRiskFactCandidate(ContractModel):
    candidate_key: CandidateKey
    source_question_ref: EntityRef
    transcript_event_id: UUID
    value_type: NormalizedScalarType
    normalized_value: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=4000)]
    confidence: Probability


class ProblemResolutionKind(StrEnum):
    FULL = "FULL"
    PARTIAL = "PARTIAL"
    CONTRIBUTES = "CONTRIBUTES"
    DOES_NOT_RESOLVE = "DOES_NOT_RESOLVE"


class ProblemResolutionLink(ContractModel):
    problem_ref: EntityRef
    resolution_kind: ProblemResolutionKind


class TurnProblemCandidate(ContractModel):
    candidate_key: CandidateKey
    problem_kind: ProblemKind
    domain: Domain
    severity: Severity
    statement: Statement
    consequence: Statement
    evidence_refs: Annotated[tuple[EntityRef, ...], Field(min_length=1, max_length=25)]


class TurnProblemClusterCandidate(ContractModel):
    candidate_key: CandidateKey
    title: ShortText
    problem_refs: Annotated[tuple[EntityRef, ...], Field(min_length=2, max_length=20)]
    coupling_type: ClusterCouplingType
    coupling_reason: Statement
    suggested_confirmation_mode: SuggestedConfirmationMode


class TurnQuestionCandidate(ContractModel):
    candidate_key: CandidateKey
    text: Statement
    rationale: Statement
    question_shape: QuestionShape
    capture_policy: CapturePolicy
    answer_options: Annotated[tuple[ShortText, ...], Field(max_length=20)]
    addresses_problem_refs: Annotated[tuple[EntityRef, ...], Field(min_length=1, max_length=20)]
    prerequisite_problem_refs: Annotated[tuple[EntityRef, ...], Field(max_length=20)]
    safe_without_current_turn_interpretation: bool

    @model_validator(mode="after")
    def valid_question_shape(self) -> TurnQuestionCandidate:
        if self.question_shape is QuestionShape.CLOSED_ENUM:
            if len(self.answer_options) < 2:
                raise ValueError("CLOSED_ENUM requires at least two answer options")
        elif self.answer_options:
            raise ValueError("answer_options are allowed only for CLOSED_ENUM")
        if self.question_shape is QuestionShape.OPEN_TEXT and self.capture_policy is CapturePolicy.LOW_RISK_FACT:
            raise ValueError("OPEN_TEXT cannot use LOW_RISK_FACT capture")
        return self


class TurnProblemClusterRevisionCandidate(ContractModel):
    cluster_ref: FoundationEntityRef
    title: ShortText
    problem_refs: Annotated[tuple[EntityRef, ...], Field(min_length=2, max_length=20)]
    coupling_type: ClusterCouplingType
    coupling_reason: Statement
    suggested_confirmation_mode: SuggestedConfirmationMode


class TurnQuestionRevisionCandidate(ContractModel):
    question_ref: FoundationEntityRef
    text: Statement
    rationale: Statement
    question_shape: QuestionShape
    capture_policy: CapturePolicy
    answer_options: Annotated[tuple[ShortText, ...], Field(max_length=20)]
    addresses_problem_refs: Annotated[tuple[EntityRef, ...], Field(min_length=1, max_length=20)]
    prerequisite_problem_refs: Annotated[tuple[EntityRef, ...], Field(max_length=20)]
    safe_without_current_turn_interpretation: bool

    @model_validator(mode="after")
    def valid_question_shape(self) -> TurnQuestionRevisionCandidate:
        if self.question_shape is QuestionShape.CLOSED_ENUM:
            if len(self.answer_options) < 2:
                raise ValueError("CLOSED_ENUM requires at least two answer options")
        elif self.answer_options:
            raise ValueError("answer_options are allowed only for CLOSED_ENUM")
        if self.question_shape is QuestionShape.OPEN_TEXT and self.capture_policy is CapturePolicy.LOW_RISK_FACT:
            raise ValueError("OPEN_TEXT cannot use LOW_RISK_FACT capture")
        return self


class DecisionCandidate(ContractModel):
    candidate_key: CandidateKey
    existing_decision_ref: FoundationEntityRef | None
    classification: Domain
    statement: Statement
    rationale: Statement
    alternatives_considered: Annotated[tuple[Statement, ...], Field(max_length=20)]
    problem_links: Annotated[tuple[ProblemResolutionLink, ...], Field(min_length=1, max_length=25)]
    evidence_refs: Annotated[tuple[EntityRef, ...], Field(min_length=1, max_length=100)]
    requires_human_confirmation: Literal[True]


class ProblemResolutionAssessment(ContractModel):
    problem_ref: EntityRef
    assessment: Literal["RESOLVED", "PARTIALLY_RESOLVED", "UNRESOLVED", "CONTRADICTED"]
    explanation: Statement


class EvidenceAssessment(StrEnum):
    SUPPORTS = "SUPPORTS"
    SUGGESTS = "SUGGESTS"
    CONTRADICTS = "CONTRADICTS"
    INSUFFICIENT = "INSUFFICIENT"
    AMBIGUOUS = "AMBIGUOUS"


class SemanticEvidenceFindingCandidate(ContractModel):
    candidate_key: CandidateKey
    claim_ref: EntityRef
    evidence_ref: EntityRef
    assessment: EvidenceAssessment
    confidence: Probability
    explanation: Statement


class TurnDisposition(StrEnum):
    NO_SEMANTIC_CHANGE = "NO_SEMANTIC_CHANGE"
    SUBSTANTIVE = "SUBSTANTIVE"


class TurnAnalysisCandidate(ContractModel):
    protocol_version: Literal["1.0.0"]
    output_type: Literal["TURN_ANALYSIS_CANDIDATE"]
    analyzer_run_id: UUID
    context_id: UUID
    request_hash: Sha256
    source_set_hash: Sha256
    transcript_event_id: UUID
    based_on_case_revision: NonNegativeInt
    disposition: TurnDisposition
    no_change_reason_code: Literal["SOCIAL_ONLY", "REPETITION", "NON_FINAL", "OUT_OF_SCOPE"] | None
    evidence_candidates: Annotated[tuple[EvidenceCandidate, ...], Field(max_length=200)]
    new_problems: Annotated[tuple[TurnProblemCandidate, ...], Field(max_length=100)]
    new_problem_clusters: Annotated[tuple[TurnProblemClusterCandidate, ...], Field(max_length=50)]
    revised_problem_clusters: Annotated[
        tuple[TurnProblemClusterRevisionCandidate, ...], Field(max_length=50)
    ]
    new_questions: Annotated[tuple[TurnQuestionCandidate, ...], Field(max_length=100)]
    revised_questions: Annotated[tuple[TurnQuestionRevisionCandidate, ...], Field(max_length=100)]
    low_risk_facts: Annotated[tuple[LowRiskFactCandidate, ...], Field(max_length=50)]
    decisions: Annotated[tuple[DecisionCandidate, ...], Field(max_length=50)]
    problem_assessments: Annotated[tuple[ProblemResolutionAssessment, ...], Field(max_length=100)]
    evidence_findings: Annotated[tuple[SemanticEvidenceFindingCandidate, ...], Field(max_length=200)]

    @model_validator(mode="after")
    def valid_disposition_and_keys(self) -> TurnAnalysisCandidate:
        semantic_groups = (
            self.evidence_candidates,
            self.new_problems,
            self.new_problem_clusters,
            self.revised_problem_clusters,
            self.new_questions,
            self.revised_questions,
            self.low_risk_facts,
            self.decisions,
            self.problem_assessments,
            self.evidence_findings,
        )
        has_semantics = any(semantic_groups)
        if self.disposition is TurnDisposition.NO_SEMANTIC_CHANGE:
            if has_semantics or self.no_change_reason_code is None:
                raise ValueError("NO_SEMANTIC_CHANGE requires only a reason code")
        elif not has_semantics or self.no_change_reason_code is not None:
            raise ValueError("SUBSTANTIVE requires semantic content and no no-change code")
        keyed_groups = (
            self.evidence_candidates,
            self.new_problems,
            self.new_problem_clusters,
            self.new_questions,
            self.low_risk_facts,
            self.decisions,
            self.evidence_findings,
        )
        keys = [item.candidate_key for group in keyed_groups for item in group]
        if len(keys) != len(set(keys)):
            raise ValueError("candidate keys must be unique within one turn analysis")

        kind_by_key = {
            **{item.candidate_key: "EVIDENCE" for item in self.evidence_candidates},
            **{item.candidate_key: "PROBLEM" for item in self.new_problems},
            **{item.candidate_key: "CLUSTER" for item in self.new_problem_clusters},
            **{item.candidate_key: "QUESTION" for item in self.new_questions},
            **{item.candidate_key: "FACT" for item in self.low_risk_facts},
            **{item.candidate_key: "DECISION" for item in self.decisions},
            **{item.candidate_key: "FINDING" for item in self.evidence_findings},
        }

        def require_candidate_kind(ref: EntityRef, expected: set[str]) -> None:
            if isinstance(ref, CandidateEntityRef) and kind_by_key.get(ref.candidate_key) not in expected:
                raise ValueError("candidate reference is missing or has the wrong entity kind")

        for item in self.new_problems:
            for ref in item.evidence_refs:
                require_candidate_kind(ref, {"EVIDENCE"})
        for item in self.new_problem_clusters:
            for ref in item.problem_refs:
                require_candidate_kind(ref, {"PROBLEM"})
        for item in self.revised_problem_clusters:
            for ref in item.problem_refs:
                require_candidate_kind(ref, {"PROBLEM"})
        for item in self.new_questions:
            for ref in (*item.addresses_problem_refs, *item.prerequisite_problem_refs):
                require_candidate_kind(ref, {"PROBLEM"})
        for item in self.revised_questions:
            for ref in (*item.addresses_problem_refs, *item.prerequisite_problem_refs):
                require_candidate_kind(ref, {"PROBLEM"})
        for item in self.low_risk_facts:
            require_candidate_kind(item.source_question_ref, {"QUESTION"})
        for item in self.decisions:
            for link in item.problem_links:
                require_candidate_kind(link.problem_ref, {"PROBLEM"})
            for ref in item.evidence_refs:
                require_candidate_kind(ref, {"EVIDENCE"})
        for item in self.problem_assessments:
            require_candidate_kind(item.problem_ref, {"PROBLEM"})
        for item in self.evidence_findings:
            require_candidate_kind(item.claim_ref, {"PROBLEM", "QUESTION", "FACT", "DECISION"})
            require_candidate_kind(item.evidence_ref, {"EVIDENCE"})
        revised_cluster_ids = [item.cluster_ref.foundation_id for item in self.revised_problem_clusters]
        revised_question_ids = [item.question_ref.foundation_id for item in self.revised_questions]
        if len(revised_cluster_ids) != len(set(revised_cluster_ids)):
            raise ValueError("a problem cluster may be revised at most once per analysis")
        if len(revised_question_ids) != len(set(revised_question_ids)):
            raise ValueError("a question may be revised at most once per analysis")
        return self


class GuidanceDependencyKind(StrEnum):
    SOURCE_SET = "SOURCE_SET"
    FOUNDATION_ENTITY = "FOUNDATION_ENTITY"
    QUESTION = "QUESTION"
    PROBLEM = "PROBLEM"


class GuidanceDependency(ContractModel):
    dependency_kind: GuidanceDependencyKind
    entity_ref: FoundationEntityRef | None

    @model_validator(mode="after")
    def valid_dependency(self) -> GuidanceDependency:
        requires_entity = self.dependency_kind is not GuidanceDependencyKind.SOURCE_SET
        if requires_entity != (self.entity_ref is not None):
            raise ValueError("only SOURCE_SET omits entity_ref")
        return self


class GuidanceQuestion(ContractModel):
    question_ref: FoundationEntityRef
    exact_text: Statement
    reason: Statement


class GuidanceCandidate(ContractModel):
    protocol_version: Literal["1.0.0"]
    output_type: Literal["GUIDANCE_CANDIDATE"]
    analyzer_run_id: UUID
    context_id: UUID
    request_hash: Sha256
    source_set_hash: Sha256
    based_on_case_revision: NonNegativeInt
    recommended_question: GuidanceQuestion
    safe_alternates: Annotated[tuple[GuidanceQuestion, ...], Field(max_length=5)]
    do_not_ask_question_refs: Annotated[tuple[FoundationEntityRef, ...], Field(max_length=50)]
    dependencies: Annotated[tuple[GuidanceDependency, ...], Field(min_length=1, max_length=100)]
    acknowledgement_suggestion: Statement

    @model_validator(mode="after")
    def unique_questions(self) -> GuidanceCandidate:
        safe_ids = [
            self.recommended_question.question_ref.foundation_id,
            *(item.question_ref.foundation_id for item in self.safe_alternates),
        ]
        blocked_ids = [item.foundation_id for item in self.do_not_ask_question_refs]
        if len(safe_ids) != len(set(safe_ids)) or len(blocked_ids) != len(set(blocked_ids)):
            raise ValueError("guidance question references must be unique")
        if set(safe_ids).intersection(blocked_ids):
            raise ValueError("safe and do-not-ask questions must be disjoint")
        return self


class GuidanceInvalidationTrigger(StrEnum):
    SOURCE_SET_CHANGED = "SOURCE_SET_CHANGED"
    FOUNDATION_ENTITY_CHANGED = "FOUNDATION_ENTITY_CHANGED"
    QUESTION_ANSWERED = "QUESTION_ANSWERED"
    PROBLEM_RESOLVED = "PROBLEM_RESOLVED"
    GUIDANCE_SUPERSEDED = "GUIDANCE_SUPERSEDED"
    WORKSHOP_CLOSED = "WORKSHOP_CLOSED"


class AdmittedGuidanceQuestion(ContractModel):
    question_id: UUID
    question_version: PositiveInt
    exact_text: Statement
    reason: Statement


class AdmittedGuidanceDependency(ContractModel):
    dependency_kind: GuidanceDependencyKind
    entity_id: UUID | None
    expected_version: PositiveInt | None
    source_set_hash: Sha256 | None

    @model_validator(mode="after")
    def valid_dependency(self) -> AdmittedGuidanceDependency:
        source_dependency = self.dependency_kind is GuidanceDependencyKind.SOURCE_SET
        if source_dependency:
            if self.source_set_hash is None or self.entity_id is not None or self.expected_version is not None:
                raise ValueError("SOURCE_SET dependency requires only source_set_hash")
        elif self.source_set_hash is not None or self.entity_id is None or self.expected_version is None:
            raise ValueError("entity dependency requires ID and expected version")
        return self


class AdmittedGuidance(ContractModel):
    guidance_id: UUID
    guidance_version: PositiveInt
    source_analyzer_run_id: UUID
    source_context_id: UUID
    source_request_hash: Sha256
    based_on_case_revision: NonNegativeInt
    source_set_hash: Sha256
    recommended_question: AdmittedGuidanceQuestion
    safe_alternates: Annotated[tuple[AdmittedGuidanceQuestion, ...], Field(max_length=5)]
    do_not_ask_questions: Annotated[tuple[FoundationEntityRef, ...], Field(max_length=50)]
    dependencies: Annotated[tuple[AdmittedGuidanceDependency, ...], Field(min_length=1, max_length=100)]
    acknowledgement_suggestion: Statement
    invalidation_triggers: Annotated[
        tuple[GuidanceInvalidationTrigger, ...], Field(min_length=1, max_length=6)
    ]
    admitted_at: datetime


class AdmittedSemanticEvidenceFinding(ContractModel):
    finding_id: UUID
    finding_version: PositiveInt
    claim_id: UUID
    claim_version: PositiveInt
    claim_hash: Sha256
    evidence_id: UUID
    evidence_version: PositiveInt
    source_hash: Sha256
    excerpt_hash: Sha256
    assessment: EvidenceAssessment
    confidence: Probability
    source_analyzer_run_id: UUID
    analyzer_contract: AnalyzerContractBinding
    admitted_at: datetime


class SemanticRecordStatus(StrEnum):
    OPEN = "OPEN"
    PENDING_CONFIRMATION = "PENDING_CONFIRMATION"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"
    DEFERRED = "DEFERRED"
    RESOLVED = "RESOLVED"
    STALE = "STALE"


class FoundationEvidenceSnapshot(ContractModel):
    evidence_id: UUID
    evidence_version: PositiveInt
    source_role: SourceRole
    source_hash: Sha256
    locator: SourceLocator
    exact_excerpt: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=8000)]
    excerpt_hash: Sha256
    relevance_claim: Statement


class FoundationProblemSnapshot(ContractModel):
    problem_id: UUID
    problem_version: PositiveInt
    status: SemanticRecordStatus
    problem_kind: ProblemKind
    domain: Domain
    severity: Severity
    statement: Statement
    consequence: Statement
    evidence_ids: Annotated[tuple[UUID, ...], Field(min_length=1, max_length=25)]


class FoundationQuestionSnapshot(ContractModel):
    question_id: UUID
    question_version: PositiveInt
    status: SemanticRecordStatus
    text: Statement
    rationale: Statement
    question_shape: QuestionShape
    capture_policy: CapturePolicy
    answer_options: Annotated[tuple[ShortText, ...], Field(max_length=20)]
    addresses_problem_ids: Annotated[tuple[UUID, ...], Field(min_length=1, max_length=20)]
    prerequisite_problem_ids: Annotated[tuple[UUID, ...], Field(max_length=20)]


class FoundationFactSnapshot(ContractModel):
    fact_id: UUID
    fact_version: PositiveInt
    status: SemanticRecordStatus
    source_question_id: UUID
    value_type: NormalizedScalarType
    normalized_value: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=4000)]
    source_transcript_event_id: UUID


class FoundationDecisionSnapshot(ContractModel):
    decision_id: UUID
    decision_version: PositiveInt
    status: SemanticRecordStatus
    classification: Domain
    statement: Statement
    rationale: Statement
    alternatives_considered: Annotated[tuple[Statement, ...], Field(max_length=20)]
    problem_ids: Annotated[tuple[UUID, ...], Field(min_length=1, max_length=25)]
    evidence_ids: Annotated[tuple[UUID, ...], Field(min_length=1, max_length=100)]


class FoundationRevisionRequestSnapshot(ContractModel):
    revision_request_id: UUID
    revision_request_version: PositiveInt
    pending_decision_id: UUID
    source_transcript_span: TranscriptSpan
    status: Literal["OPEN", "RESOLVED", "SUPERSEDED"]


class FoundationSemanticSnapshot(ContractModel):
    case_revision: NonNegativeInt
    source_set_hash: Sha256
    readiness: Readiness
    review_obligation: ReviewObligation
    evidence: Annotated[tuple[FoundationEvidenceSnapshot, ...], Field(max_length=1000)]
    problems: Annotated[tuple[FoundationProblemSnapshot, ...], Field(max_length=1000)]
    questions: Annotated[tuple[FoundationQuestionSnapshot, ...], Field(max_length=1000)]
    facts: Annotated[tuple[FoundationFactSnapshot, ...], Field(max_length=1000)]
    decisions: Annotated[tuple[FoundationDecisionSnapshot, ...], Field(max_length=1000)]
    evidence_findings: Annotated[tuple[AdmittedSemanticEvidenceFinding, ...], Field(max_length=2000)]
    revision_requests: Annotated[tuple[FoundationRevisionRequestSnapshot, ...], Field(max_length=500)]


class AnalyzerRequestEnvelope(ContractModel):
    protocol_version: Literal["1.0.0"]
    request_type: AnalyzerOperation
    client_request_id: ProviderIdentifier
    request_hash: Sha256
    analyzer_run_id: UUID
    context_id: UUID
    provider_conversation_id: ProviderIdentifier
    source_set_hash: Sha256
    analyzer_contract: AnalyzerContractBinding
    based_on_case_revision: NonNegativeInt


class BootstrapAnalyzerRequest(AnalyzerRequestEnvelope):
    request_type: Literal[AnalyzerOperation.BOOTSTRAP]
    source_set: SourceSetBinding
    requested_output: Literal["INTERVIEW_BRIEF_CANDIDATE"]

    @model_validator(mode="after")
    def matching_source_set(self) -> BootstrapAnalyzerRequest:
        if self.source_set_hash != self.source_set.source_set_hash:
            raise ValueError("request and source-set hashes must match")
        return self


class FinalizedTranscriptInput(ContractModel):
    transcript_event_id: UUID
    transcript_hash: Sha256
    speaker_actor_id: UUID | None
    actor: TranscriptActor
    sequence_number: PositiveInt
    text: Statement


class PriorTranscriptBinding(ContractModel):
    transcript_event_id: UUID
    transcript_hash: Sha256
    sequence_number: PositiveInt


class AnalyzeFinalTurnRequest(AnalyzerRequestEnvelope):
    request_type: Literal[AnalyzerOperation.TURN_ANALYSIS]
    transcript: FinalizedTranscriptInput
    prior_transcript: PriorTranscriptBinding | None
    foundation_snapshot: FoundationSemanticSnapshot
    requested_output: Literal["TURN_ANALYSIS_CANDIDATE"]

    @model_validator(mode="after")
    def matching_snapshot(self) -> AnalyzeFinalTurnRequest:
        if self.based_on_case_revision != self.foundation_snapshot.case_revision:
            raise ValueError("request and snapshot case revisions must match")
        if self.source_set_hash != self.foundation_snapshot.source_set_hash:
            raise ValueError("request and snapshot source-set hashes must match")
        if self.transcript.sequence_number == 1:
            if self.prior_transcript is not None:
                raise ValueError("first finalized transcript has no prior transcript")
        elif self.prior_transcript is None or (
            self.prior_transcript.sequence_number + 1 != self.transcript.sequence_number
        ):
            raise ValueError("finalized transcript must extend the exact prior transcript sequence")
        return self


class RunwayStateSnapshot(ContractModel):
    active_question_refs: Annotated[tuple[FoundationEntityRef, ...], Field(max_length=6)]
    asked_question_refs: Annotated[tuple[FoundationEntityRef, ...], Field(max_length=1000)]
    desired_safe_depth: Annotated[int, Field(strict=True, ge=1, le=5)]


class ReplenishGuidanceRequest(AnalyzerRequestEnvelope):
    request_type: Literal[AnalyzerOperation.GUIDANCE]
    foundation_snapshot: FoundationSemanticSnapshot
    runway_state: RunwayStateSnapshot
    requested_output: Literal["GUIDANCE_CANDIDATE"]

    @model_validator(mode="after")
    def matching_snapshot(self) -> ReplenishGuidanceRequest:
        if self.based_on_case_revision != self.foundation_snapshot.case_revision:
            raise ValueError("request and snapshot case revisions must match")
        if self.source_set_hash != self.foundation_snapshot.source_set_hash:
            raise ValueError("request and snapshot source-set hashes must match")
        return self


class ArtifactDraftTarget(ContractModel):
    artifact_type: Literal["SPEC_PACKAGE", "TECHNICAL_CONTRACT"]
    foundation_artifact_id: UUID
    artifact_key: Annotated[str, StringConstraints(strict=True, pattern=r"^(?:SPEC|CONTRACT)-[A-Z0-9][A-Z0-9-]{2,63}$")]
    next_artifact_version: PositiveInt

    @model_validator(mode="after")
    def matching_artifact_key(self) -> ArtifactDraftTarget:
        prefix = "SPEC-" if self.artifact_type == "SPEC_PACKAGE" else "CONTRACT-"
        if not self.artifact_key.startswith(prefix):
            raise ValueError("artifact type and artifact key prefix must match")
        return self


class PlannedArtifactIdentity(ContractModel):
    foundation_id: UUID
    foundation_version: PositiveInt
    entity_kind: Annotated[
        str,
        StringConstraints(strict=True, pattern=r"^[A-Z][A-Z0-9_]{1,63}$"),
    ]


ArtifactIdentitySlotKey = Annotated[
    str,
    StringConstraints(
        strict=True,
        pattern=r"^[A-Z][A-Z0-9_]{1,63}:[0-9]{4}$",
        max_length=69,
    ),
]


class ArtifactIdentitySlot(ContractModel):
    slot_key: ArtifactIdentitySlotKey
    entity_kind: Annotated[
        str,
        StringConstraints(strict=True, pattern=r"^[A-Z][A-Z0-9_]{1,63}$"),
    ]
    ordinal: PositiveInt
    owner: Literal["ANALYZER", "FOUNDATION"]
    foundation_id: UUID
    allocation_mode: Literal["NEW_ENTITY", "BOUND_EXISTING"]


class ArtifactSynthesisIdentityPlan(ContractModel):
    identity_plan_id: UUID
    identity_plan_version: PositiveInt
    target: ArtifactDraftTarget
    based_on_case_revision: NonNegativeInt
    semantic_state_hash: Sha256
    source_entity_refs: Annotated[
        tuple[FoundationEntityRef, ...], Field(max_length=100)
    ]
    planned_identities: Annotated[tuple[PlannedArtifactIdentity, ...], Field(min_length=1, max_length=10_000)]
    slots: Annotated[tuple[ArtifactIdentitySlot, ...], Field(max_length=10_000)] = ()

    @model_validator(mode="after")
    def unique_identities(self) -> ArtifactSynthesisIdentityPlan:
        identities = [item.foundation_id for item in self.planned_identities]
        if len(identities) != len(set(identities)):
            raise ValueError("planned Foundation identities must be unique")
        if not self.slots:
            return self
        slot_keys = [item.slot_key for item in self.slots]
        ordinals = [(item.entity_kind, item.ordinal) for item in self.slots]
        slot_identities = [item.foundation_id for item in self.slots]
        if len(slot_keys) != len(set(slot_keys)):
            raise ValueError("identity-plan slot keys must be unique")
        if len(ordinals) != len(set(ordinals)):
            raise ValueError("identity-plan kind ordinals must be unique")
        if len(slot_identities) != len(set(slot_identities)):
            raise ValueError("identity-plan slots must bind unique Foundation identities")
        planned = {
            (item.foundation_id, item.entity_kind) for item in self.planned_identities
        }
        slotted = {(item.foundation_id, item.entity_kind) for item in self.slots}
        if planned != slotted:
            raise ValueError("identity-plan slots must exactly cover planned identities")
        if any(
            item.allocation_mode == "BOUND_EXISTING" and item.owner != "FOUNDATION"
            for item in self.slots
        ):
            raise ValueError("bound-existing slots must be Foundation-owned")
        return self


class ArtifactConstructionSlot(ContractModel):
    slot_key: ArtifactIdentitySlotKey
    foundation_id: UUID
    foundation_version: PositiveInt
    entity_kind: Annotated[
        str,
        StringConstraints(strict=True, pattern=r"^[A-Z][A-Z0-9_]{1,63}$"),
    ]
    ordinal: PositiveInt
    owner: Literal["ANALYZER", "FOUNDATION"]
    allocation_mode: Literal["NEW_ENTITY", "BOUND_EXISTING"]
    purpose: Statement


class ArtifactConstructionBlueprint(ContractModel):
    blueprint_version: Literal["1.0.0"]
    slots: Annotated[
        tuple[ArtifactConstructionSlot, ...], Field(min_length=1, max_length=10_000)
    ]

    @model_validator(mode="after")
    def unique_slots(self) -> ArtifactConstructionBlueprint:
        identities = [(item.foundation_id, item.foundation_version) for item in self.slots]
        if len(identities) != len(set(identities)):
            raise ValueError("construction slots must bind unique Foundation identities")
        slot_keys = [item.slot_key for item in self.slots]
        if len(slot_keys) != len(set(slot_keys)):
            raise ValueError("construction slot keys must be unique")
        return self


class ConfirmedDecisionSynthesisBinding(ContractModel):
    """Exact Foundation ceremony needed to project one confirmed decision."""

    decision_id: UUID
    decision_version: PositiveInt
    classification: Domain
    statement: Statement
    rationale: Statement
    alternatives_considered: Annotated[tuple[Statement, ...], Field(max_length=20)]
    problem_ids: Annotated[tuple[UUID, ...], Field(min_length=1, max_length=25)]
    evidence_ids: Annotated[tuple[UUID, ...], Field(min_length=1, max_length=100)]
    confirmation_id: UUID
    decision_batch_view_id: UUID
    decision_batch_view_hash: Sha256
    review_item_id: UUID
    confirmed_case_revision: PositiveInt
    actor_ref: UUID
    authority_validation_id: UUID
    transcript_event_id: UUID
    confirmed_at: datetime


class ArtifactSynthesisQualityRule(ContractModel):
    """Exact quality-contract rule supplied as a construction constraint."""

    rule_id: Annotated[
        str, StringConstraints(strict=True, pattern=r"^(?:SPEC|TECH)-Q-[0-9]{3}$")
    ]
    name: Statement
    dimension: ShortText
    check_types: Annotated[
        tuple[Literal["structural", "referential", "semantic", "authority", "human"], ...],
        Field(min_length=1, max_length=5),
    ]
    gate: Literal["block_confirmation", "block_handoff"]
    primary_evaluator: Literal["analyzer", "foundation"]
    requirement: Statement
    pass_condition: Statement
    evidence_of_pass: Statement
    failure_effect: Literal[
        "NEEDS_CLARIFICATION", "BLOCKED", "CONDITIONAL", "REJECT_MUTATION"
    ]


TECHNICAL_CLOSURE_RULE_LAYOUT = (
    (
        "ARCHITECTURE_CLOSURE",
        "ARCHITECTURE",
        "Represent every component and substrate dependency exactly once as an architecture node, and define every interaction purpose, data or signal, and trust-boundary crossing.",
    ),
    (
        "RESPONSIBILITY_CLOSURE",
        "RESPONSIBILITY",
        "Assign every technical responsibility to exactly one owned component and connect build units, interfaces, data contracts, failures, and dependencies to those components.",
    ),
    (
        "INTERFACE_CLOSURE",
        "INTERFACE",
        "For every independently executable operation define producer, consumers, input, output, preconditions, postconditions, errors, authorization, idempotency, timeout, and versioning.",
    ),
    (
        "DATA_CLOSURE",
        "DATA",
        "Map every material Spec data rule to field-level source, representation, null and invalid behavior, classification, retention, consistency, and temporal or numeric boundaries.",
    ),
    (
        "WORKFLOW_CLOSURE",
        "WORKFLOW",
        "For every workflow define exactly one initial state, transitions for every nonterminal state, failure behavior, concurrency behavior, and invalid-transition behavior.",
    ),
    (
        "DELIVERY_GOVERNANCE_CLOSURE",
        "DELIVERY_GOVERNANCE",
        "Assign rollout ownership; materialize every Ask First obligation as one owned approval interface, durable receipt, lifecycle, fail-closed control, audit record, verification, and exact trace; keep only concrete evidence-backed choices in engineering decisions and place unresolved choices in review obligations with an owner, required evidence, and downstream effect.",
    ),
)

TECHNICAL_CLOSURE_OBLIGATION_PATHS = (
    ("requirements", "requirement"),
    ("data_rules", "data_rule"),
    ("acceptance_checks", "acceptance_check"),
    ("quality_attributes", "quality_attribute"),
)


def technical_closure_obligations_from_spec_payload(
    payload: dict[str, Any],
) -> tuple[tuple[str, UUID, str], ...]:
    """Return the exact ordered Spec identities that Technical must cover."""

    result: list[tuple[str, UUID, str]] = []
    for collection, source_kind in TECHNICAL_CLOSURE_OBLIGATION_PATHS:
        values = payload.get(collection, [])
        if not isinstance(values, list):
            raise ValueError("confirmed Spec closure collection is invalid")
        for index, item in enumerate(values):
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                raise ValueError("confirmed Spec closure item has no identity")
            result.append((source_kind, UUID(item["id"]), f"/{collection}/{index}"))
    behaviour = payload.get("behaviour_contract", {})
    if not isinstance(behaviour, dict):
        raise ValueError("confirmed Spec behavior contract is invalid")
    for collection in ("always", "ask_first", "never"):
        values = behaviour.get(collection, [])
        if not isinstance(values, list):
            raise ValueError("confirmed Spec behavior collection is invalid")
        for index, item in enumerate(values):
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                raise ValueError("confirmed Spec behavior item has no identity")
            result.append(
                (
                    "behaviour_rule",
                    UUID(item["id"]),
                    f"/behaviour_contract/{collection}/{index}",
                )
            )
    return tuple(result)


class TechnicalClosureRule(ContractModel):
    rule_key: Annotated[
        str,
        StringConstraints(strict=True, pattern=r"^[A-Z][A-Z0-9_]{1,63}$"),
    ]
    section: Literal[
        "ARCHITECTURE",
        "RESPONSIBILITY",
        "INTERFACE",
        "DATA",
        "WORKFLOW",
        "DELIVERY_GOVERNANCE",
    ]
    requirement: Statement


class TechnicalClosureObligation(ContractModel):
    source_kind: Literal[
        "requirement",
        "data_rule",
        "acceptance_check",
        "quality_attribute",
        "behaviour_rule",
    ]
    source_ref: UUID
    source_pointer: Annotated[
        str,
        StringConstraints(strict=True, pattern=r"^/(?:[a-z][a-z0-9_]*|[0-9]+)(?:/(?:[a-z][a-z0-9_]*|[0-9]+))*$"),
    ]


class TechnicalClosureManifest(ContractModel):
    manifest_version: Literal["1.0.0"]
    rules: Annotated[tuple[TechnicalClosureRule, ...], Field(min_length=6, max_length=6)]
    obligations: Annotated[
        tuple[TechnicalClosureObligation, ...], Field(min_length=1, max_length=1_000)
    ]

    @model_validator(mode="after")
    def exact_rules_and_unique_obligations(self) -> TechnicalClosureManifest:
        supplied_rules = tuple(
            (item.rule_key, item.section, item.requirement) for item in self.rules
        )
        if supplied_rules != TECHNICAL_CLOSURE_RULE_LAYOUT:
            raise ValueError("Technical closure manifest rules changed")
        identities = [item.source_ref for item in self.obligations]
        pointers = [item.source_pointer for item in self.obligations]
        if len(identities) != len(set(identities)) or len(pointers) != len(set(pointers)):
            raise ValueError("Technical closure obligations must be unique")
        return self


class ConfirmedSpecSynthesisBinding(ContractModel):
    foundation_artifact_id: UUID
    artifact_key: Annotated[str, StringConstraints(strict=True, pattern=r"^SPEC-[A-Z0-9][A-Z0-9-]{2,63}$")]
    artifact_version: PositiveInt
    record_revision: PositiveInt
    confirmed_case_revision: PositiveInt
    payload_hash: Sha256
    confirmation_id: UUID
    canonical_payload_json: JsonObjectText

    @field_validator("canonical_payload_json")
    @classmethod
    def valid_canonical_payload(cls, value: str) -> str:
        return validate_json_object_text(value, "confirmed Spec payload")


class SpecPackageSynthesisRequest(AnalyzerRequestEnvelope):
    request_type: Literal[AnalyzerOperation.SPEC_PACKAGE_SYNTHESIS]
    target: ArtifactDraftTarget
    identity_plan: ArtifactSynthesisIdentityPlan
    construction_blueprint: ArtifactConstructionBlueprint
    foundation_snapshot: FoundationSemanticSnapshot
    quality_rule_manifest: Annotated[
        tuple[ArtifactSynthesisQualityRule, ...], Field(min_length=26, max_length=26)
    ]
    confirmed_decision_bindings: Annotated[
        tuple[ConfirmedDecisionSynthesisBinding, ...], Field(max_length=26)
    ]
    payload_schema_id: Literal["spec-package-payload"]
    payload_schema_version: Literal["4.0.2"]
    requested_output: Literal["SPEC_PACKAGE_SYNTHESIS_CANDIDATE"]

    @model_validator(mode="after")
    def matching_snapshot_and_target(self) -> SpecPackageSynthesisRequest:
        if self.target.artifact_type != "SPEC_PACKAGE":
            raise ValueError("Spec synthesis requires a Spec Package target")
        if self.target != self.identity_plan.target:
            raise ValueError("request target and identity plan target must match")
        if self.based_on_case_revision != self.identity_plan.based_on_case_revision:
            raise ValueError("request and identity plan case revisions must match")
        if self.based_on_case_revision != self.foundation_snapshot.case_revision:
            raise ValueError("request and snapshot case revisions must match")
        if self.source_set_hash != self.foundation_snapshot.source_set_hash:
            raise ValueError("request and snapshot source-set hashes must match")
        expected_rules = tuple(f"SPEC-Q-{index:03d}" for index in range(1, 27))
        if tuple(item.rule_id for item in self.quality_rule_manifest) != expected_rules:
            raise ValueError("Spec synthesis requires the exact ordered 26-rule manifest")
        snapshot_decisions = {
            (item.decision_id, item.decision_version): item
            for item in self.foundation_snapshot.decisions
            if item.status is SemanticRecordStatus.CONFIRMED
        }
        supplied = {
            (item.decision_id, item.decision_version): item
            for item in self.confirmed_decision_bindings
        }
        if set(supplied) != set(snapshot_decisions):
            raise ValueError("Spec synthesis must bind every confirmed Foundation decision")
        if any(
            supplied[key].statement != snapshot_decisions[key].statement
            or supplied[key].problem_ids != snapshot_decisions[key].problem_ids
            or supplied[key].evidence_ids != snapshot_decisions[key].evidence_ids
            for key in supplied
        ):
            raise ValueError("confirmed decision synthesis binding changed semantic content")
        planned = {
            (item.foundation_id, item.foundation_version): item.entity_kind
            for item in self.identity_plan.planned_identities
        }
        blueprint = {
            (item.foundation_id, item.foundation_version): item.entity_kind
            for item in self.construction_blueprint.slots
        }
        if blueprint != planned:
            raise ValueError("construction blueprint must bind every planned identity exactly")
        blueprint_owners = {
            (item.foundation_id, item.foundation_version): item.owner
            for item in self.construction_blueprint.slots
        }
        if any(planned.get(key) != "DECISION" for key in supplied):
            raise ValueError("every confirmed decision must retain its canonical planned identity")
        if any(blueprint_owners.get(key) != "FOUNDATION" for key in supplied):
            raise ValueError("confirmed decisions must be Foundation-owned construction slots")
        actor_refs = {item.actor_ref for item in self.confirmed_decision_bindings}
        if any(planned.get((actor_ref, 1)) != "ACTOR" for actor_ref in actor_refs):
            raise ValueError("every confirming actor must retain its canonical planned identity")
        if any(blueprint_owners.get((actor_ref, 1)) != "FOUNDATION" for actor_ref in actor_refs):
            raise ValueError("confirming actors must be Foundation-owned construction slots")
        return self


class TechnicalContractSynthesisRequest(AnalyzerRequestEnvelope):
    request_type: Literal[AnalyzerOperation.TECHNICAL_CONTRACT_SYNTHESIS]
    target: ArtifactDraftTarget
    identity_plan: ArtifactSynthesisIdentityPlan
    construction_blueprint: ArtifactConstructionBlueprint
    technical_closure_manifest: TechnicalClosureManifest
    confirmed_spec: ConfirmedSpecSynthesisBinding
    foundation_snapshot: FoundationSemanticSnapshot
    payload_schema_id: Literal["technical-contract-payload"]
    payload_schema_version: Literal["4.0.0"]
    requested_output: Literal["TECHNICAL_CONTRACT_SYNTHESIS_CANDIDATE"]

    @model_validator(mode="after")
    def matching_snapshot_and_target(self) -> TechnicalContractSynthesisRequest:
        if self.target.artifact_type != "TECHNICAL_CONTRACT":
            raise ValueError("Technical synthesis requires a Technical Contract target")
        if self.target != self.identity_plan.target:
            raise ValueError("request target and identity plan target must match")
        if self.based_on_case_revision != self.identity_plan.based_on_case_revision:
            raise ValueError("request and identity plan case revisions must match")
        if self.based_on_case_revision != self.foundation_snapshot.case_revision:
            raise ValueError("request and snapshot case revisions must match")
        if self.source_set_hash != self.foundation_snapshot.source_set_hash:
            raise ValueError("request and snapshot source-set hashes must match")
        if self.based_on_case_revision < self.confirmed_spec.confirmed_case_revision:
            raise ValueError("Technical synthesis cannot precede the confirmed Spec commit")
        planned = {
            (item.foundation_id, item.foundation_version): item.entity_kind
            for item in self.identity_plan.planned_identities
        }
        blueprint = {
            (item.foundation_id, item.foundation_version): item.entity_kind
            for item in self.construction_blueprint.slots
        }
        if blueprint != planned:
            raise ValueError("construction blueprint must bind every planned identity exactly")
        spec_payload = json.loads(self.confirmed_spec.canonical_payload_json)
        expected_obligations = technical_closure_obligations_from_spec_payload(spec_payload)
        supplied_obligations = tuple(
            (item.source_kind, item.source_ref, item.source_pointer)
            for item in self.technical_closure_manifest.obligations
        )
        if supplied_obligations != expected_obligations:
            raise ValueError("Technical closure manifest changed confirmed Spec obligations")
        return self


class ArtifactPayloadSynthesisCandidate(ContractModel):
    analyzer_run_id: UUID
    context_id: UUID
    request_hash: Sha256
    source_set_hash: Sha256
    based_on_case_revision: NonNegativeInt
    foundation_artifact_id: UUID
    identity_plan_id: UUID
    identity_plan_version: PositiveInt
    semantic_state_hash: Sha256
    candidate_payload_json: JsonObjectText

    @field_validator("candidate_payload_json")
    @classmethod
    def valid_json_object(cls, value: str) -> str:
        return validate_json_object_text(value, "candidate payload")


class ArtifactIdentityAssignment(ContractModel):
    slot_key: ArtifactIdentitySlotKey
    entity_kind: Annotated[
        str,
        StringConstraints(strict=True, pattern=r"^[A-Z][A-Z0-9_]{1,63}$"),
    ]
    ordinal: PositiveInt
    foundation_id: UUID
    canonical_pointer: Annotated[
        str,
        StringConstraints(
            strict=True,
            pattern=r"^(?:/(?:[^~/]|~0|~1)*)+$",
            max_length=2_048,
        ),
    ]


class SpecPackageSynthesisCandidate(ArtifactPayloadSynthesisCandidate):
    output_type: Literal["SPEC_PACKAGE_SYNTHESIS_CANDIDATE"]
    payload_schema_id: Literal["spec-package-payload"]
    payload_schema_version: Literal["4.0.2"]
    evidence_support_proposals: Annotated[
        tuple["SpecEvidenceSupportProposalCandidate", ...], Field(max_length=1_000)
    ] = ()
    identity_assignment_map: Annotated[
        tuple[ArtifactIdentityAssignment, ...], Field(max_length=10_000)
    ] = ()


class SpecEvidenceSupportProposalCandidate(ContractModel):
    evidence_ref: UUID | ArtifactIdentitySlotKey
    finding_ref: UUID | ArtifactIdentitySlotKey
    claim_ref: UUID | ArtifactIdentitySlotKey
    claim_pointer: Annotated[
        str,
        StringConstraints(
            strict=True,
            pattern=r"^(?:|(?:/(?:[^~/]|~0|~1)*)+)$",
            max_length=2_048,
        ),
    ]
    source_role: SourceRole
    locator: Statement
    exact_excerpt: Annotated[
        str, StringConstraints(strict=True, min_length=1, max_length=8_000)
    ]


class TechnicalContractSynthesisCandidate(ArtifactPayloadSynthesisCandidate):
    output_type: Literal["TECHNICAL_CONTRACT_SYNTHESIS_CANDIDATE"]
    payload_schema_id: Literal["technical-contract-payload"]
    payload_schema_version: Literal["4.0.0"]


class TranscriptSpan(ContractModel):
    transcript_event_id: UUID
    start_character: NonNegativeInt
    end_character_exclusive: PositiveInt

    @model_validator(mode="after")
    def ordered(self) -> TranscriptSpan:
        if self.start_character >= self.end_character_exclusive:
            raise ValueError("transcript span must be non-empty and ordered")
        return self


class ConfirmationAction(StrEnum):
    CONFIRM = "CONFIRM"
    REVISE = "REVISE"
    REJECT = "REJECT"
    DEFER = "DEFER"


class VoiceConfirmationSelectionItemCandidate(ContractModel):
    handle: QuestionHandle
    action: ConfirmationAction
    revision_span: TranscriptSpan | None

    @model_validator(mode="after")
    def revision_shape(self) -> VoiceConfirmationSelectionItemCandidate:
        if (self.action is ConfirmationAction.REVISE) != (self.revision_span is not None):
            raise ValueError("only REVISE requires a revision transcript span")
        return self


class ConfirmationMappingStatus(StrEnum):
    MAPPED = "MAPPED"
    CLARIFICATION_REQUIRED = "CLARIFICATION_REQUIRED"


class VoiceConfirmationSelectionCandidate(ContractModel):
    protocol_version: Literal["1.0.0"]
    output_type: Literal["VOICE_CONFIRMATION_SELECTION_CANDIDATE"]
    producer: Literal["VOICE"]
    selection_event_id: UUID
    mapping_status: ConfirmationMappingStatus
    decision_batch_view_id: UUID
    decision_batch_view_hash: Sha256
    observed_case_revision: NonNegativeInt
    transcript_event_id: UUID
    speaker_actor_id: UUID
    selections: Annotated[tuple[VoiceConfirmationSelectionItemCandidate, ...], Field(max_length=26)]
    unmentioned_item_policy: Literal["REMAIN_PENDING"]
    clarification_question: Statement | None

    @model_validator(mode="after")
    def valid_mapping(self) -> VoiceConfirmationSelectionCandidate:
        handles = [item.handle for item in self.selections]
        if len(handles) != len(set(handles)):
            raise ValueError("selection handles must be unique")
        if self.mapping_status is ConfirmationMappingStatus.MAPPED:
            if not self.selections or self.clarification_question is not None:
                raise ValueError("MAPPED requires selections and no clarification")
        elif self.selections or self.clarification_question is None:
            raise ValueError("CLARIFICATION_REQUIRED requires only a question")
        return self


class ReviewConsiderationKind(StrEnum):
    IMPACT = "IMPACT"
    RISK = "RISK"
    FUTURE_CONSEQUENCE = "FUTURE_CONSEQUENCE"


class ReviewConsideration(ContractModel):
    consideration_kind: ReviewConsiderationKind
    text: Statement


class ReviewNarrationItemCandidate(ContractModel):
    handle: QuestionHandle
    core_concept: Statement
    material_considerations: Annotated[tuple[ReviewConsideration, ...], Field(max_length=5)]


class ReviewNarrationCandidate(ContractModel):
    protocol_version: Literal["1.0.0"]
    output_type: Literal["REVIEW_NARRATION_CANDIDATE"]
    analyzer_run_id: UUID
    context_id: UUID
    request_hash: Sha256
    source_set_hash: Sha256
    decision_batch_view_id: UUID
    decision_batch_view_hash: Sha256
    based_on_case_revision: NonNegativeInt
    spoken_opening: Statement
    items: Annotated[tuple[ReviewNarrationItemCandidate, ...], Field(min_length=1, max_length=26)]
    spoken_confirmation_question: Statement

    @model_validator(mode="after")
    def unique_handles(self) -> ReviewNarrationCandidate:
        handles = [item.handle for item in self.items]
        if len(handles) != len(set(handles)):
            raise ValueError("narration item handles must be unique")
        return self


class GenerateReviewNarrationRequest(AnalyzerRequestEnvelope):
    request_type: Literal[AnalyzerOperation.REVIEW_NARRATION]
    review_view: DecisionBatchReviewView
    requested_output: Literal["REVIEW_NARRATION_CANDIDATE"]

    @model_validator(mode="after")
    def matching_view(self) -> GenerateReviewNarrationRequest:
        if self.based_on_case_revision != self.review_view.based_on_case_revision:
            raise ValueError("request and review-view case revisions must match")
        return self


class AdmittedReviewNarration(ContractModel):
    narration_id: UUID
    narration_version: PositiveInt
    source_analyzer_run_id: UUID
    source_context_id: UUID
    source_request_hash: Sha256
    source_set_hash: Sha256
    decision_batch_view_id: UUID
    decision_batch_view_hash: Sha256
    based_on_case_revision: NonNegativeInt
    spoken_opening: Statement
    items: Annotated[tuple[ReviewNarrationItemCandidate, ...], Field(min_length=1, max_length=26)]
    spoken_confirmation_question: Statement
    admitted_at: datetime


class SafeValidationCode(StrEnum):
    MISSING = "missing"
    INVALID_TYPE = "invalid_type"
    OUT_OF_RANGE = "out_of_range"
    UNKNOWN_FIELD = "unknown_field"
    WRONG_DISCRIMINATOR = "wrong_discriminator"
    INVARIANT_FAILED = "invariant_failed"
    MALFORMED_JSON = "malformed_json"


class SafeValidationDiagnostic(ContractModel):
    path: Annotated[tuple[SchemaPathSegment, ...], Field(max_length=16)]
    code: SafeValidationCode


class ProviderProcessingStage(StrEnum):
    FILE_UPLOAD = "FILE_UPLOAD"
    CONVERSATION_CREATE = "CONVERSATION_CREATE"
    BOOTSTRAP = "BOOTSTRAP"
    TURN_ANALYSIS = "TURN_ANALYSIS"
    GUIDANCE = "GUIDANCE"
    REVIEW_NARRATION = "REVIEW_NARRATION"
    SPEC_PACKAGE_SYNTHESIS = "SPEC_PACKAGE_SYNTHESIS"
    TECHNICAL_CONTRACT_SYNTHESIS = "TECHNICAL_CONTRACT_SYNTHESIS"
    LOCAL_VALIDATION = "LOCAL_VALIDATION"


class ProviderFailureCode(StrEnum):
    HTTP_401 = "HTTP_401"
    HTTP_403 = "HTTP_403"
    HTTP_404 = "HTTP_404"
    HTTP_409 = "HTTP_409"
    HTTP_429 = "HTTP_429"
    HTTP_5XX = "HTTP_5XX"
    TIMEOUT = "TIMEOUT"
    CONNECTION = "CONNECTION"
    SCHEMA_REJECTED = "SCHEMA_REJECTED"
    INVALID_REQUEST_SHAPE = "INVALID_REQUEST_SHAPE"
    UNSUPPORTED_SCHEMA_KEYWORD = "UNSUPPORTED_SCHEMA_KEYWORD"
    INPUT_FILE_UNAVAILABLE = "INPUT_FILE_UNAVAILABLE"
    CONVERSATION_UNAVAILABLE = "CONVERSATION_UNAVAILABLE"
    CONTEXT_MISMATCH = "CONTEXT_MISMATCH"
    REQUEST_TOO_LARGE = "REQUEST_TOO_LARGE"
    OUTPUT_INVALID = "OUTPUT_INVALID"
    CONTEXT_UNAVAILABLE = "CONTEXT_UNAVAILABLE"
    UNKNOWN_SAFE = "UNKNOWN_SAFE"


class ProviderFailureReceipt(ContractModel):
    provider: Literal[ProviderName.OPENAI]
    stage: ProviderProcessingStage
    client_request_id: ProviderIdentifier
    provider_request_id: ProviderIdentifier | None
    status_code: Annotated[int, Field(strict=True, ge=100, le=599)] | None
    code: ProviderFailureCode
    retryable: bool
    validation_diagnostics: Annotated[tuple[SafeValidationDiagnostic, ...], Field(max_length=50)]
    occurred_at: datetime

    @model_validator(mode="after")
    def safe_classification(self) -> ProviderFailureReceipt:
        exact_status = {
            ProviderFailureCode.HTTP_401: 401,
            ProviderFailureCode.HTTP_403: 403,
            ProviderFailureCode.HTTP_404: 404,
            ProviderFailureCode.HTTP_409: 409,
            ProviderFailureCode.HTTP_429: 429,
        }
        if self.code in exact_status and self.status_code != exact_status[self.code]:
            raise ValueError("HTTP failure code and status must match")
        if self.code is ProviderFailureCode.HTTP_5XX and (
            self.status_code is None or not 500 <= self.status_code <= 599
        ):
            raise ValueError("HTTP_5XX requires a 5xx status")
        classified_400 = {
            ProviderFailureCode.SCHEMA_REJECTED,
            ProviderFailureCode.INVALID_REQUEST_SHAPE,
            ProviderFailureCode.UNSUPPORTED_SCHEMA_KEYWORD,
            ProviderFailureCode.INPUT_FILE_UNAVAILABLE,
            ProviderFailureCode.CONVERSATION_UNAVAILABLE,
            ProviderFailureCode.CONTEXT_MISMATCH,
            ProviderFailureCode.REQUEST_TOO_LARGE,
        }
        if self.code in classified_400 and self.status_code != 400:
            raise ValueError("safe invalid-request categories require HTTP 400")
        if self.code in {ProviderFailureCode.TIMEOUT, ProviderFailureCode.CONNECTION} and self.status_code is not None:
            raise ValueError("transport failures do not carry an HTTP status")
        local_output_diagnostics = (
            self.code is ProviderFailureCode.OUTPUT_INVALID
            and self.stage is ProviderProcessingStage.LOCAL_VALIDATION
        )
        safe_schema_terms = {
            "additional_properties",
            "all_of",
            "any_of",
            "const",
            "contains",
            "defs",
            "dependent_required",
            "dependent_schemas",
            "enum",
            "exclusive_maximum",
            "exclusive_minimum",
            "format",
            "items",
            "max_contains",
            "max_items",
            "max_length",
            "maximum",
            "min_contains",
            "min_items",
            "min_length",
            "minimum",
            "multiple_of",
            "not",
            "one_of",
            "pattern",
            "pattern_properties",
            "property_names",
            "ref",
            "required",
            "type",
            "unevaluated_properties",
            "unique_items",
        }
        provider_schema_diagnostics = (
            self.code
            in {
                ProviderFailureCode.SCHEMA_REJECTED,
                ProviderFailureCode.UNSUPPORTED_SCHEMA_KEYWORD,
            }
            and self.status_code == 400
            and all(
                item.code is SafeValidationCode.INVARIANT_FAILED
                and len(item.path) == 2
                and item.path[0] == "provider_schema"
                and item.path[1] in safe_schema_terms
                for item in self.validation_diagnostics
            )
        )
        if self.validation_diagnostics and not (
            local_output_diagnostics or provider_schema_diagnostics
        ):
            raise ValueError("validation diagnostics are outside the safe allowlist")
        return self


class ActivateAnalyzerContextCommand(StrictRevisionCommandEnvelope):
    command_type: Literal["ACTIVATE_ANALYZER_CONTEXT"]
    acting_actor_id: Literal["SYSTEM"]
    context: AnalyzerContextBinding

    @model_validator(mode="after")
    def matching_session(self) -> ActivateAnalyzerContextCommand:
        if self.session_id != self.context.session_id:
            raise ValueError("command and context session_id must match")
        if self.context.status is not ContextStatus.ACTIVE:
            raise ValueError("only an ACTIVE Analyzer context may be activated")
        return self


class InvalidateAnalyzerContextCommand(StrictRevisionCommandEnvelope):
    command_type: Literal["INVALIDATE_ANALYZER_CONTEXT"]
    acting_actor_id: Literal["SYSTEM"]
    context_id: UUID
    reason_code: Literal[
        "SOURCE_SET_CHANGED",
        "CONVERSATION_UNAVAILABLE",
        "PROFILE_MISMATCH",
        "ANALYZER_CONTRACT_CHANGED",
        "CONTEXT_MISMATCH",
        "PREPARATION_REJECTED",
        "WORKSHOP_CLOSED",
    ]


class RecordFinalTranscriptCommand(StrictRevisionCommandEnvelope):
    command_type: Literal["RECORD_FINAL_TRANSCRIPT"]
    acting_actor_id: Literal["SYSTEM"]
    transcript: TranscriptFinalizedEvent

    @model_validator(mode="after")
    def matching_event_envelope(self) -> RecordFinalTranscriptCommand:
        if self.case_id != self.transcript.case_id:
            raise ValueError("command and transcript case_id must match")
        if self.session_id != self.transcript.session_id:
            raise ValueError("command and transcript session_id must match")
        if self.correlation_id != self.transcript.correlation_id:
            raise ValueError("command and transcript correlation_id must match")
        if self.expected_case_revision != self.transcript.observed_case_revision:
            raise ValueError("command revision must match transcript observation")
        return self


class WorkshopCompletionSource(StrEnum):
    BUTTON = "BUTTON"
    VOICE_EXPLICIT = "VOICE_EXPLICIT"
    VOICE_CONFIRMED = "VOICE_CONFIRMED"


class ClaimWorkshopCompleteCommand(StrictRevisionCommandEnvelope):
    command_type: Literal["CLAIM_WORKSHOP_COMPLETE"]
    acting_actor_id: UUID
    completion_source: WorkshopCompletionSource
    completion_transcript_event_id: UUID | None
    confirmation_transcript_event_id: UUID | None

    @model_validator(mode="after")
    def source_binding(self) -> ClaimWorkshopCompleteCommand:
        if self.completion_source is WorkshopCompletionSource.BUTTON:
            if (
                self.completion_transcript_event_id is not None
                or self.confirmation_transcript_event_id is not None
            ):
                raise ValueError("button completion cannot carry transcript bindings")
        elif self.completion_source is WorkshopCompletionSource.VOICE_EXPLICIT:
            if (
                self.completion_transcript_event_id is None
                or self.confirmation_transcript_event_id is not None
            ):
                raise ValueError("explicit voice completion requires exactly one transcript")
        elif (
            self.completion_transcript_event_id is None
            or self.confirmation_transcript_event_id is None
            or self.completion_transcript_event_id
            == self.confirmation_transcript_event_id
        ):
            raise ValueError("confirmed voice completion requires two distinct transcripts")
        return self


class AdmitInterviewBriefCommand(StrictRevisionCommandEnvelope):
    command_type: Literal["ADMIT_INTERVIEW_BRIEF"]
    acting_actor_id: Literal["SYSTEM"]
    analyzer_run_id: UUID
    context_id: UUID
    provider_request_hash: Sha256
    candidate: InterviewBriefCandidate

    @model_validator(mode="after")
    def matching_run_and_context(self) -> AdmitInterviewBriefCommand:
        if self.analyzer_run_id != self.candidate.analyzer_run_id:
            raise ValueError("command and candidate analyzer_run_id must match")
        if self.context_id != self.candidate.context_id:
            raise ValueError("command and candidate context_id must match")
        if self.provider_request_hash != self.candidate.request_hash:
            raise ValueError("command and candidate provider request hashes must match")
        if self.expected_case_revision != self.candidate.based_on_case_revision:
            raise ValueError("command and candidate case revision must match")
        return self


class AdmitTurnAnalysisCommand(StrictRevisionCommandEnvelope):
    command_type: Literal["ADMIT_TURN_ANALYSIS"]
    acting_actor_id: Literal["SYSTEM"]
    analyzer_run_id: UUID
    context_id: UUID
    provider_request_hash: Sha256
    candidate: TurnAnalysisCandidate

    @model_validator(mode="after")
    def matching_run_and_context(self) -> AdmitTurnAnalysisCommand:
        if self.analyzer_run_id != self.candidate.analyzer_run_id:
            raise ValueError("command and candidate analyzer_run_id must match")
        if self.context_id != self.candidate.context_id:
            raise ValueError("command and candidate context_id must match")
        if self.provider_request_hash != self.candidate.request_hash:
            raise ValueError("command and candidate provider request hashes must match")
        if self.expected_case_revision != self.candidate.based_on_case_revision:
            raise ValueError("command and candidate case revision must match")
        return self


class AdmitGuidanceCommand(StrictRevisionCommandEnvelope):
    command_type: Literal["ADMIT_GUIDANCE"]
    acting_actor_id: Literal["SYSTEM"]
    analyzer_run_id: UUID
    context_id: UUID
    provider_request_hash: Sha256
    candidate: GuidanceCandidate

    @model_validator(mode="after")
    def matching_run_and_context(self) -> AdmitGuidanceCommand:
        if self.analyzer_run_id != self.candidate.analyzer_run_id:
            raise ValueError("command and candidate analyzer_run_id must match")
        if self.context_id != self.candidate.context_id:
            raise ValueError("command and candidate context_id must match")
        if self.provider_request_hash != self.candidate.request_hash:
            raise ValueError("command and candidate provider request hashes must match")
        if self.expected_case_revision != self.candidate.based_on_case_revision:
            raise ValueError("command and candidate case revision must match")
        return self


class AdmitReviewNarrationCommand(StrictRevisionCommandEnvelope):
    command_type: Literal["ADMIT_REVIEW_NARRATION"]
    acting_actor_id: Literal["SYSTEM"]
    analyzer_run_id: UUID
    context_id: UUID
    provider_request_hash: Sha256
    candidate: ReviewNarrationCandidate

    @model_validator(mode="after")
    def matching_run_and_context(self) -> AdmitReviewNarrationCommand:
        if self.analyzer_run_id != self.candidate.analyzer_run_id:
            raise ValueError("command and candidate analyzer_run_id must match")
        if self.context_id != self.candidate.context_id:
            raise ValueError("command and candidate context_id must match")
        if self.provider_request_hash != self.candidate.request_hash:
            raise ValueError("command and candidate provider request hashes must match")
        if self.expected_case_revision != self.candidate.based_on_case_revision:
            raise ValueError("command and candidate case revision must match")
        return self


class AdmitSpecPackageSynthesisCommand(StrictRevisionCommandEnvelope):
    command_type: Literal["ADMIT_SPEC_PACKAGE_SYNTHESIS"]
    acting_actor_id: Literal["SYSTEM"]
    target: ArtifactDraftTarget
    identity_plan: ArtifactSynthesisIdentityPlan
    confirmed_decision_bindings: Annotated[
        tuple[ConfirmedDecisionSynthesisBinding, ...], Field(max_length=26)
    ] = ()
    provider_request_hash: Sha256
    candidate: SpecPackageSynthesisCandidate

    @model_validator(mode="after")
    def matching_candidate(self) -> AdmitSpecPackageSynthesisCommand:
        if self.target.artifact_type != "SPEC_PACKAGE":
            raise ValueError("Spec synthesis requires a Spec Package target")
        if self.target != self.identity_plan.target:
            raise ValueError("command target and identity plan target must match")
        if self.candidate.foundation_artifact_id != self.target.foundation_artifact_id:
            raise ValueError("candidate and target artifact IDs must match")
        if (self.candidate.identity_plan_id, self.candidate.identity_plan_version) != (
            self.identity_plan.identity_plan_id,
            self.identity_plan.identity_plan_version,
        ):
            raise ValueError("candidate and identity plan versions must match")
        if self.candidate.semantic_state_hash != self.identity_plan.semantic_state_hash:
            raise ValueError("candidate and identity plan semantic-state hashes must match")
        if self.provider_request_hash != self.candidate.request_hash:
            raise ValueError("command and candidate provider request hashes must match")
        if self.expected_case_revision != self.candidate.based_on_case_revision:
            raise ValueError("command and candidate case revision must match")
        if self.expected_case_revision != self.identity_plan.based_on_case_revision:
            raise ValueError("command and identity plan case revision must match")
        planned = {
            (item.foundation_id, item.foundation_version): item.entity_kind
            for item in self.identity_plan.planned_identities
        }
        if any(
            planned.get((item.decision_id, item.decision_version)) != "DECISION"
            for item in self.confirmed_decision_bindings
        ):
            raise ValueError("confirmed decisions must retain their canonical planned identities")
        return self


class AdmitTechnicalContractSynthesisCommand(StrictRevisionCommandEnvelope):
    command_type: Literal["ADMIT_TECHNICAL_CONTRACT_SYNTHESIS"]
    acting_actor_id: Literal["SYSTEM"]
    target: ArtifactDraftTarget
    identity_plan: ArtifactSynthesisIdentityPlan
    confirmed_spec: ConfirmedSpecSynthesisBinding
    provider_request_hash: Sha256
    candidate: TechnicalContractSynthesisCandidate

    @model_validator(mode="after")
    def matching_candidate(self) -> AdmitTechnicalContractSynthesisCommand:
        if self.target.artifact_type != "TECHNICAL_CONTRACT":
            raise ValueError("Technical synthesis requires a Technical Contract target")
        if self.target != self.identity_plan.target:
            raise ValueError("command target and identity plan target must match")
        if self.candidate.foundation_artifact_id != self.target.foundation_artifact_id:
            raise ValueError("candidate and target artifact IDs must match")
        if (self.candidate.identity_plan_id, self.candidate.identity_plan_version) != (
            self.identity_plan.identity_plan_id,
            self.identity_plan.identity_plan_version,
        ):
            raise ValueError("candidate and identity plan versions must match")
        if self.candidate.semantic_state_hash != self.identity_plan.semantic_state_hash:
            raise ValueError("candidate and identity plan semantic-state hashes must match")
        if self.provider_request_hash != self.candidate.request_hash:
            raise ValueError("command and candidate provider request hashes must match")
        if self.expected_case_revision != self.candidate.based_on_case_revision:
            raise ValueError("command and candidate case revision must match")
        if self.expected_case_revision != self.identity_plan.based_on_case_revision:
            raise ValueError("command and identity plan case revision must match")
        return self


class CaptureLowRiskFactCommand(StrictRevisionCommandEnvelope):
    command_type: Literal["CAPTURE_LOW_RISK_FACT"]
    acting_actor_id: UUID
    fact_proposal_id: UUID
    question_id: UUID
    expected_question_version: PositiveInt
    transcript_event_id: UUID
    speaker_actor_id: UUID
    addressed_actor_id: UUID

    @model_validator(mode="after")
    def matching_actor(self) -> CaptureLowRiskFactCommand:
        if not (
            self.acting_actor_id == self.speaker_actor_id == self.addressed_actor_id
        ):
            raise ValueError("low-risk capture requires the addressed actor to be the speaker and actor")
        return self


class MaterializeDecisionBatchReviewCommand(StrictRevisionCommandEnvelope):
    command_type: Literal["MATERIALIZE_DECISION_BATCH_REVIEW"]
    pending_decision_ids: Annotated[tuple[UUID, ...], Field(min_length=1, max_length=26)]
    derived_from_cluster_ids: Annotated[tuple[UUID, ...], Field(max_length=20)]

    @model_validator(mode="after")
    def unique_ids(self) -> MaterializeDecisionBatchReviewCommand:
        if len(self.pending_decision_ids) != len(set(self.pending_decision_ids)):
            raise ValueError("pending decision IDs must be unique")
        if len(self.derived_from_cluster_ids) != len(set(self.derived_from_cluster_ids)):
            raise ValueError("cluster IDs must be unique")
        return self


class DecisionBatchItemActionCommand(ContractModel):
    review_item_id: UUID
    handle: QuestionHandle
    pending_decision_id: UUID
    expected_pending_decision_version: PositiveInt
    action: ConfirmationAction
    revision_span: TranscriptSpan | None

    @model_validator(mode="after")
    def revision_shape(self) -> DecisionBatchItemActionCommand:
        if (self.action is ConfirmationAction.REVISE) != (self.revision_span is not None):
            raise ValueError("only REVISE requires a revision transcript span")
        return self


class ApplyDecisionBatchResponseCommand(ReviewBoundCommandEnvelope):
    command_type: Literal["APPLY_DECISION_BATCH_RESPONSE"]
    actor_authentication: ActorAuthentication
    selection: VoiceConfirmationSelectionCandidate
    decision_batch_view_id: UUID
    decision_batch_view_hash: Sha256
    response_transcript_event_id: UUID
    item_actions: Annotated[tuple[DecisionBatchItemActionCommand, ...], Field(min_length=1, max_length=26)]
    unmentioned_item_policy: Literal["REMAIN_PENDING"]

    @model_validator(mode="after")
    def unique_items(self) -> ApplyDecisionBatchResponseCommand:
        if self.acting_actor_id == "SYSTEM":
            raise ValueError("a human actor must apply a decision batch response")
        if self.actor_authentication.actor_id != self.acting_actor_id:
            raise ValueError("authenticated actor must match acting_actor_id")
        if self.selection.mapping_status is not ConfirmationMappingStatus.MAPPED:
            raise ValueError("only a mapped Voice selection may be applied")
        if self.selection.speaker_actor_id != self.acting_actor_id:
            raise ValueError("Voice-attributed speaker must match acting_actor_id")
        if self.selection.decision_batch_view_id != self.decision_batch_view_id:
            raise ValueError("selection and command review-view IDs must match")
        if self.selection.decision_batch_view_hash != self.decision_batch_view_hash:
            raise ValueError("selection and command review-view hashes must match")
        if self.selection.observed_case_revision != self.observed_case_revision:
            raise ValueError("selection and command observed revisions must match")
        if self.selection.transcript_event_id != self.response_transcript_event_id:
            raise ValueError("selection and command transcript events must match")
        if (
            isinstance(self.actor_authentication, VerbalSelfAssertion)
            and self.actor_authentication.assertion_transcript_event_id
            != self.response_transcript_event_id
        ):
            raise ValueError("verbal assertion must bind the response transcript")
        review_ids = [item.review_item_id for item in self.item_actions]
        handles = [item.handle for item in self.item_actions]
        decision_ids = [item.pending_decision_id for item in self.item_actions]
        if len(review_ids) != len(set(review_ids)):
            raise ValueError("review item IDs must be unique")
        if len(handles) != len(set(handles)):
            raise ValueError("handles must be unique")
        if len(decision_ids) != len(set(decision_ids)):
            raise ValueError("pending decision IDs must be unique")
        selected = {
            item.handle: (item.action, item.revision_span)
            for item in self.selection.selections
        }
        commanded = {
            item.handle: (item.action, item.revision_span)
            for item in self.item_actions
        }
        if selected != commanded:
            raise ValueError("Foundation item actions must exactly reproduce the observed Voice selection")
        return self


class ArtifactReviewSubjectBinding(ContractModel):
    artifact_type: Literal["SPEC_PACKAGE", "TECHNICAL_CONTRACT"]
    artifact_id: UUID
    artifact_key: Annotated[str, StringConstraints(strict=True, pattern=r"^(?:SPEC|CONTRACT)-[A-Z0-9][A-Z0-9-]{2,63}$")]
    artifact_version: PositiveInt
    record_revision: PositiveInt
    payload_hash: Sha256

    @model_validator(mode="after")
    def matching_artifact_key(self) -> ArtifactReviewSubjectBinding:
        prefix = "SPEC-" if self.artifact_type == "SPEC_PACKAGE" else "CONTRACT-"
        if not self.artifact_key.startswith(prefix):
            raise ValueError("artifact type and artifact key prefix must match")
        return self


class MaterializeArtifactReviewCommand(StrictRevisionCommandEnvelope):
    command_type: Literal["MATERIALIZE_ARTIFACT_REVIEW"]
    subject: ArtifactReviewSubjectBinding
    view_mode: Literal["DRAFT", "REVIEW", "FINAL", "DIFF"]


class ArtifactConfirmationBinding(ArtifactReviewSubjectBinding):
    confirmation_id: UUID
    view_id: UUID
    view_hash: Sha256


class ResidualQualityRiskAcceptance(ContractModel):
    policy_id: Literal["V0_EXPLICIT_RESIDUAL_SPEC_RISK"]
    audit_id: UUID
    accepted_failed_rule_ids: Annotated[
        tuple[
            Annotated[
                str,
                StringConstraints(strict=True, pattern=r"^SPEC-Q-[0-9]{3}$"),
            ],
            ...,
        ],
        Field(min_length=1, max_length=26),
    ]
    acceptance_statement: Statement

    @model_validator(mode="after")
    def exact_ordered_rule_set(self) -> ResidualQualityRiskAcceptance:
        if tuple(sorted(set(self.accepted_failed_rule_ids))) != self.accepted_failed_rule_ids:
            raise ValueError("accepted failed rule IDs must be unique and sorted")
        return self


class ConfirmArtifactCommand(StrictRevisionCommandEnvelope):
    command_type: Literal["CONFIRM_ARTIFACT"]
    actor_authentication: ActorAuthentication
    binding: ArtifactConfirmationBinding
    confirmation_transcript_event_id: UUID
    approved_exception_ids: Annotated[tuple[UUID, ...], Field(max_length=100)]
    residual_quality_risk_acceptance: ResidualQualityRiskAcceptance | None = None

    @model_validator(mode="after")
    def human_actor(self) -> ConfirmArtifactCommand:
        if self.acting_actor_id == "SYSTEM":
            raise ValueError("artifact confirmation requires a human actor")
        if self.actor_authentication.actor_id != self.acting_actor_id:
            raise ValueError("authenticated actor must match acting_actor_id")
        if (
            isinstance(self.actor_authentication, VerbalSelfAssertion)
            and self.actor_authentication.assertion_transcript_event_id
            != self.confirmation_transcript_event_id
        ):
            raise ValueError("verbal assertion must bind the artifact confirmation transcript")
        if len(self.approved_exception_ids) != len(set(self.approved_exception_ids)):
            raise ValueError("approved exception IDs must be unique")
        if (
            self.residual_quality_risk_acceptance is not None
            and self.binding.artifact_type != "SPEC_PACKAGE"
        ):
            raise ValueError("residual quality risk acceptance applies only to a Spec Package")
        return self


FoundationCommand = Annotated[
    ActivateAnalyzerContextCommand
    | InvalidateAnalyzerContextCommand
    | RecordFinalTranscriptCommand
    | ClaimWorkshopCompleteCommand
    | AdmitInterviewBriefCommand
    | AdmitTurnAnalysisCommand
    | AdmitGuidanceCommand
    | AdmitReviewNarrationCommand
    | AdmitSpecPackageSynthesisCommand
    | AdmitTechnicalContractSynthesisCommand
    | CaptureLowRiskFactCommand
    | MaterializeDecisionBatchReviewCommand
    | ApplyDecisionBatchResponseCommand
    | MaterializeArtifactReviewCommand
    | ConfirmArtifactCommand,
    Field(discriminator="command_type"),
]


class CandidateIdentityMapping(ContractModel):
    analyzer_run_id: UUID
    candidate_key: CandidateKey
    entity_kind: Literal["EVIDENCE", "PROBLEM", "CLUSTER", "QUESTION", "FACT", "DECISION", "FINDING"]
    foundation_id: UUID
    record_version: PositiveInt


class CommandOutcome(StrEnum):
    APPLIED = "APPLIED"
    NO_OP_REPLAY = "NO_OP_REPLAY"
    REJECTED = "REJECTED"


class FoundationRejectionCode(StrEnum):
    STALE_STATE = "STALE_STATE"
    STALE_VIEW = "STALE_VIEW"
    STALE_ENTITY = "STALE_ENTITY"
    AUTHORITY_FAILED = "AUTHORITY_FAILED"
    SOURCE_BINDING_FAILED = "SOURCE_BINDING_FAILED"
    EVIDENCE_BINDING_FAILED = "EVIDENCE_BINDING_FAILED"
    PROVIDER_REQUEST_BINDING_FAILED = "PROVIDER_REQUEST_BINDING_FAILED"
    TRANSCRIPT_BINDING_FAILED = "TRANSCRIPT_BINDING_FAILED"
    IDENTITY_PLAN_FAILED = "IDENTITY_PLAN_FAILED"
    PAYLOAD_SCHEMA_FAILED = "PAYLOAD_SCHEMA_FAILED"
    CONFIRMATION_BINDING_FAILED = "CONFIRMATION_BINDING_FAILED"
    DUPLICATE_CONFLICT = "DUPLICATE_CONFLICT"
    UNKNOWN_REFERENCE = "UNKNOWN_REFERENCE"
    INVALID_TRANSITION = "INVALID_TRANSITION"
    MALFORMED_COMMAND = "MALFORMED_COMMAND"


class FoundationCommandReceipt(ContractModel):
    receipt_type: Literal["FOUNDATION_COMMAND"]
    command_id: UUID
    idempotency_key: IdempotencyKey
    outcome: CommandOutcome
    prior_case_revision: NonNegativeInt
    resulting_case_revision: NonNegativeInt
    occurred_at: datetime
    rejection_code: FoundationRejectionCode | None

    @model_validator(mode="after")
    def receipt_shape(self) -> FoundationCommandReceipt:
        rejected = self.outcome is CommandOutcome.REJECTED
        if rejected != (self.rejection_code is not None):
            raise ValueError("only rejected receipts carry rejection_code")
        if rejected and self.prior_case_revision != self.resulting_case_revision:
            raise ValueError("rejected command cannot advance Foundation state")
        if (
            self.outcome is CommandOutcome.APPLIED
            and self.resulting_case_revision != self.prior_case_revision + 1
        ):
            raise ValueError("an applied Foundation command advances exactly one case revision")
        return self


class DecisionItemOutcome(StrEnum):
    COMMITTED = "COMMITTED"
    REVISION_REQUESTED = "REVISION_REQUESTED"
    REJECTED = "REJECTED"
    DEFERRED = "DEFERRED"
    REMAINED_PENDING = "REMAINED_PENDING"
    REJECTED_STALE = "REJECTED_STALE"
    REJECTED_AUTHORITY = "REJECTED_AUTHORITY"


class DecisionBatchItemReceipt(ContractModel):
    review_item_id: UUID
    pending_decision_id: UUID
    outcome: DecisionItemOutcome
    committed_decision_id: UUID | None
    revision_request_id: UUID | None

    @model_validator(mode="after")
    def identifiers_match_outcome(self) -> DecisionBatchItemReceipt:
        if (self.outcome is DecisionItemOutcome.COMMITTED) != (self.committed_decision_id is not None):
            raise ValueError("only COMMITTED carries committed_decision_id")
        if (self.outcome is DecisionItemOutcome.REVISION_REQUESTED) != (
            self.revision_request_id is not None
        ):
            raise ValueError("only REVISION_REQUESTED carries revision_request_id")
        return self


class DecisionBatchResponseReceipt(ContractModel):
    receipt_type: Literal["DECISION_BATCH_RESPONSE"]
    command: FoundationCommandReceipt
    decision_batch_view_id: UUID
    response_transcript_event_id: UUID | None = None
    item_results: Annotated[tuple[DecisionBatchItemReceipt, ...], Field(min_length=1, max_length=26)]
    resulting_readiness: Readiness
    resulting_review_obligation: ReviewObligation


class AnalyzerContextCommandReceipt(ContractModel):
    receipt_type: Literal["ANALYZER_CONTEXT"]
    command: FoundationCommandReceipt
    context_id: UUID
    context_status: ContextStatus


class TranscriptRecordedReceipt(ContractModel):
    receipt_type: Literal["TRANSCRIPT_RECORDED"]
    command: FoundationCommandReceipt
    transcript_event_id: UUID
    transcript_hash: Sha256


class WorkshopCompletionState(StrEnum):
    FINISHING_ANALYSIS = "FINISHING_ANALYSIS"
    CLEANUP_PENDING = "CLEANUP_PENDING"
    COMPLETE = "COMPLETE"


class WorkshopCompletionReceipt(ContractModel):
    receipt_type: Literal["WORKSHOP_COMPLETION"]
    command: FoundationCommandReceipt
    completion_source: WorkshopCompletionSource
    completed_at: datetime
    completion_transcript_event_id: UUID | None
    confirmation_transcript_event_id: UUID | None
    state: Literal[WorkshopCompletionState.FINISHING_ANALYSIS]


class ProposalAdmissionReceipt(ContractModel):
    receipt_type: Literal["PROPOSAL_ADMISSION"]
    command: FoundationCommandReceipt
    analyzer_run_id: UUID
    identity_mappings: Annotated[tuple[CandidateIdentityMapping, ...], Field(max_length=2500)]
    admitted_guidance_id: UUID | None


class ArtifactSynthesisAdmissionReceipt(ContractModel):
    receipt_type: Literal["ARTIFACT_SYNTHESIS_ADMISSION"]
    command: FoundationCommandReceipt
    analyzer_run_id: UUID
    artifact_type: Literal["SPEC_PACKAGE", "TECHNICAL_CONTRACT"]
    artifact_id: UUID
    artifact_key: Annotated[str, StringConstraints(strict=True, pattern=r"^(?:SPEC|CONTRACT)-[A-Z0-9][A-Z0-9-]{2,63}$")]
    artifact_version: PositiveInt
    record_revision: PositiveInt
    payload_hash: Sha256


class ReviewNarrationAdmissionReceipt(ContractModel):
    receipt_type: Literal["REVIEW_NARRATION_ADMISSION"]
    command: FoundationCommandReceipt
    narration_id: UUID
    narration_version: PositiveInt
    decision_batch_view_id: UUID


class LowRiskFactCaptureReceipt(ContractModel):
    receipt_type: Literal["LOW_RISK_FACT_CAPTURE"]
    command: FoundationCommandReceipt
    fact_id: UUID
    fact_version: PositiveInt
    promoted_to_pending_decision_id: UUID | None


class DecisionBatchReviewReceipt(ContractModel):
    receipt_type: Literal["DECISION_BATCH_REVIEW"]
    command: FoundationCommandReceipt
    view_id: UUID
    view_hash: Sha256


class ArtifactReviewReceipt(ContractModel):
    receipt_type: Literal["ARTIFACT_REVIEW"]
    command: FoundationCommandReceipt
    confirmation_id: UUID
    view_id: UUID
    view_hash: Sha256


class ArtifactConfirmationReceipt(ContractModel):
    receipt_type: Literal["ARTIFACT_CONFIRMATION"]
    command: FoundationCommandReceipt
    binding: ArtifactConfirmationBinding


FoundationReceipt = Annotated[
    AnalyzerContextCommandReceipt
    | TranscriptRecordedReceipt
    | WorkshopCompletionReceipt
    | ProposalAdmissionReceipt
    | ArtifactSynthesisAdmissionReceipt
    | ReviewNarrationAdmissionReceipt
    | LowRiskFactCaptureReceipt
    | DecisionBatchReviewReceipt
    | DecisionBatchResponseReceipt
    | ArtifactReviewReceipt
    | ArtifactConfirmationReceipt,
    Field(discriminator="receipt_type"),
]


class ProblemOriginView(ContractModel):
    problem_id: UUID
    problem_version: PositiveInt
    problem_statement: Statement
    resolution_kind: ProblemResolutionKind
    evidence_summary: Statement


class DecisionReviewItemView(ContractModel):
    review_item_id: UUID
    handle: QuestionHandle
    pending_decision_id: UUID
    pending_decision_version: PositiveInt
    classification: Domain
    exact_statement: Statement
    rationale: Statement
    problem_origins: Annotated[tuple[ProblemOriginView, ...], Field(min_length=1, max_length=25)]


class DecisionBatchReviewView(ContractModel):
    protocol_version: Literal["1.0.0"]
    view_type: Literal["DECISION_BATCH_REVIEW"]
    view_id: UUID
    view_hash: Sha256
    session_id: UUID
    based_on_case_revision: NonNegativeInt
    derived_from_cluster_ids: Annotated[tuple[UUID, ...], Field(max_length=20)]
    items: Annotated[tuple[DecisionReviewItemView, ...], Field(min_length=1, max_length=26)]
    generated_at: datetime

    @model_validator(mode="after")
    def unique_view_items(self) -> DecisionBatchReviewView:
        handles = [item.handle for item in self.items]
        item_ids = [item.review_item_id for item in self.items]
        decision_ids = [item.pending_decision_id for item in self.items]
        if len(handles) != len(set(handles)):
            raise ValueError("view handles must be unique")
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("review item IDs must be unique")
        if len(decision_ids) != len(set(decision_ids)):
            raise ValueError("pending decision IDs must be unique")
        return self


class RunwayHealth(StrEnum):
    HEALTHY = "HEALTHY"
    PRIORITIZE_RUNWAY_REPLENISHMENT = "PRIORITIZE_RUNWAY_REPLENISHMENT"
    PAUSE_DEEP_SYNTHESIS = "PAUSE_DEEP_SYNTHESIS"
    SAFE_RECOVERY_ONLY = "SAFE_RECOVERY_ONLY"


class VoiceSessionCard(ContractModel):
    protocol_version: Literal["1.0.0"]
    view_type: Literal["VOICE_SESSION_CARD"]
    session_id: UUID
    case_revision: NonNegativeInt
    readiness: Readiness
    review_obligation: ReviewObligation
    committed_summary: Annotated[tuple[Statement, ...], Field(max_length=50)]
    admitted_guidance: AdmittedGuidance | None
    runway_health: RunwayHealth
    pending_decision_batch_view_id: UUID | None
    admitted_review_narration: AdmittedReviewNarration | None
    generated_at: datetime


class AnalyzerContextActivatedEvent(EventEnvelope):
    event_type: Literal["ANALYZER_CONTEXT_ACTIVATED"]
    producer: Literal["FOUNDATION"]
    context_id: UUID
    source_set_hash: Sha256


class AnalyzerProposalAdmittedEvent(EventEnvelope):
    event_type: Literal["ANALYZER_PROPOSAL_ADMITTED"]
    producer: Literal["FOUNDATION"]
    analyzer_run_id: UUID
    identity_mappings: Annotated[tuple[CandidateIdentityMapping, ...], Field(max_length=2500)]


class GuidanceAdmittedEvent(EventEnvelope):
    event_type: Literal["GUIDANCE_ADMITTED"]
    producer: Literal["FOUNDATION"]
    guidance: AdmittedGuidance


class GuidanceInvalidatedEvent(EventEnvelope):
    event_type: Literal["GUIDANCE_INVALIDATED"]
    producer: Literal["FOUNDATION"]
    guidance_id: UUID
    guidance_version: PositiveInt
    trigger: GuidanceInvalidationTrigger


class ReviewNarrationAdmittedEvent(EventEnvelope):
    event_type: Literal["REVIEW_NARRATION_ADMITTED"]
    producer: Literal["FOUNDATION"]
    narration: AdmittedReviewNarration


class AnalyzerContextInvalidatedEvent(EventEnvelope):
    event_type: Literal["ANALYZER_CONTEXT_INVALIDATED"]
    producer: Literal["FOUNDATION"]
    context_id: UUID
    reason_code: Literal[
        "SOURCE_SET_CHANGED",
        "CONVERSATION_UNAVAILABLE",
        "PROFILE_MISMATCH",
        "ANALYZER_CONTRACT_CHANGED",
        "CONTEXT_MISMATCH",
        "PREPARATION_REJECTED",
        "WORKSHOP_CLOSED",
    ]


class ReviewNarrationInvalidatedEvent(EventEnvelope):
    event_type: Literal["REVIEW_NARRATION_INVALIDATED"]
    producer: Literal["FOUNDATION"]
    narration_id: UUID
    narration_version: PositiveInt
    decision_batch_view_id: UUID
    trigger: Literal["VIEW_REPLACED", "VIEW_STALE", "SOURCE_SET_CHANGED", "WORKSHOP_CLOSED"]


class VoiceConfirmationSelectionObservedEvent(EventEnvelope):
    event_type: Literal["VOICE_CONFIRMATION_SELECTION_OBSERVED"]
    producer: Literal["VOICE"]
    candidate: VoiceConfirmationSelectionCandidate

    @model_validator(mode="after")
    def matching_candidate(self) -> VoiceConfirmationSelectionObservedEvent:
        if self.event_id != self.candidate.selection_event_id:
            raise ValueError("event and selection candidate IDs must match")
        if self.observed_case_revision != self.candidate.observed_case_revision:
            raise ValueError("event and selection candidate revisions must match")
        return self


class ArtifactSynthesisAdmittedEvent(EventEnvelope):
    event_type: Literal["ARTIFACT_SYNTHESIS_ADMITTED"]
    producer: Literal["FOUNDATION"]
    receipt: ArtifactSynthesisAdmissionReceipt


class DecisionBatchResponseAppliedEvent(EventEnvelope):
    event_type: Literal["DECISION_BATCH_RESPONSE_APPLIED"]
    producer: Literal["FOUNDATION"]
    receipt: DecisionBatchResponseReceipt


class ArtifactConfirmedEvent(EventEnvelope):
    event_type: Literal["ARTIFACT_CONFIRMED"]
    producer: Literal["FOUNDATION"]
    binding: ArtifactConfirmationBinding


class ArtifactReviewMaterializedEvent(EventEnvelope):
    event_type: Literal["ARTIFACT_REVIEW_MATERIALIZED"]
    producer: Literal["FOUNDATION"]
    subject: ArtifactReviewSubjectBinding
    confirmation_id: UUID
    view_id: UUID
    view_hash: Sha256


WorkshopEvent = Annotated[
    TranscriptFinalizedEvent
    | AnalyzerContextActivatedEvent
    | AnalyzerContextInvalidatedEvent
    | AnalyzerProposalAdmittedEvent
    | GuidanceAdmittedEvent
    | GuidanceInvalidatedEvent
    | ReviewNarrationAdmittedEvent
    | ReviewNarrationInvalidatedEvent
    | VoiceConfirmationSelectionObservedEvent
    | ArtifactSynthesisAdmittedEvent
    | DecisionBatchResponseAppliedEvent
    | ArtifactReviewMaterializedEvent
    | ArtifactConfirmedEvent,
    Field(discriminator="event_type"),
]


AnalyzerProviderRequest = Annotated[
    BootstrapAnalyzerRequest
    | AnalyzeFinalTurnRequest
    | ReplenishGuidanceRequest
    | GenerateReviewNarrationRequest
    | SpecPackageSynthesisRequest
    | TechnicalContractSynthesisRequest,
    Field(discriminator="request_type"),
]


AnalyzerProviderCandidate = Annotated[
    InterviewBriefCandidate
    | TurnAnalysisCandidate
    | GuidanceCandidate
    | ReviewNarrationCandidate
    | SpecPackageSynthesisCandidate
    | TechnicalContractSynthesisCandidate,
    Field(discriminator="output_type"),
]


__all__ = [
    "PROTOCOL_VERSION",
    "INITIAL_RUNWAY_DEPTH",
    "INITIAL_RUNWAY_SAFE_ALTERNATE_COUNT",
    "ActivateAnalyzerContextCommand",
    "AdmitSpecPackageSynthesisCommand",
    "AdmitTechnicalContractSynthesisCommand",
    "AdmitGuidanceCommand",
    "AdmitReviewNarrationCommand",
    "AdmitInterviewBriefCommand",
    "AdmitTurnAnalysisCommand",
    "AdmittedGuidance",
    "AdmittedReviewNarration",
    "AnalyzerContextBinding",
    "AnalyzerContractBinding",
    "AnalyzerProviderCandidate",
    "AnalyzerProviderRequest",
    "AnalyzeFinalTurnRequest",
    "ApplyDecisionBatchResponseCommand",
    "ArtifactConstructionBlueprint",
    "ArtifactConstructionSlot",
    "ArtifactConfirmationBinding",
    "ArtifactIdentityAssignment",
    "ArtifactIdentitySlot",
    "ArtifactSynthesisQualityRule",
    "ArtifactReviewSubjectBinding",
    "BootstrapAnalyzerRequest",
    "CaptureLowRiskFactCommand",
    "ConfirmArtifactCommand",
    "ConfirmedDecisionSynthesisBinding",
    "GenerateReviewNarrationRequest",
    "DecisionBatchResponseReceipt",
    "DecisionBatchReviewView",
    "FoundationCommand",
    "FoundationCommandReceipt",
    "FoundationReceipt",
    "GuidanceCandidate",
    "InterviewBriefCandidate",
    "MaterializeArtifactReviewCommand",
    "ProviderFailureReceipt",
    "ReviewNarrationCandidate",
    "ReplenishGuidanceRequest",
    "SourceSetBinding",
    "TranscriptFinalizedEvent",
    "SpecPackageSynthesisCandidate",
    "SpecPackageSynthesisRequest",
    "TechnicalContractSynthesisCandidate",
    "TechnicalContractSynthesisRequest",
    "TechnicalClosureManifest",
    "TechnicalClosureObligation",
    "TechnicalClosureRule",
    "TECHNICAL_CLOSURE_OBLIGATION_PATHS",
    "TECHNICAL_CLOSURE_RULE_LAYOUT",
    "technical_closure_obligations_from_spec_payload",
    "TurnAnalysisCandidate",
    "VoiceConfirmationSelectionCandidate",
    "VoiceSessionCard",
    "WorkshopEvent",
]
