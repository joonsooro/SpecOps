from __future__ import annotations

import hashlib
from uuid import UUID, uuid4

import pytest

from specops_contracts import artifact_quality_v1 as q
from specops_contracts.canonical import domain_hash, payload_hash
from specops_workflow.artifact_evidence_support import (
    EvidenceSupportProposal,
    materialize_supported_evidence,
    normalize_evidence_claim_pointers,
    prepare_evidence_support_request,
    project_evidence_catalog,
)
from specops_workshop.v4.schema_compiler import artifact_evidence_support_native_schema
from v4_payload_factory import PayloadFactory


def _source(text: str) -> q.AuditSourceDocument:
    return q.AuditSourceDocument(
        source_id=UUID("70000000-0000-4000-8000-000000000001"),
        role=q.SourceRole.PM_SPEC,
        version=1,
        payload_hash="sha256:" + hashlib.sha256(text.encode()).hexdigest(),
        canonical_locator="/fixture/pm.md",
        filename="pm.md",
        media_type="text/markdown",
        complete_text=text,
    )


def test_foundation_computes_exact_claim_and_evidence_hashes_before_assessment():
    payload = PayloadFactory().payload("spec-package-payload.schema.json")
    payload["requirements"][0]["source_evidence_refs"] = []
    claim = payload["requirements"][0]["behaviour"]
    claim_ref = UUID(payload["requirements"][0]["id"])
    source = _source("The export includes every accepted snapshot row.\n")
    excerpt = "The export includes every accepted snapshot row."
    proposal = EvidenceSupportProposal(
        claim_ref=claim_ref,
        claim_pointer="/requirements/0/behaviour",
        source_id=source.source_id,
        locator="line:1",
        exact_excerpt=excerpt,
        evidence_ref=uuid4(),
        finding_ref=uuid4(),
    )
    payload, accepted, dropped = project_evidence_catalog(
        payload=payload, sources=(source,), proposals=(proposal,)
    )
    assert accepted == (proposal.evidence_ref,)
    assert dropped == ()
    assert payload["requirements"][0]["source_evidence_refs"] == [
        str(proposal.evidence_ref)
    ]
    request = prepare_evidence_support_request(
        evaluator_run_id=uuid4(),
        artifact_id=uuid4(),
        artifact_version=2,
        record_revision=3,
        payload=payload,
        sources=(source,),
        proposals=(proposal,),
    )

    pair = request.pairs[0]
    assert pair.exact_claim == claim
    assert pair.claim_hash == domain_hash("SPECOPS:ARTIFACT_CLAIM:v1", claim)
    assert pair.source_hash == source.payload_hash
    assert pair.excerpt_hash == "sha256:" + hashlib.sha256(excerpt.encode()).hexdigest()
    assert request.payload_hash == payload_hash(payload)

    candidate = q.ArtifactEvidenceSupportCandidate(
        protocol_version="1.0.0",
        output_type="ARTIFACT_EVIDENCE_SUPPORT_CANDIDATE",
        request_id=request.request_id,
        evaluator_run_id=request.evaluator_run_id,
        request_hash=request.request_hash,
        artifact_id=request.artifact_id,
        artifact_version=request.artifact_version,
        record_revision=request.record_revision,
        payload_hash=request.payload_hash,
        assessments=(
            q.EvidenceSupportAssessment(
                pair_id=pair.pair_id,
                assessment=q.EvidenceSupportResult.SUPPORTS,
                confidence=0.99,
            ),
        ),
    )
    revised = materialize_supported_evidence(
        payload=payload, request=request, candidate=candidate
    )
    assert revised["evidence_catalog"][0]["excerpt_hash"] == pair.excerpt_hash
    assert revised["semantic_evidence_findings"][0]["claim_hash"] == pair.claim_hash
    assert revised["semantic_evidence_findings"][0]["assessment"] == "SUPPORTS"
    schema = artifact_evidence_support_native_schema()
    assert schema["properties"]["output_type"]["const"] == (
        "ARTIFACT_EVIDENCE_SUPPORT_CANDIDATE"
    )
    assert "claim_hash" not in schema["properties"]


@pytest.mark.parametrize(
    "quarantined_result",
    (
        q.EvidenceSupportResult.INSUFFICIENT,
        q.EvidenceSupportResult.CONTRADICTS,
        q.EvidenceSupportResult.AMBIGUOUS,
    ),
)
def test_evidence_policy_keeps_suggestions_quarantines_negative_results_and_rejects_changed_claim(
    quarantined_result,
):
    payload = PayloadFactory().payload("spec-package-payload.schema.json")
    payload["requirements"][0]["source_evidence_refs"] = []
    source = _source("Exact source statement.\n")
    proposal = EvidenceSupportProposal(
        claim_ref=UUID(payload["requirements"][0]["id"]),
        claim_pointer="/requirements/0/behaviour",
        source_id=source.source_id,
        locator="line:1",
        exact_excerpt="Exact source statement.",
        evidence_ref=uuid4(),
        finding_ref=uuid4(),
    )
    payload, _, _ = project_evidence_catalog(
        payload=payload, sources=(source,), proposals=(proposal,)
    )
    request = prepare_evidence_support_request(
        evaluator_run_id=uuid4(),
        artifact_id=uuid4(),
        artifact_version=1,
        record_revision=1,
        payload=payload,
        sources=(source,),
        proposals=(proposal,),
    )
    pair = request.pairs[0]
    candidate = q.ArtifactEvidenceSupportCandidate(
        protocol_version="1.0.0",
        output_type="ARTIFACT_EVIDENCE_SUPPORT_CANDIDATE",
        request_id=request.request_id,
        evaluator_run_id=request.evaluator_run_id,
        request_hash=request.request_hash,
        artifact_id=request.artifact_id,
        artifact_version=request.artifact_version,
        record_revision=request.record_revision,
        payload_hash=request.payload_hash,
        assessments=(
            q.EvidenceSupportAssessment(
                pair_id=pair.pair_id,
                assessment=quarantined_result,
                confidence=0.8,
            ),
        ),
    )
    insufficient = materialize_supported_evidence(
        payload=payload, request=request, candidate=candidate
    )
    assert insufficient["evidence_catalog"] == []
    assert insufficient["semantic_evidence_findings"] == []
    assert insufficient["requirements"][0]["source_evidence_refs"] == []

    suggestion = candidate.model_copy(
        update={
            "assessments": (
                q.EvidenceSupportAssessment(
                    pair_id=pair.pair_id,
                    assessment=q.EvidenceSupportResult.SUGGESTS,
                    confidence=0.81,
                ),
            )
        }
    )
    suggested = materialize_supported_evidence(
        payload=payload, request=request, candidate=suggestion
    )
    assert suggested["evidence_catalog"][0]["id"] == str(pair.evidence_ref)
    assert suggested["semantic_evidence_findings"][0]["assessment"] == "SUGGESTS"
    assert suggested["requirements"][0]["source_evidence_refs"] == [
        str(pair.evidence_ref)
    ]

    changed = {**payload}
    changed["requirements"] = [dict(item) for item in payload["requirements"]]
    changed["requirements"][0]["behaviour"] = "Changed after assessment."
    support = candidate.model_copy(
        update={
            "assessments": (
                q.EvidenceSupportAssessment(
                    pair_id=pair.pair_id,
                    assessment=q.EvidenceSupportResult.SUPPORTS,
                    confidence=0.99,
                ),
            )
        }
    )
    with pytest.raises(ValueError, match="claim changed"):
        materialize_supported_evidence(
            payload=changed, request=request, candidate=support
        )


def test_false_exact_quote_is_pruned_without_changing_claim_or_decision_binding():
    payload = PayloadFactory().payload("spec-package-payload.schema.json")
    claim = payload["requirements"][0]
    source = _source("A different exact source sentence.\n")
    evidence_ref = uuid4()
    finding_ref = uuid4()
    claim["source_evidence_refs"] = []
    decision_refs = list(claim["decision_refs"])

    revised, accepted, dropped = project_evidence_catalog(
        payload=payload,
        sources=(source,),
        proposals=(
            EvidenceSupportProposal(
                claim_ref=UUID(claim["id"]),
                claim_pointer="/requirements/0/behaviour",
                source_id=source.source_id,
                locator="line:1",
                exact_excerpt="Invented source sentence.",
                evidence_ref=evidence_ref,
                finding_ref=finding_ref,
            ),
        ),
    )

    assert accepted == ()
    assert dropped == (evidence_ref,)
    assert revised["requirements"][0]["behaviour"] == claim["behaviour"]
    assert revised["requirements"][0]["decision_refs"] == decision_refs
    assert revised["requirements"][0]["source_evidence_refs"] == []
    assert revised["evidence_catalog"] == []


def test_untrusted_embedded_evidence_link_is_rejected_before_quote_projection():
    payload = PayloadFactory().payload("spec-package-payload.schema.json")
    claim = payload["requirements"][0]
    source = _source("Exact source statement.\n")
    claim["source_evidence_refs"] = [str(uuid4())]

    with pytest.raises(ValueError, match="not Foundation-initialized"):
        project_evidence_catalog(
            payload=payload,
            sources=(source,),
            proposals=(
                EvidenceSupportProposal(
                    claim_ref=UUID(claim["id"]),
                    claim_pointer="/requirements/0/behaviour",
                    source_id=source.source_id,
                    locator="line:1",
                    exact_excerpt="Exact source statement.",
                    evidence_ref=uuid4(),
                    finding_ref=uuid4(),
                ),
            ),
        )


def test_nested_text_pointer_is_owned_by_nearest_identity_bearing_record():
    payload = PayloadFactory().payload("spec-package-payload.schema.json")
    claim = payload["requirements"][0]
    claim["exclusions"] = ["Pagination is a display concern only."]
    claim["source_evidence_refs"] = []
    source = _source("Pagination is a display concern only.\n")
    proposal = EvidenceSupportProposal(
        claim_ref=UUID(claim["id"]),
        claim_pointer="/requirements/0/exclusions/0",
        source_id=source.source_id,
        locator="line:1",
        exact_excerpt="Pagination is a display concern only.",
        evidence_ref=uuid4(),
        finding_ref=uuid4(),
    )

    revised, accepted, dropped = project_evidence_catalog(
        payload=payload, sources=(source,), proposals=(proposal,)
    )
    request = prepare_evidence_support_request(
        evaluator_run_id=uuid4(),
        artifact_id=uuid4(),
        artifact_version=1,
        record_revision=1,
        payload=revised,
        sources=(source,),
        proposals=(proposal,),
    )

    assert accepted == (proposal.evidence_ref,)
    assert dropped == ()
    assert revised["requirements"][0]["source_evidence_refs"] == [
        str(proposal.evidence_ref)
    ]
    assert request.pairs[0].exact_claim == claim["exclusions"][0]


def test_nested_text_pointer_rejects_a_different_record_identity():
    payload = PayloadFactory().payload("spec-package-payload.schema.json")
    claim = payload["requirements"][0]
    claim["exclusions"] = ["Pagination is a display concern only."]
    claim["source_evidence_refs"] = []
    source = _source("Pagination is a display concern only.\n")

    with pytest.raises(ValueError, match="claim ref does not own"):
        project_evidence_catalog(
            payload=payload,
            sources=(source,),
            proposals=(
                EvidenceSupportProposal(
                    claim_ref=uuid4(),
                    claim_pointer="/requirements/0/exclusions/0",
                    source_id=source.source_id,
                    locator="line:1",
                    exact_excerpt="Pagination is a display concern only.",
                    evidence_ref=uuid4(),
                    finding_ref=uuid4(),
                ),
            ),
        )


@pytest.mark.parametrize(
    ("collection_path", "claim_field"),
    (
        (("requirements", "0"), "behaviour"),
        (("behaviour_contract", "never", "0"), "obligation"),
        (("scope", "non_goals", "0"), "statement"),
    ),
)
def test_exact_owner_pointer_uses_only_its_schema_owned_primary_text_field(
    collection_path, claim_field
):
    payload = PayloadFactory().payload("spec-package-payload.schema.json")
    value = payload
    for part in collection_path:
        value = value[int(part)] if isinstance(value, list) else value[part]
    pointer = "/" + "/".join(collection_path)
    proposal = EvidenceSupportProposal(
        claim_ref=UUID(value["id"]),
        claim_pointer=pointer,
        source_id=uuid4(),
        locator="line:1",
        exact_excerpt="Exact source statement.",
        evidence_ref=uuid4(),
        finding_ref=uuid4(),
    )

    normalized = normalize_evidence_claim_pointers(
        payload=payload,
        proposals=(proposal,),
    )

    assert normalized[0].claim_pointer == f"{pointer}/{claim_field}"


def test_object_pointer_fails_closed_when_primary_text_field_is_ambiguous():
    payload = PayloadFactory().payload("spec-package-payload.schema.json")
    boundary = {
        "id": str(uuid4()),
        "dimension": "release boundary",
        "inside": "CSV export",
        "outside": "other formats",
        "evidence_refs": [],
    }
    payload["scope"]["boundaries"] = [boundary]

    with pytest.raises(ValueError, match="no unambiguous primary text field"):
        normalize_evidence_claim_pointers(
            payload=payload,
            proposals=(
                EvidenceSupportProposal(
                    claim_ref=UUID(boundary["id"]),
                    claim_pointer="/scope/boundaries/0",
                    source_id=uuid4(),
                    locator="line:1",
                    exact_excerpt="Exact source statement.",
                    evidence_ref=uuid4(),
                    finding_ref=uuid4(),
                ),
            ),
        )


def test_object_pointer_fails_closed_when_claim_ref_does_not_own_it():
    payload = PayloadFactory().payload("spec-package-payload.schema.json")

    with pytest.raises(ValueError, match="must resolve to exact text"):
        normalize_evidence_claim_pointers(
            payload=payload,
            proposals=(
                EvidenceSupportProposal(
                    claim_ref=uuid4(),
                    claim_pointer="/requirements/0",
                    source_id=uuid4(),
                    locator="line:1",
                    exact_excerpt="Exact source statement.",
                    evidence_ref=uuid4(),
                    finding_ref=uuid4(),
                ),
            ),
        )
