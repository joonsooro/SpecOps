from __future__ import annotations

import json
from uuid import uuid4

import pytest

from specops_contracts import artifact_quality_v1 as q
from specops_contracts.canonical import payload_hash
from specops_workflow.artifact_quality_foundation import (
    _confirmed_spec_payload_from_record,
)
from specops_workflow.artifact_quality_revision import (
    apply_quality_revision_candidate,
    normalize_quality_revision_candidate_wire,
    prepare_quality_revision_request,
    quality_revision_enum_constraints,
    quality_revision_pointer_closure,
    require_monotonic_quality_improvement,
)


def test_technical_split_allocations_open_only_their_canonical_collections():
    payload = {
        "data_contracts": [{"id": "00000000-0000-4000-8000-000000000001"}],
        "quality_budgets": [{"id": "00000000-0000-4000-8000-000000000002"}],
        "verification_plan": [{"id": "00000000-0000-4000-8000-000000000003"}],
    }

    pointers = quality_revision_pointer_closure(
        payload=payload,
        finding_pointers=("/data_contracts/0",),
        allocated_identity_kinds=(
            "DATA_CONTRACT",
            "QUALITY_BUDGET",
            "VERIFICATION_ITEM",
        ),
    )
    assert pointers == (
        "/data_contracts",
        "/data_contracts/0",
        "/quality_budgets",
        "/verification_plan",
    )


def test_technical_closure_revision_opens_nested_architecture_and_owned_records():
    payload = {
        "architecture_context": {
            "nodes": [{"id": "00000000-0000-4000-8000-000000000011"}]
        },
        "components": [{"id": "00000000-0000-4000-8000-000000000012"}],
        "interfaces": [{"id": "00000000-0000-4000-8000-000000000013"}],
    }

    pointers = quality_revision_pointer_closure(
        payload=payload,
        finding_pointers=(
            "/architecture_context/nodes/0",
            "/components/0",
            "/interfaces/0",
        ),
        allocated_identity_kinds=(
            "ARCHITECTURE_NODE",
            "COMPONENT",
            "INTERFACE",
        ),
    )

    assert pointers == (
        "/architecture_context",
        "/architecture_context/nodes",
        "/architecture_context/nodes/0",
        "/components",
        "/components/0",
        "/interfaces",
        "/interfaces/0",
    )


def test_governed_change_revision_opens_nested_control_and_audit_collections():
    payload = {
        "security_privacy_contract": {
            "controls": [{"id": "00000000-0000-4000-8000-000000000021"}]
        },
        "observability_audit": {
            "audit_records": [
                {"id": "00000000-0000-4000-8000-000000000022"}
            ]
        },
    }

    pointers = quality_revision_pointer_closure(
        payload=payload,
        finding_pointers=(
            "/security_privacy_contract/controls/0",
            "/observability_audit/audit_records/0",
        ),
        allocated_identity_kinds=("SECURITY_CONTROL", "AUDIT_RECORD"),
    )

    assert pointers == (
        "/observability_audit",
        "/observability_audit/audit_records",
        "/observability_audit/audit_records/0",
        "/security_privacy_contract",
        "/security_privacy_contract/controls",
        "/security_privacy_contract/controls/0",
    )


def test_revision_enum_constraints_resolve_reusable_artifact_definitions():
    schema = {
        "type": "object",
        "properties": {
            "interfaces": {
                "type": "array",
                "items": {"$ref": "#/$defs/interface"},
            }
        },
        "$defs": {
            "interface": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["api", "event", "internal"]},
                    "status": {"$ref": "#/$defs/status"},
                },
            },
            "status": {"type": "string", "enum": ["open", "closed"]},
        },
    }

    assert quality_revision_enum_constraints(schema) == {
        "/interfaces/*/kind": ("api", "event", "internal"),
        "/interfaces/*/status": ("open", "closed"),
    }


def test_technical_revision_recovers_only_hash_bound_confirmed_spec_lineage():
    spec_payload = PayloadFactory().payload("spec-package-payload.schema.json")
    binding = json.dumps(
        {
            "canonical_payload_json": json.dumps(spec_payload),
            "payload_hash": payload_hash(spec_payload),
        }
    )

    assert (
        _confirmed_spec_payload_from_record("TECHNICAL_CONTRACT", binding)
        == spec_payload
    )
    assert _confirmed_spec_payload_from_record("SPEC_PACKAGE", None) is None
    with pytest.raises(ValueError, match="hash changed"):
        _confirmed_spec_payload_from_record(
            "TECHNICAL_CONTRACT",
            json.dumps(
                {
                    "canonical_payload_json": json.dumps(spec_payload),
                    "payload_hash": "sha256:" + "0" * 64,
                }
            ),
        )


def test_wire_normalization_is_exactly_string_typed_and_focus_bounded():
    request_id = uuid4()
    artifact_id = uuid4()
    raw = json.dumps(
        {
            "protocol_version": "1.0.0",
            "output_type": "ARTIFACT_QUALITY_REVISION_CANDIDATE",
            "revision_request_id": str(request_id),
            "revision_request_version": 1,
            "request_hash": "sha256:" + "1" * 64,
            "artifact_id": str(artifact_id),
            "artifact_version": 1,
            "record_revision": 2,
            "payload_hash": "sha256:" + "2" * 64,
            "attempt": 1,
            "patches": [
                {"pointer": "/kept", "replacement_value_json": "plain text"},
                {"pointer": "/deferred/value", "replacement_value_json": "{}"},
            ],
        }
    )

    candidate, receipt = normalize_quality_revision_candidate_wire(
        raw_candidate_json=raw,
        payload={"kept": "old", "deferred": {"value": {}}},
        excluded_pointer_prefixes=("/deferred",),
    )

    assert candidate.patches[0].replacement_value_json == '"plain text"'
    assert receipt == {
        "excluded_pointers": ("/deferred/value",),
        "normalized_string_pointers": ("/kept",),
    }


def test_wire_normalization_rejects_ambiguous_non_string_content():
    raw = json.dumps(
        {
            "protocol_version": "1.0.0",
            "output_type": "ARTIFACT_QUALITY_REVISION_CANDIDATE",
            "revision_request_id": str(uuid4()),
            "revision_request_version": 1,
            "request_hash": "sha256:" + "1" * 64,
            "artifact_id": str(uuid4()),
            "artifact_version": 1,
            "record_revision": 2,
            "payload_hash": "sha256:" + "2" * 64,
            "attempt": 1,
            "patches": [
                {"pointer": "/value", "replacement_value_json": "not-json"}
            ],
        }
    )

    with pytest.raises(ValueError, match="ambiguous non-JSON"):
        normalize_quality_revision_candidate_wire(
            raw_candidate_json=raw,
            payload={"value": {}},
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


def test_bounded_revision_treats_allocated_identities_as_a_capacity_ceiling():
    payload = PayloadFactory().payload("spec-package-payload.schema.json")
    request = prepare_quality_revision_request(
        artifact_id=uuid4(),
        artifact_version=3,
        record_revision=2,
        based_on_case_revision=8,
        payload=payload,
        audit_id=uuid4(),
        finding_ids=(uuid4(),),
        failed_rule_ids=("SPEC-Q-007",),
        canonical_artifact_pointers=("/requirements",),
        allocated_identity_kinds=("REQUIREMENT", "REQUIREMENT"),
    )
    replacement = [dict(item) for item in payload["requirements"]]
    added = dict(replacement[0])
    added["id"] = str(request.allocated_identities[0].foundation_id)
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

    assert revised["requirements"][-1]["id"] == str(
        request.allocated_identities[0].foundation_id
    )
    assert str(request.allocated_identities[1].foundation_id) not in json.dumps(revised)


@pytest.mark.parametrize("mode", ("unallocated", "wrong_kind"))
def test_revision_capacity_ceiling_still_rejects_unallocated_or_wrong_kind_ids(mode):
    payload = PayloadFactory().payload("spec-package-payload.schema.json")
    collection = "requirements" if mode == "unallocated" else "acceptance_checks"
    request = prepare_quality_revision_request(
        artifact_id=uuid4(),
        artifact_version=3,
        record_revision=2,
        based_on_case_revision=8,
        payload=payload,
        audit_id=uuid4(),
        finding_ids=(uuid4(),),
        failed_rule_ids=("SPEC-Q-007",),
        canonical_artifact_pointers=(f"/{collection}",),
        allocated_identity_kinds=("REQUIREMENT",),
    )
    replacement = [dict(item) for item in payload[collection]]
    added = dict(replacement[0])
    added["id"] = (
        str(uuid4())
        if mode == "unallocated"
        else str(request.allocated_identities[0].foundation_id)
    )
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
                pointer=f"/{collection}",
                replacement_value_json=json.dumps(replacement),
            ),
        ),
    )

    with pytest.raises(ValueError, match="allocation ceiling"):
        apply_quality_revision_candidate(
            payload=payload,
            request=request,
            candidate=candidate,
            identity_kinds=WorkshopFoundationService._artifact_identity_kinds,
        )


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


def test_requirement_split_scope_closes_requirement_and_acceptance_references():
    payload = PayloadFactory().payload("spec-package-payload.schema.json")
    requirement_id = payload["requirements"][0]["id"]
    acceptance_id = payload["acceptance_checks"][0]["id"]
    payload["requirements"][0]["acceptance_check_refs"] = [acceptance_id]
    payload["acceptance_checks"][0]["requirement_refs"] = [requirement_id]
    payload["package_items"][0]["requirement_refs"] = [requirement_id]
    payload["package_items"][0]["acceptance_check_refs"] = [acceptance_id]
    payload["decisions"] = [{"affected_refs": [requirement_id]}]

    pointers = quality_revision_pointer_closure(
        payload=payload,
        finding_pointers=(
            "/requirements/0/behaviour",
            "/acceptance_checks/0/then/0",
            "/glossary",
        ),
        allocated_identity_kinds=(
            "REQUIREMENT",
            "REQUIREMENT",
            "ACCEPTANCE_CHECK",
            "GLOSSARY_TERM",
        ),
    )

    assert "/requirements" in pointers
    assert "/acceptance_checks" in pointers
    assert "/glossary" in pointers
    assert "/package_items/0" in pointers
    assert "/traceability" in pointers
    assert not any(pointer.startswith("/decisions") for pointer in pointers)


def test_experience_and_scenario_additions_are_pointer_and_trace_closed():
    payload = PayloadFactory().payload("spec-package-payload.schema.json")

    pointers = quality_revision_pointer_closure(
        payload=payload,
        finding_pointers=("/data_rules/0", "/experience_states"),
        allocated_identity_kinds=(
            "EXPERIENCE_STATE",
            "SCENARIO",
            "ACCEPTANCE_CHECK",
        ),
    )

    assert "/experience_states" in pointers
    assert "/scenarios" in pointers
    assert "/acceptance_checks" in pointers
    assert "/traceability" in pointers
    assert "/package_items/0" in pointers
    assert not any(pointer.startswith("/decisions") for pointer in pointers)


def test_exact_found_actor_responsibility_may_change_but_actor_role_may_not():
    payload = PayloadFactory().payload("spec-package-payload.schema.json")
    payload["actors"][0]["responsibilities"] = ["Old scope wording"]
    request = prepare_quality_revision_request(
        artifact_id=uuid4(),
        artifact_version=3,
        record_revision=2,
        based_on_case_revision=8,
        payload=payload,
        audit_id=uuid4(),
        finding_ids=(uuid4(),),
        failed_rule_ids=("SPEC-Q-005",),
        canonical_artifact_pointers=("/actors/0/responsibilities/0",),
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
                pointer="/actors/0/responsibilities/0",
                replacement_value_json=json.dumps("Exact fingerprint comparison"),
            ),
        ),
    )

    revised = apply_quality_revision_candidate(
        payload=payload,
        request=request,
        candidate=candidate,
        identity_kinds=WorkshopFoundationService._artifact_identity_kinds,
    )
    assert revised["actors"][0]["responsibilities"] == [
        "Exact fingerprint comparison"
    ]
    assert revised["actors"][0]["id"] == payload["actors"][0]["id"]

    changed_role = candidate.model_copy(
        update={
            "patches": (
                q.ArtifactRevisionPatch(
                    pointer="/actors/0/responsibilities/0",
                    replacement_value_json=json.dumps("Exact fingerprint comparison"),
                ),
                q.ArtifactRevisionPatch(
                    pointer="/actors/0/name",
                    replacement_value_json=json.dumps("Changed role"),
                ),
            )
        }
    )
    with pytest.raises(ValueError, match="outside admitted"):
        apply_quality_revision_candidate(
            payload=payload,
            request=request,
            candidate=changed_role,
            identity_kinds=WorkshopFoundationService._artifact_identity_kinds,
        )
