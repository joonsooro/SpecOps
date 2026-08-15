from copy import deepcopy
from uuid import UUID

import pytest

from specops_contracts import workshop_v1 as c
from specops_workflow.spec_identity_materialization import (
    SpecIdentityMaterializationError,
    materialize_spec_identities,
    validate_materialized_assignment,
)


HASH = "sha256:" + "1" * 64


def _slot(
    kind: str,
    ordinal: int,
    identity: str,
    *,
    owner: str = "ANALYZER",
    allocation_mode: str = "NEW_ENTITY",
):
    return c.ArtifactIdentitySlot(
        slot_key=f"{kind}:{ordinal:04d}",
        entity_kind=kind,
        ordinal=ordinal,
        owner=owner,
        foundation_id=UUID(identity),
        allocation_mode=allocation_mode,
    )


def _plan(*slots: c.ArtifactIdentitySlot) -> c.ArtifactSynthesisIdentityPlan:
    target = c.ArtifactDraftTarget(
        artifact_type="SPEC_PACKAGE",
        foundation_artifact_id=UUID("00000000-0000-4000-8000-000000000100"),
        artifact_key="SPEC-IDENTITY-MATERIALIZATION",
        next_artifact_version=1,
    )
    return c.ArtifactSynthesisIdentityPlan(
        identity_plan_id=UUID("00000000-0000-4000-8000-000000000101"),
        identity_plan_version=1,
        target=target,
        based_on_case_revision=4,
        semantic_state_hash=HASH,
        source_entity_refs=(),
        planned_identities=tuple(
            c.PlannedArtifactIdentity(
                foundation_id=item.foundation_id,
                foundation_version=1,
                entity_kind=item.entity_kind,
            )
            for item in slots
        ),
        slots=slots,
    )


def _payload() -> dict:
    return {
        "package_items": {},
        "glossary": {},
        "outcomes": {},
        "scope": {"in_scope": {}, "non_goals": {}, "boundaries": {}},
        "journeys": {},
        "behaviour_contract": {"always": {}, "ask_first": {}, "never": {}},
        "requirements": {},
        "data_rules": {},
        "experience_states": {},
        "scenarios": {},
        "quality_attributes": {},
        "constraints": {},
        "dependencies": {},
        "risks": {},
        "open_items": {},
        "acceptance_checks": {},
    }


def test_foundation_assigns_distinct_typed_ids_and_rewrites_local_graph():
    package = _slot(
        "PACKAGE_ITEM", 1, "00000000-0000-4000-8000-000000000110"
    )
    requirement_1 = _slot(
        "REQUIREMENT", 1, "00000000-0000-4000-8000-000000000111"
    )
    requirement_2 = _slot(
        "REQUIREMENT", 2, "00000000-0000-4000-8000-000000000112"
    )
    evidence = _slot(
        "EVIDENCE",
        1,
        "00000000-0000-4000-8000-000000000113",
        owner="FOUNDATION",
    )
    finding = _slot(
        "SEMANTIC_EVIDENCE_FINDING",
        1,
        "00000000-0000-4000-8000-000000000114",
        owner="FOUNDATION",
    )
    decision = _slot(
        "DECISION",
        1,
        "00000000-0000-4000-8000-000000000115",
        owner="FOUNDATION",
        allocation_mode="BOUND_EXISTING",
    )
    plan = _plan(
        package, requirement_1, requirement_2, evidence, finding, decision
    )
    payload = _payload()
    payload["package_items"] = {
        package.slot_key: {"requirement_refs": [requirement_2.slot_key]}
    }
    payload["requirements"] = {
        requirement_1.slot_key: {"title": "First"},
        requirement_2.slot_key: {"title": "Second"},
    }
    proposal = c.SpecEvidenceSupportProposalCandidate(
        evidence_ref=evidence.slot_key,
        finding_ref=finding.slot_key,
        claim_ref=requirement_2.slot_key,
        claim_pointer=f"/requirements/{requirement_2.slot_key}/title",
        source_role=c.SourceRole.PM_SPEC,
        locator="PM source line 1",
        exact_excerpt="Second",
    )

    replay_payload = deepcopy(payload)
    canonical, proposals, assignment_map = materialize_spec_identities(
        payload=payload,
        proposals=(proposal,),
        identity_plan=plan,
    )

    assert [item["id"] for item in canonical["requirements"]] == [
        str(requirement_1.foundation_id),
        str(requirement_2.foundation_id),
    ]
    assert canonical["package_items"][0]["requirement_refs"] == [
        str(requirement_2.foundation_id)
    ]
    assert canonical["requirements"][1]["id"] != str(decision.foundation_id)
    assert proposals[0].claim_ref == requirement_2.foundation_id
    assert proposals[0].claim_pointer == "/requirements/1/title"
    assert proposals[0].evidence_ref == evidence.foundation_id
    assert proposals[0].finding_ref == finding.foundation_id
    validate_materialized_assignment(
        payload=canonical, identity_plan=plan, assignment_map=assignment_map
    )
    replay_canonical, replay_proposals, replay_map = materialize_spec_identities(
        payload=replay_payload,
        proposals=(proposal,),
        identity_plan=plan,
    )
    assert replay_canonical == canonical
    assert replay_proposals == proposals
    assert replay_map == assignment_map


def test_foundation_fails_closed_on_slot_reuse_and_capacity_change():
    scope = _slot("SCOPE_ITEM", 1, "00000000-0000-4000-8000-000000000120")
    plan = _plan(scope)
    payload = _payload()
    payload["scope"]["in_scope"] = {scope.slot_key: {"statement": "Included"}}
    payload["scope"]["non_goals"] = {scope.slot_key: {"statement": "Excluded"}}

    with pytest.raises(SpecIdentityMaterializationError, match="reused"):
        materialize_spec_identities(payload=payload, proposals=(), identity_plan=plan)

    payload = _payload()
    payload["scope"]["in_scope"] = {}
    payload["scope"]["non_goals"] = {scope.slot_key: None}
    with pytest.raises(SpecIdentityMaterializationError, match="typed slot capacity"):
        materialize_spec_identities(payload=payload, proposals=(), identity_plan=plan)


def test_foundation_rejects_direct_new_entity_uuid_reference_bypass():
    package = _slot(
        "PACKAGE_ITEM", 1, "00000000-0000-4000-8000-000000000130"
    )
    requirement = _slot(
        "REQUIREMENT", 1, "00000000-0000-4000-8000-000000000131"
    )
    plan = _plan(package, requirement)
    payload = _payload()
    payload["package_items"] = {
        package.slot_key: {"requirement_refs": [str(requirement.foundation_id)]}
    }
    payload["requirements"] = {requirement.slot_key: {"title": "Bound"}}

    with pytest.raises(
        SpecIdentityMaterializationError, match="bypasses its local handle"
    ) as error:
        materialize_spec_identities(payload=payload, proposals=(), identity_plan=plan)
    assert error.value.pointers == ("/package_items/0/requirement_refs/0",)
