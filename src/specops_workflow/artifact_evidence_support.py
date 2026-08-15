"""Foundation-owned exact-claim evidence binding for SPEC-Q-026."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import Callable
from uuid import UUID, uuid4

from specops_contracts import artifact_quality_v1 as q
from specops_contracts.canonical import domain_hash, payload_hash
from specops_workshop.v4.artifact_quality import resolve_payload_pointer

from .spec_identity_materialization import SPEC_EVIDENCE_CLAIM_TEXT_FIELDS


@dataclass(frozen=True)
class EvidenceSupportProposal:
    claim_ref: UUID
    claim_pointer: str
    source_id: UUID
    locator: str
    exact_excerpt: str
    evidence_ref: UUID | None = None
    finding_ref: UUID | None = None


def normalize_evidence_claim_pointers(
    *,
    payload: dict,
    proposals: tuple[EvidenceSupportProposal, ...],
) -> tuple[EvidenceSupportProposal, ...]:
    """Resolve an exact owner pointer only when its primary text leaf is unique."""

    normalized: list[EvidenceSupportProposal] = []
    for proposal in proposals:
        value = resolve_payload_pointer(payload, proposal.claim_pointer)
        if isinstance(value, str) and value.strip():
            normalized.append(proposal)
            continue
        if not isinstance(value, dict) or value.get("id") != str(proposal.claim_ref):
            raise ValueError("evidence-support claim pointer must resolve to exact text")
        pointer_parts = tuple(
            part.replace("~1", "/").replace("~0", "~")
            for part in proposal.claim_pointer.split("/")[1:]
        )
        collection_path = pointer_parts[:-1]
        fields = SPEC_EVIDENCE_CLAIM_TEXT_FIELDS.get(collection_path)
        if fields is None or len(fields) != 1:
            raise ValueError(
                "evidence-support object pointer has no unambiguous primary text field"
            )
        field = fields[0]
        exact_claim = value.get(field)
        if not isinstance(exact_claim, str) or not exact_claim.strip():
            raise ValueError("evidence-support claim pointer must resolve to exact text")
        normalized.append(
            replace(
                proposal,
                claim_pointer=f"{proposal.claim_pointer}/{field}",
            )
        )
    return tuple(normalized)


def _resolve_claim_owner(
    payload: dict, claim_pointer: str, claim_ref: UUID
) -> tuple[str, dict]:
    """Resolve the nearest identity-bearing object that owns a text pointer."""

    owner_pointer = claim_pointer.rsplit("/", 1)[0]
    while owner_pointer:
        candidate = resolve_payload_pointer(payload, owner_pointer)
        if isinstance(candidate, dict) and "id" in candidate:
            if candidate["id"] != str(claim_ref):
                raise ValueError("claim ref does not own the exact claim pointer")
            return owner_pointer, candidate
        owner_pointer = owner_pointer.rsplit("/", 1)[0]
    raise ValueError("claim ref does not own the exact claim pointer")


def project_evidence_catalog(
    *,
    payload: dict,
    sources: tuple[q.AuditSourceDocument, ...],
    proposals: tuple[EvidenceSupportProposal, ...],
) -> tuple[dict, tuple[UUID, ...], tuple[UUID, ...]]:
    """Materialize exact evidence identities before reference-graph admission.

    False source quotations are removed as evidence links only. Their claims and
    canonical decision bindings remain unchanged, so quality evaluation can
    honestly identify missing source support without admitting fabricated proof.
    """

    revised = json.loads(json.dumps(payload))
    proposals = normalize_evidence_claim_pointers(
        payload=revised,
        proposals=proposals,
    )
    if revised.get("evidence_catalog") or revised.get("semantic_evidence_findings"):
        raise ValueError("evidence catalog projection requires empty server-owned fields")
    evidence_ids = [item.evidence_ref for item in proposals]
    finding_ids = [item.finding_ref for item in proposals]
    if None in evidence_ids or len(evidence_ids) != len(set(evidence_ids)):
        raise ValueError("evidence proposal identities must be present and unique")
    if None in finding_ids or len(finding_ids) != len(set(finding_ids)):
        raise ValueError("evidence finding identities must be present and unique")

    source_by_id = {item.source_id: item for item in sources}
    accepted: list[UUID] = []
    dropped: list[UUID] = []
    initialized_claims: set[tuple[str, str]] = set()
    for proposal in proposals:
        exact_claim = resolve_payload_pointer(revised, proposal.claim_pointer)
        if not isinstance(exact_claim, str) or not exact_claim.strip():
            raise ValueError("evidence-support claim pointer must resolve to exact text")
        parent_pointer, parent = _resolve_claim_owner(
            revised, proposal.claim_pointer, proposal.claim_ref
        )
        evidence_field = (
            "source_evidence_refs"
            if "source_evidence_refs" in parent
            else "evidence_refs"
            if "evidence_refs" in parent
            else None
        )
        if evidence_field is None or not isinstance(parent[evidence_field], list):
            raise ValueError("evidence-support claim owner lacks a canonical evidence field")
        claim_key = (str(proposal.claim_ref), parent_pointer)
        if claim_key not in initialized_claims:
            if parent[evidence_field]:
                raise ValueError("evidence-support claim field was not Foundation-initialized")
            initialized_claims.add(claim_key)
        source = source_by_id.get(proposal.source_id)
        if source is None:
            raise ValueError("evidence-support proposal references an unknown source")
        assert proposal.evidence_ref is not None
        if proposal.exact_excerpt not in source.complete_text:
            dropped.append(proposal.evidence_ref)
            continue
        revised["evidence_catalog"].append(
            {
                "id": str(proposal.evidence_ref),
                "source_id": str(proposal.source_id),
                "source_hash": source.payload_hash,
                "locator": proposal.locator,
                "excerpt_hash": "sha256:"
                + hashlib.sha256(proposal.exact_excerpt.encode("utf-8")).hexdigest(),
                "claim_refs": [str(proposal.claim_ref)],
            }
        )
        parent[evidence_field].append(str(proposal.evidence_ref))
        accepted.append(proposal.evidence_ref)
    return revised, tuple(accepted), tuple(dropped)


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

    proposals = normalize_evidence_claim_pointers(
        payload=payload,
        proposals=proposals,
    )
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
        _resolve_claim_owner(payload, proposal.claim_pointer, proposal.claim_ref)
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


CANONICAL_EVIDENCE_RESULTS = frozenset(
    (q.EvidenceSupportResult.SUPPORTS, q.EvidenceSupportResult.SUGGESTS)
)


def evidence_assessment_disposition(
    result: q.EvidenceSupportResult,
) -> str:
    """Map an exact assessment to its V0 canonical or quarantine disposition."""

    return "CANONICAL" if result in CANONICAL_EVIDENCE_RESULTS else "QUARANTINED"


def _remove_quarantined_evidence_refs(value, quarantined: set[str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"evidence_refs", "source_evidence_refs"} and isinstance(item, list):
                value[key] = [reference for reference in item if reference not in quarantined]
            else:
                _remove_quarantined_evidence_refs(item, quarantined)
    elif isinstance(value, list):
        for item in value:
            _remove_quarantined_evidence_refs(item, quarantined)


def materialize_supported_evidence(
    *,
    payload: dict,
    request: q.ArtifactEvidenceSupportRequest,
    candidate: q.ArtifactEvidenceSupportCandidate,
    new_id: Callable[[], UUID] = uuid4,
) -> dict:
    """Materialize relevant exact pairs; quarantine all other assessment results."""

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
    revised = json.loads(json.dumps(payload))
    if revised.get("semantic_evidence_findings"):
        raise ValueError("evidence support may be materialized only once per exact revision")
    expected_catalog = [
        {
            "id": str(pair.evidence_ref),
            "source_id": str(pair.source_id),
            "source_hash": pair.source_hash,
            "locator": pair.locator,
            "excerpt_hash": pair.excerpt_hash,
            "claim_refs": [str(pair.claim_ref)],
        }
        for pair in request.pairs
    ]
    if revised.get("evidence_catalog") != expected_catalog:
        raise ValueError("pre-admission evidence catalog changed")
    for pair in request.pairs:
        exact_claim = resolve_payload_pointer(revised, pair.claim_pointer)
        if (
            exact_claim != pair.exact_claim
            or domain_hash("SPECOPS:ARTIFACT_CLAIM:v1", exact_claim) != pair.claim_hash
        ):
            raise ValueError("artifact claim changed after evidence assessment began")
    quarantined = {
        str(pair.evidence_ref)
        for pair in request.pairs
        if evidence_assessment_disposition(assessments[pair.pair_id].assessment)
        == "QUARANTINED"
    }
    revised["evidence_catalog"] = [
        item for item in revised["evidence_catalog"] if item["id"] not in quarantined
    ]
    _remove_quarantined_evidence_refs(revised, quarantined)

    for pair in request.pairs:
        assessment = assessments[pair.pair_id]
        if evidence_assessment_disposition(assessment.assessment) == "QUARANTINED":
            continue
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
