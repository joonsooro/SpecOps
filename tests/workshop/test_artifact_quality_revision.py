from __future__ import annotations

import json
from uuid import uuid4

import pytest

from specops_contracts import artifact_quality_v1 as q
from specops_workflow.artifact_quality_revision import (
    apply_quality_revision_candidate,
    prepare_quality_revision_request,
    quality_revision_pointer_closure,
    require_monotonic_quality_improvement,
)
from specops_workflow.workshop_protocol import WorkshopFoundationService
from v4_payload_factory import PayloadFactory


def test_bounded_revision_repairs_existing_payload_with_only_allocated_identities():
    payload = PayloadFactory().payload("spec-package-payload.schema.json")
    finding_id = uuid4()
    request = prepare_quality_revision_request(
        artifact_id=uuid4(),
        artifact_version=3,
        record_revision=2,
        based_on_case_revision=8,
        payload=payload,
        audit_id=uuid4(),
        finding_ids=(finding_id,),
        failed_rule_ids=("SPEC-Q-007",),
        canonical_artifact_pointers=("/requirements",),
        allocated_identity_kinds=("REQUIREMENT",),
    )
    replacement = [dict(item) for item in payload["requirements"]]
    added = dict(replacement[0])
    added["id"] = str(request.allocated_identities[0].foundation_id)
    added["title"] = "One separately testable obligation"
    replacement.append(added)
    candidate = q.ArtifactQualityRevisionCandidate(
        protocol_version="1.0.0",
        output_type="ARTIFACT_QUALITY_REVISION_CANDIDATE",
        revision_request_id=request.revision_request_id,
        revision_request_version=1,
        request_hash=request.request_hash,
        artifact_id=request.artifact_id,
        artifact_version=request.artifact_version,
        record_revision=request.record_revision,
        payload_hash=request.payload_hash,
        attempt=1,
        patches=(
            q.ArtifactRevisionPatch(
                pointer="/requirements",
                replacement_value_json=json.dumps(replacement),
            ),
        ),
    )

    revised = apply_quality_revision_candidate(
        payload=payload,
        request=request,
        candidate=candidate,
        identity_kinds=WorkshopFoundationService._artifact_identity_kinds,
    )

    assert len(revised["requirements"]) == len(payload["requirements"]) + 1
    assert revised["actors"] == payload["actors"]
    assert revised["decisions"] == payload["decisions"]


def test_bounded_revision_rejects_unscoped_patch_and_requires_monotonic_audit():
    payload = PayloadFactory().payload("spec-package-payload.schema.json")
    prior_finding = uuid4()
    request = prepare_quality_revision_request(
        artifact_id=uuid4(),
        artifact_version=1,
        record_revision=1,
        based_on_case_revision=8,
        payload=payload,
        audit_id=uuid4(),
        finding_ids=(prior_finding,),
        failed_rule_ids=("SPEC-Q-007",),
        canonical_artifact_pointers=("/requirements",),
        allocated_identity_kinds=(),
    )
    candidate = q.ArtifactQualityRevisionCandidate(
        protocol_version="1.0.0",
        output_type="ARTIFACT_QUALITY_REVISION_CANDIDATE",
        revision_request_id=request.revision_request_id,
        revision_request_version=1,
        request_hash=request.request_hash,
        artifact_id=request.artifact_id,
        artifact_version=request.artifact_version,
        record_revision=request.record_revision,
        payload_hash=request.payload_hash,
        attempt=1,
        patches=(
            q.ArtifactRevisionPatch(
                pointer="/product_thesis",
                replacement_value_json=json.dumps(payload["product_thesis"]),
            ),
        ),
    )
    with pytest.raises(ValueError, match="outside admitted"):
        apply_quality_revision_candidate(
            payload=payload,
            request=request,
            candidate=candidate,
            identity_kinds=WorkshopFoundationService._artifact_identity_kinds,
        )

    require_monotonic_quality_improvement(
        prior_finding_ids=(prior_finding,),
        prior_failed_rule_ids=("SPEC-Q-007",),
        revised_finding_ids=(),
        revised_failed_rule_ids=(),
    )
    with pytest.raises(ValueError, match="did not monotonically reduce"):
        require_monotonic_quality_improvement(
            prior_finding_ids=(prior_finding,),
            prior_failed_rule_ids=("SPEC-Q-007",),
            revised_finding_ids=(prior_finding, uuid4()),
            revised_failed_rule_ids=("SPEC-Q-007",),
        )


def test_actor_split_scope_is_reference_closed_without_decision_authority():
    payload = PayloadFactory().payload("spec-package-payload.schema.json")
    actor_id = payload["actors"][0]["id"]
    payload["requirements"][0]["actor_refs"] = [actor_id]
    payload["product_thesis"]["primary_customer"] = actor_id
    payload["decisions"] = [{"authority": {"actor_ref": actor_id}}]

    pointers = quality_revision_pointer_closure(
        payload=payload,
        finding_pointers=("/actors/0", "/requirements/0"),
        allocated_identity_kinds=("ACTOR",),
    )

    assert pointers == (
        "/actors",
        "/actors/0",
        "/product_thesis",
        "/requirements/0",
    )
    assert not any(pointer.startswith("/decisions") for pointer in pointers)


def test_actor_split_allows_only_one_exact_allocated_addition_and_preserves_authority():
    payload = PayloadFactory().payload("spec-package-payload.schema.json")
    request = prepare_quality_revision_request(
        artifact_id=uuid4(),
        artifact_version=3,
        record_revision=2,
        based_on_case_revision=8,
        payload=payload,
        audit_id=uuid4(),
        finding_ids=(uuid4(),),
        failed_rule_ids=("SPEC-Q-004",),
        canonical_artifact_pointers=("/actors",),
        allocated_identity_kinds=("ACTOR",),
    )
    actors = [dict(item) for item in payload["actors"]]
    added = dict(actors[0])
    added.update(
        {
            "id": str(request.allocated_identities[0].foundation_id),
            "name": "Tenant-scoped Operations Manager",
        }
    )
    actors.append(added)
    candidate = q.ArtifactQualityRevisionCandidate(
        protocol_version="1.0.0",
        output_type="ARTIFACT_QUALITY_REVISION_CANDIDATE",
        revision_request_id=request.revision_request_id,
        revision_request_version=1,
        request_hash=request.request_hash,
        artifact_id=request.artifact_id,
        artifact_version=request.artifact_version,
        record_revision=request.record_revision,
        payload_hash=request.payload_hash,
        attempt=1,
        patches=(
            q.ArtifactRevisionPatch(
                pointer="/actors",
                replacement_value_json=json.dumps(actors),
            ),
        ),
    )

    revised = apply_quality_revision_candidate(
        payload=payload,
        request=request,
        candidate=candidate,
        identity_kinds=WorkshopFoundationService._artifact_identity_kinds,
    )
    assert revised["actors"][:-1] == payload["actors"]
    assert revised["decisions"] == payload["decisions"]

    actors[0] = {**actors[0], "name": "Changed authority"}
    changed = candidate.model_copy(
        update={
            "patches": (
                q.ArtifactRevisionPatch(
                    pointer="/actors",
                    replacement_value_json=json.dumps(actors),
                ),
            )
        }
    )
    with pytest.raises(ValueError, match="existing Foundation-owned actor"):
        apply_quality_revision_candidate(
            payload=payload,
            request=request,
            candidate=changed,
            identity_kinds=WorkshopFoundationService._artifact_identity_kinds,
        )
