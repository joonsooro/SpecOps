"""Foundation-owned exact-claim evidence binding for SPEC-Q-026."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Callable
from uuid import UUID, uuid4

from specops_contracts import artifact_quality_v1 as q
from specops_contracts.canonical import domain_hash, payload_hash
from specops_workshop.v4.artifact_quality import resolve_payload_pointer


@dataclass(frozen=True)
class EvidenceSupportProposal:
    claim_ref: UUID
    claim_pointer: str
    source_id: UUID
    locator: str
    exact_excerpt: str
    evidence_ref: UUID | None = None
    finding_ref: UUID | None = None


def prepare_evidence_support_request(
    *,
    evaluator_run_id: UUID,
    artifact_id: UUID,
    artifact_version: int,
    record_revision: int,
    payload: dict,
    sources: tuple[q.AuditSourceDocument, ...],
    proposals: tuple[EvidenceSupportProposal, ...],
    new_id: Callable[[], UUID] = uuid4,
) -> q.ArtifactEvidenceSupportRequest:
    """Compute every identity and hash before an evaluator sees the request."""

    source_by_id = {item.source_id: item for item in sources}
    pairs = []
    for proposal in proposals:
        exact_claim = resolve_payload_pointer(payload, proposal.claim_pointer)
        if not isinstance(exact_claim, str) or not exact_claim.strip():
            raise ValueError("evidence-support claim pointer must resolve to exact text")
        source = source_by_id.get(proposal.source_id)
        if source is None:
            raise ValueError("evidence-support proposal references an unknown source")
        if proposal.exact_excerpt not in source.complete_text:
            raise ValueError("evidence-support excerpt is not an exact source substring")
        parent_pointer = proposal.claim_pointer.rsplit("/", 1)[0]
        parent = resolve_payload_pointer(payload, parent_pointer)
        if not isinstance(parent, dict) or parent.get("id") != str(proposal.claim_ref):
            raise ValueError("claim ref does not own the exact claim pointer")
        pairs.append(
            q.ExactEvidenceSupportPair(
                pair_id=new_id(),
                finding_id=proposal.finding_ref or new_id(),
                claim_ref=proposal.claim_ref,
                claim_pointer=proposal.claim_pointer,
                claim_hash=domain_hash("SPECOPS:ARTIFACT_CLAIM:v1", exact_claim),
                exact_claim=exact_claim,
                evidence_ref=proposal.evidence_ref or new_id(),
                source_id=proposal.source_id,
                source_hash=source.payload_hash,
                locator=proposal.locator,
                exact_excerpt=proposal.exact_excerpt,
                excerpt_hash="sha256:"
                + hashlib.sha256(proposal.exact_excerpt.encode("utf-8")).hexdigest(),
            )
        )
    base = {
        "protocol_version": q.PROTOCOL_VERSION,
        "request_id": new_id(),
        "evaluator_run_id": evaluator_run_id,
        "request_hash": "sha256:" + "0" * 64,
        "artifact_id": artifact_id,
        "artifact_version": artifact_version,
        "record_revision": record_revision,
        "payload_hash": payload_hash(payload),
        "pairs": tuple(pairs),
    }
    base["request_hash"] = domain_hash(
        "SPECOPS:ARTIFACT_EVIDENCE_SUPPORT_REQUEST:v1",
        {key: value for key, value in base.items() if key != "request_hash"},
    )
    return q.ArtifactEvidenceSupportRequest(**base)


def materialize_supported_evidence(
    *,
    payload: dict,
    request: q.ArtifactEvidenceSupportRequest,
    candidate: q.ArtifactEvidenceSupportCandidate,
    new_id: Callable[[], UUID] = uuid4,
) -> dict:
    """Materialize only exact SUPPORTS results; never accept provider hashes."""

    echoes = (
        candidate.request_id == request.request_id,
        candidate.evaluator_run_id == request.evaluator_run_id,
        candidate.request_hash == request.request_hash,
        candidate.artifact_id == request.artifact_id,
        candidate.artifact_version == request.artifact_version,
        candidate.record_revision == request.record_revision,
        candidate.payload_hash == request.payload_hash,
    )
    if not all(echoes):
        raise ValueError("evidence-support candidate binding changed")
    assessments = {item.pair_id: item for item in candidate.assessments}
    if set(assessments) != {item.pair_id for item in request.pairs}:
        raise ValueError("evidence-support candidate must assess every exact pair once")
    if any(
        item.assessment is not q.EvidenceSupportResult.SUPPORTS
        for item in assessments.values()
    ):
        raise ValueError("only exact SUPPORTS assessments may be materialized")

    revised = json.loads(json.dumps(payload))
    if revised.get("evidence_catalog") or revised.get("semantic_evidence_findings"):
        raise ValueError("evidence support may be materialized only once per exact revision")
    for pair in request.pairs:
        exact_claim = resolve_payload_pointer(revised, pair.claim_pointer)
        if (
            exact_claim != pair.exact_claim
            or domain_hash("SPECOPS:ARTIFACT_CLAIM:v1", exact_claim) != pair.claim_hash
        ):
            raise ValueError("artifact claim changed after evidence assessment began")
        revised["evidence_catalog"].append(
            {
                "id": str(pair.evidence_ref),
                "source_id": str(pair.source_id),
                "source_hash": pair.source_hash,
                "locator": pair.locator,
                "excerpt_hash": pair.excerpt_hash,
                "claim_refs": [str(pair.claim_ref)],
            }
        )
        assessment = assessments[pair.pair_id]
        revised["semantic_evidence_findings"].append(
            {
                "finding_id": str(pair.finding_id),
                "finding_version": 1,
                "claim_ref": str(pair.claim_ref),
                "claim_hash": pair.claim_hash,
                "evidence_ref": str(pair.evidence_ref),
                "source_hash": pair.source_hash,
                "excerpt_hash": pair.excerpt_hash,
                "assessment": assessment.assessment.value,
                "confidence": assessment.confidence,
                "analyzer_run_id": str(request.evaluator_run_id),
                "analyzer_contract_version": "1.0.0",
            }
        )
    return revised
