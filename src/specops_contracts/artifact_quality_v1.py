"""Executable Artifact Quality Audit Protocol 1.0.0 contracts.

The evaluator boundary is provider-neutral. Provider outputs are semantic
attestations only; Foundation owns deterministic reduction and readiness.
"""

from __future__ import annotations

import json
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
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
Sha256 = Annotated[str, StringConstraints(pattern=r"^sha256:[a-f0-9]{64}$")]
NonEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100_000)]
CompleteText = Annotated[str, StringConstraints(min_length=1, max_length=2_000_000)]
ProviderIdentifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._:-]{1,512}$")]
JsonPointer = Annotated[
    str,
    StringConstraints(pattern=r"^(?:|(?:/(?:[^~/]|~0|~1)*)+)$", max_length=2_048),
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ArtifactType(StrEnum):
    SPEC_PACKAGE = "SPEC_PACKAGE"
    TECHNICAL_CONTRACT = "TECHNICAL_CONTRACT"


class SourceRole(StrEnum):
    PM_SPEC = "PM_SPEC"
    TECHNICAL_CONTRACT = "TECHNICAL_CONTRACT"


class TranscriptActor(StrEnum):
    PM = "PM"
    DEV_LEAD = "DEV_LEAD"
    OTHER_PARTICIPANT = "OTHER_PARTICIPANT"


class QualityCheckType(StrEnum):
    STRUCTURAL = "structural"
    REFERENTIAL = "referential"
    SEMANTIC = "semantic"
    AUTHORITY = "authority"
    HUMAN = "human"


class PrimaryEvaluator(StrEnum):
    ANALYZER = "analyzer"
    FOUNDATION = "foundation"


class QualityGate(StrEnum):
    BLOCK_CONFIRMATION = "block_confirmation"
    BLOCK_HANDOFF = "block_handoff"


class FailureEffect(StrEnum):
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    BLOCKED = "BLOCKED"
    CONDITIONAL = "CONDITIONAL"
    REJECT_MUTATION = "REJECT_MUTATION"


class SemanticAssessmentResult(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    ABSTAIN = "ABSTAIN"


class ComponentResult(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    PENDING = "PENDING"


class AuditOutcome(StrEnum):
    PENDING_AUDIT = "PENDING_AUDIT"
    PASS = "PASS"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    CONDITIONAL = "CONDITIONAL"
    BLOCKED = "BLOCKED"
    REJECTED = "REJECTED"


class AuditState(StrEnum):
    PENDING_PROVIDER = "PENDING_PROVIDER"
    CONTEXT_READY = "CONTEXT_READY"
    ADMITTED = "ADMITTED"


class QualityProviderFailureCode(StrEnum):
    HTTP_401 = "HTTP_401"
    HTTP_403 = "HTTP_403"
    HTTP_404 = "HTTP_404"
    HTTP_409 = "HTTP_409"
    HTTP_429 = "HTTP_429"
    HTTP_5XX = "HTTP_5XX"
    TIMEOUT = "TIMEOUT"
    CONNECTION = "CONNECTION"
    SCHEMA_REJECTED = "SCHEMA_REJECTED"
    REQUEST_TOO_LARGE = "REQUEST_TOO_LARGE"
    CONVERSATION_UNAVAILABLE = "CONVERSATION_UNAVAILABLE"
    OUTPUT_INVALID = "OUTPUT_INVALID"
    UNKNOWN_SAFE = "UNKNOWN_SAFE"


class QualitySafeValidationDiagnostic(StrictModel):
    path: tuple[Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_$.-]{1,64}$")], ...] = Field(max_length=16)
    code: Literal["missing", "invalid_type", "out_of_range", "unknown_field", "invariant_failed", "malformed_json"]


class QualityProviderFailureReceipt(StrictModel):
    provider: Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{1,31}$")]
    stage: Literal["CONVERSATION_CREATE", "ARTIFACT_QUALITY_AUDIT", "LOCAL_VALIDATION"]
    client_request_id: ProviderIdentifier
    provider_request_id: ProviderIdentifier | None
    status_code: int | None = Field(default=None, strict=True, ge=100, le=599)
    code: QualityProviderFailureCode
    retryable: bool
    validation_diagnostics: tuple[QualitySafeValidationDiagnostic, ...] = Field(max_length=50)
    occurred_at: datetime


class ArtifactAuditSubject(StrictModel):
    artifact_type: ArtifactType
    artifact_id: UUID
    artifact_key: Annotated[str, StringConstraints(pattern=r"^(?:SPEC|CONTRACT)-[A-Z0-9_-]{1,64}$")]
    artifact_version: int = Field(strict=True, ge=1)
    record_revision: int = Field(strict=True, ge=1)
    payload_hash: Sha256
    canonical_payload_json: CompleteText


class AuditSourceDocument(StrictModel):
    source_id: UUID
    role: SourceRole
    version: int = Field(strict=True, ge=1)
    payload_hash: Sha256
    canonical_locator: NonEmpty
    filename: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    media_type: Literal["text/markdown", "text/plain"]
    complete_text: CompleteText


class ExactEvidenceSupportPair(StrictModel):
    pair_id: UUID
    finding_id: UUID
    claim_ref: UUID
    claim_pointer: JsonPointer
    claim_hash: Sha256
    exact_claim: NonEmpty
    evidence_ref: UUID
    source_id: UUID
    source_hash: Sha256
    locator: NonEmpty
    exact_excerpt: NonEmpty
    excerpt_hash: Sha256


class ArtifactEvidenceSupportRequest(StrictModel):
    protocol_version: Literal["1.0.0"]
    request_id: UUID
    evaluator_run_id: UUID
    request_hash: Sha256
    artifact_id: UUID
    artifact_version: int = Field(strict=True, ge=1)
    record_revision: int = Field(strict=True, ge=1)
    payload_hash: Sha256
    pairs: tuple[ExactEvidenceSupportPair, ...] = Field(min_length=1, max_length=1_000)

    @model_validator(mode="after")
    def unique_pairs(self):
        pair_ids = [item.pair_id for item in self.pairs]
        evidence_ids = [item.evidence_ref for item in self.pairs]
        finding_ids = [item.finding_id for item in self.pairs]
        if len(pair_ids) != len(set(pair_ids)):
            raise ValueError("evidence-support pair IDs must be unique")
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("evidence identities must be unique")
        if len(finding_ids) != len(set(finding_ids)):
            raise ValueError("evidence finding identities must be unique")
        return self


class EvidenceSupportResult(StrEnum):
    SUPPORTS = "SUPPORTS"
    SUGGESTS = "SUGGESTS"
    CONTRADICTS = "CONTRADICTS"
    INSUFFICIENT = "INSUFFICIENT"
    AMBIGUOUS = "AMBIGUOUS"


class EvidenceSupportAssessment(StrictModel):
    pair_id: UUID
    assessment: EvidenceSupportResult
    confidence: float = Field(strict=True, ge=0, le=1)


class ArtifactEvidenceSupportCandidate(StrictModel):
    protocol_version: Literal["1.0.0"]
    output_type: Literal["ARTIFACT_EVIDENCE_SUPPORT_CANDIDATE"]
    request_id: UUID
    evaluator_run_id: UUID
    request_hash: Sha256
    artifact_id: UUID
    artifact_version: int = Field(strict=True, ge=1)
    record_revision: int = Field(strict=True, ge=1)
    payload_hash: Sha256
    assessments: tuple[EvidenceSupportAssessment, ...] = Field(
        min_length=1, max_length=1_000
    )

    @model_validator(mode="after")
    def unique_assessments(self):
        pair_ids = [item.pair_id for item in self.assessments]
        if len(pair_ids) != len(set(pair_ids)):
            raise ValueError("evidence-support assessments must be unique")
        return self


class AllocatedRevisionIdentity(StrictModel):
    foundation_id: UUID
    foundation_version: int = Field(strict=True, ge=1)
    entity_kind: Annotated[
        str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{1,63}$")
    ]


class ArtifactQualityRevisionRequest(StrictModel):
    protocol_version: Literal["1.0.0"]
    revision_request_id: UUID
    revision_request_version: Literal[1]
    request_hash: Sha256
    artifact_id: UUID
    artifact_version: int = Field(strict=True, ge=1)
    record_revision: int = Field(strict=True, ge=1)
    based_on_case_revision: int = Field(strict=True, ge=0)
    payload_hash: Sha256
    canonical_payload_json: CompleteText
    audit_id: UUID
    finding_ids: tuple[UUID, ...] = Field(min_length=1, max_length=1_000)
    failed_rule_ids: tuple[
        Annotated[str, StringConstraints(pattern=r"^(?:SPEC|TECH)-Q-[0-9]{3}$")],
        ...,
    ] = Field(min_length=1, max_length=26)
    canonical_artifact_pointers: tuple[JsonPointer, ...] = Field(
        min_length=1, max_length=1_000
    )
    allocated_identities: tuple[AllocatedRevisionIdentity, ...] = Field(max_length=10_000)
    immutable_projection_hash: Sha256
    max_revision_attempts: Literal[1]

    @model_validator(mode="after")
    def unique_revision_bindings(self):
        for values, label in (
            (self.finding_ids, "finding IDs"),
            (self.failed_rule_ids, "failed rule IDs"),
            (self.canonical_artifact_pointers, "artifact pointers"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"revision request {label} must be unique")
        identities = [item.foundation_id for item in self.allocated_identities]
        if len(identities) != len(set(identities)):
            raise ValueError("allocated revision identities must be unique")
        return self


class ArtifactQualityRevisionReceipt(StrictModel):
    protocol_version: Literal["1.0.0"]
    revision_request_id: UUID
    revision_request_version: Literal[1]
    request_hash: Sha256
    artifact_id: UUID
    artifact_version: int = Field(strict=True, ge=1)
    prior_record_revision: int = Field(strict=True, ge=1)
    resulting_record_revision: int = Field(strict=True, ge=2)
    prior_payload_hash: Sha256
    resulting_payload_hash: Sha256
    audit_id: UUID
    resulting_case_revision: int = Field(strict=True, ge=1)
    replayed: bool


class ArtifactRevisionPatch(StrictModel):
    pointer: JsonPointer
    replacement_value_json: CompleteText

    @field_validator("replacement_value_json")
    @classmethod
    def valid_json_value(cls, value: str) -> str:
        try:
            json.loads(value)
        except ValueError as exc:
            raise ValueError("revision replacement must be valid JSON") from exc
        return value


class ArtifactQualityRevisionCandidate(StrictModel):
    protocol_version: Literal["1.0.0"]
    output_type: Literal["ARTIFACT_QUALITY_REVISION_CANDIDATE"]
    revision_request_id: UUID
    revision_request_version: Literal[1]
    request_hash: Sha256
    artifact_id: UUID
    artifact_version: int = Field(strict=True, ge=1)
    record_revision: int = Field(strict=True, ge=1)
    payload_hash: Sha256
    attempt: Literal[1]
    patches: tuple[ArtifactRevisionPatch, ...] = Field(min_length=1, max_length=1_000)

    @model_validator(mode="after")
    def unique_patch_pointers(self):
        pointers = [item.pointer for item in self.patches]
        if len(pointers) != len(set(pointers)):
            raise ValueError("revision patch pointers must be unique")
        return self


class AuditTranscript(StrictModel):
    event_id: UUID
    sequence_number: int = Field(strict=True, ge=1)
    transcript_artifact_id: UUID
    transcript_version: int = Field(strict=True, ge=1)
    transcript_hash: Sha256
    actor: TranscriptActor
    speaker_actor_id: UUID
    complete_text: CompleteText


class QualityContractBinding(StrictModel):
    contract_id: Literal["SEMANTIC-QUALITY-CONTRACT"]
    version: Literal["2.2.0"]
    content_hash: Sha256


class QualityRuleManifestEntry(StrictModel):
    rule_id: Annotated[str, StringConstraints(pattern=r"^(?:SPEC|TECH)-Q-[0-9]{3}$")]
    name: NonEmpty
    dimension: NonEmpty
    check_types: tuple[QualityCheckType, ...] = Field(min_length=1, max_length=5)
    gate: QualityGate
    primary_evaluator: PrimaryEvaluator
    requirement: NonEmpty
    pass_condition: NonEmpty
    evidence_of_pass: NonEmpty
    failure_effect: FailureEffect

    @model_validator(mode="after")
    def unique_check_types(self):
        if len(set(self.check_types)) != len(self.check_types):
            raise ValueError("check_types must be unique")
        return self


class ConfirmedSpecAuditBinding(StrictModel):
    artifact_id: UUID
    artifact_key: Annotated[str, StringConstraints(pattern=r"^SPEC-[A-Z0-9_-]{1,64}$")]
    artifact_version: int = Field(strict=True, ge=1)
    record_revision: int = Field(strict=True, ge=1)
    payload_hash: Sha256
    confirmation_id: UUID
    confirmed_case_revision: int = Field(strict=True, ge=1)
    canonical_payload_json: NonEmpty


class ArtifactQualityAuditBundle(StrictModel):
    protocol_version: Literal["1.0.0"]
    audit_id: UUID
    evaluator_run_id: UUID
    case_id: UUID
    session_id: UUID
    based_on_case_revision: int = Field(strict=True, ge=0)
    subject: ArtifactAuditSubject
    quality_contract: QualityContractBinding
    source_set_hash: Sha256
    sources: tuple[AuditSourceDocument, AuditSourceDocument]
    transcript_count: int = Field(strict=True, ge=0, le=10_000)
    first_transcript_sequence: int | None = Field(default=None, strict=True, ge=1)
    last_transcript_sequence: int | None = Field(default=None, strict=True, ge=1)
    transcript_manifest_hash: Sha256
    transcripts: tuple[AuditTranscript, ...] = Field(max_length=10_000)
    semantic_state_hash: Sha256
    canonical_semantic_snapshot_json: NonEmpty
    confirmed_spec: ConfirmedSpecAuditBinding | None
    rule_manifest: tuple[QualityRuleManifestEntry, ...] = Field(min_length=26, max_length=26)
    semantic_rule_ids: tuple[str, ...] = Field(min_length=21, max_length=21)
    audit_scope_manifest_hash: Sha256
    request_hash: Sha256

    @model_validator(mode="after")
    def complete_closed_universe(self):
        if tuple(item.role for item in self.sources) != (SourceRole.PM_SPEC, SourceRole.TECHNICAL_CONTRACT):
            raise ValueError("sources must be ordered PM_SPEC then TECHNICAL_CONTRACT")
        if len({item.source_id for item in self.sources}) != 2:
            raise ValueError("sources must have distinct Foundation identities")
        if self.transcript_count != len(self.transcripts):
            raise ValueError("transcript_count must equal the transcript manifest")
        sequences = tuple(item.sequence_number for item in self.transcripts)
        if sequences and sequences != tuple(range(1, len(sequences) + 1)):
            raise ValueError("final transcripts must be complete and contiguous from sequence 1")
        boundary = (sequences[0], sequences[-1]) if sequences else (None, None)
        if (self.first_transcript_sequence, self.last_transcript_sequence) != boundary:
            raise ValueError("transcript range does not match the complete manifest")
        rule_ids = tuple(item.rule_id for item in self.rule_manifest)
        if len(set(rule_ids)) != 26:
            raise ValueError("rule manifest must contain 26 unique rules")
        semantic_ids = tuple(item.rule_id for item in self.rule_manifest if QualityCheckType.SEMANTIC in item.check_types)
        if semantic_ids != self.semantic_rule_ids or len(set(self.semantic_rule_ids)) != 21:
            raise ValueError("semantic_rule_ids must exactly match the 21 semantic rules")
        prefix = "SPEC" if self.subject.artifact_type is ArtifactType.SPEC_PACKAGE else "TECH"
        if any(not item.startswith(f"{prefix}-Q-") for item in rule_ids):
            raise ValueError("rule manifest does not match the artifact type")
        if (self.subject.artifact_type is ArtifactType.TECHNICAL_CONTRACT) != (self.confirmed_spec is not None):
            raise ValueError("confirmed Spec is required only for a Technical Contract audit")
        return self


class QualityFindingCandidate(StrictModel):
    candidate_key: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]
    severity: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
    category: Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{1,63}$")]
    message: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2_000)]
    artifact_pointers: tuple[JsonPointer, ...] = Field(max_length=100)
    evidence_ids: tuple[UUID, ...] = Field(max_length=100)
    transcript_event_ids: tuple[UUID, ...] = Field(max_length=100)


class SemanticRuleAssessment(StrictModel):
    rule_id: Annotated[str, StringConstraints(pattern=r"^(?:SPEC|TECH)-Q-[0-9]{3}$")]
    result: SemanticAssessmentResult
    explanation: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4_000)]
    applicability_reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2_000)] | None
    artifact_pointers: tuple[JsonPointer, ...] = Field(max_length=100)
    evidence_ids: tuple[UUID, ...] = Field(max_length=100)
    transcript_event_ids: tuple[UUID, ...] = Field(max_length=100)
    findings: tuple[QualityFindingCandidate, ...] = Field(max_length=50)

    @model_validator(mode="after")
    def evidence_and_applicability(self):
        if (self.result is SemanticAssessmentResult.NOT_APPLICABLE) != (self.applicability_reason is not None):
            raise ValueError("only NOT_APPLICABLE requires an applicability reason")
        if self.result is SemanticAssessmentResult.PASS and not (self.artifact_pointers or self.evidence_ids or self.transcript_event_ids):
            raise ValueError("semantic PASS requires resolvable evidence of pass")
        keys = tuple(item.candidate_key for item in self.findings)
        if len(set(keys)) != len(keys):
            raise ValueError("finding candidate keys must be unique within an assessment")
        return self


class ArtifactSemanticAttestationCandidate(StrictModel):
    protocol_version: Literal["1.0.0"]
    output_type: Literal["ARTIFACT_SEMANTIC_ATTESTATION_CANDIDATE"]
    audit_id: UUID
    evaluator_run_id: UUID
    request_hash: Sha256
    artifact_id: UUID
    artifact_version: int = Field(strict=True, ge=1)
    record_revision: int = Field(strict=True, ge=1)
    payload_hash: Sha256
    audit_scope_manifest_hash: Sha256
    semantic_quality_contract_hash: Sha256
    assessments: tuple[SemanticRuleAssessment, ...] = Field(min_length=21, max_length=21)

    @model_validator(mode="after")
    def unique_rule_assessments(self):
        if len({item.rule_id for item in self.assessments}) != 21:
            raise ValueError("attestation must contain 21 unique semantic rule assessments")
        return self


class EvaluatorExecutionBinding(StrictModel):
    provider: Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{1,31}$")]
    model: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    reasoning_effort: Literal["medium"]
    provider_conversation_id: ProviderIdentifier
    provider_response_id: ProviderIdentifier
    client_request_id: ProviderIdentifier
    store_enabled: Literal[True]
    started_at: datetime
    completed_at: datetime


class StandaloneEvaluatorExecutionBinding(StrictModel):
    """One stored Response whose request carries all required state."""

    provider: Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{1,31}$")]
    model: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    reasoning_effort: Literal["medium"]
    provider_response_id: ProviderIdentifier
    client_request_id: ProviderIdentifier
    store_enabled: Literal[True]
    started_at: datetime
    completed_at: datetime


class PrepareArtifactQualityAuditCommand(StrictModel):
    protocol_version: Literal["1.0.0"]
    command_id: UUID
    idempotency_key: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")]
    expected_case_revision: int = Field(strict=True, ge=0)
    bundle: ArtifactQualityAuditBundle


class BindArtifactQualityEvaluatorCommand(StrictModel):
    protocol_version: Literal["1.0.0"]
    command_id: UUID
    audit_id: UUID
    request_hash: Sha256
    provider: Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{1,31}$")]
    model: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    reasoning_effort: Literal["medium"]
    provider_conversation_id: ProviderIdentifier
    client_request_id: ProviderIdentifier
    started_at: datetime


class CheckpointArtifactQualityResponseCommand(StrictModel):
    protocol_version: Literal["1.0.0"]
    command_id: UUID
    audit_id: UUID
    request_hash: Sha256
    client_request_id: ProviderIdentifier
    provider_response_id: ProviderIdentifier


class AdmitArtifactQualityAuditCommand(StrictModel):
    protocol_version: Literal["1.0.0"]
    command_id: UUID
    idempotency_key: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")]
    expected_case_revision: int = Field(strict=True, ge=0)
    bundle: ArtifactQualityAuditBundle
    execution: EvaluatorExecutionBinding
    candidate: ArtifactSemanticAttestationCandidate


class AdmittedQualityFinding(StrictModel):
    finding_id: UUID
    finding_version: Literal[1]
    audit_id: UUID
    rule_id: Annotated[str, StringConstraints(pattern=r"^(?:SPEC|TECH)-Q-[0-9]{3}$")]
    candidate_key: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]
    severity: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
    category: Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{1,63}$")]
    message: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2_000)]
    artifact_pointers: tuple[JsonPointer, ...]
    evidence_ids: tuple[UUID, ...]
    transcript_event_ids: tuple[UUID, ...]


class FoundationSubcheck(StrictModel):
    check_type: Literal["structural", "referential", "authority", "human"]
    result: ComponentResult
    evidence_codes: tuple[Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{1,63}$")], ...]
    explanation: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2_000)]


class CombinedQualityRuleResult(StrictModel):
    rule_id: Annotated[str, StringConstraints(pattern=r"^(?:SPEC|TECH)-Q-[0-9]{3}$")]
    semantic_result: SemanticAssessmentResult | None
    foundation_subchecks: tuple[FoundationSubcheck, ...]
    result: ComponentResult
    failure_effect: FailureEffect
    finding_refs: tuple[UUID, ...]
    explanation: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4_000)]


class ArtifactQualityAuditReceipt(StrictModel):
    protocol_version: Literal["1.0.0"]
    audit_id: UUID
    request_hash: Sha256
    state: AuditState
    artifact_id: UUID
    artifact_version: int = Field(strict=True, ge=1)
    audited_record_revision: int = Field(strict=True, ge=1)
    resulting_record_revision: int = Field(strict=True, ge=1)
    outcome: AuditOutcome
    findings: tuple[AdmittedQualityFinding, ...]
    combined_rule_results: tuple[CombinedQualityRuleResult, ...]
    resulting_case_revision: int = Field(strict=True, ge=0)
    replayed: bool


class PreparedArtifactQualityAudit(StrictModel):
    audit_id: UUID
    request_hash: Sha256
    state: AuditState
    outcome: AuditOutcome
    existing_receipt: ArtifactQualityAuditReceipt | None


__all__ = tuple(name for name in globals() if not name.startswith("_") and name not in {"Annotated", "Literal"})
