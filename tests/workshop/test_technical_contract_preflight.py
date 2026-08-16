from copy import deepcopy
import json
from uuid import UUID

import pytest
from specops_contracts import workshop_v1 as c

from specops_workflow.technical_contract_preflight import (
    build_technical_closure_manifest,
    technical_contract_preflight,
)
from specops_workshop.v4.schema_compiler import (
    artifact_quality_revision_native_schema,
    validate_openai_strict_schema,
)


def _spec():
    return {
        "requirements": [{"id": "00000000-0000-4000-8000-000000000101"}],
        "data_rules": [{"id": "00000000-0000-4000-8000-000000000102"}],
        "acceptance_checks": [{"id": "00000000-0000-4000-8000-000000000103"}],
        "quality_attributes": [{"id": "00000000-0000-4000-8000-000000000104"}],
        "behaviour_contract": {
            "always": [{"id": "00000000-0000-4000-8000-000000000105"}],
            "ask_first": [],
            "never": [{"id": "00000000-0000-4000-8000-000000000106"}],
        },
    }


def _technical():
    component_id = "00000000-0000-4000-8000-000000000201"
    interface_id = "00000000-0000-4000-8000-000000000202"
    data_contract_id = "00000000-0000-4000-8000-000000000203"
    failure_id = "00000000-0000-4000-8000-000000000204"
    return {
        "architecture_context": {
            "nodes": [
                {
                    "id": "00000000-0000-4000-8000-000000000205",
                    "kind": "service",
                    "technical_ref": component_id,
                    "inside_system_boundary": True,
                }
            ],
            "interactions": [],
        },
        "components": [
            {
                "id": component_id,
                "provides_interface_refs": [interface_id],
                "consumes_interface_refs": [interface_id],
                "owns_data_refs": [data_contract_id],
                "dependency_refs": [],
                "implements_spec_refs": [
                    "00000000-0000-4000-8000-000000000101",
                    "00000000-0000-4000-8000-000000000105",
                    "00000000-0000-4000-8000-000000000106",
                ],
            }
        ],
        "data_contracts": [
            {
                "id": data_contract_id,
                "name": "canonical-payload.v1",
                "owner_component_ref": component_id,
                "implements_spec_refs": [
                    "00000000-0000-4000-8000-000000000102"
                ],
            }
        ],
        "interfaces": [
            {
                "id": interface_id,
                "producer_ref": component_id,
                "consumer_refs": [component_id],
                "input": {"schema_ref": "canonical-payload.v1"},
                "output": {"schema_ref": data_contract_id},
            }
        ],
        "failure_contracts": [
            {"id": failure_id, "owner_component_ref": component_id}
        ],
        "workflows": [
            {
                "initial_state": "ready",
                "trigger_interface_ref": interface_id,
                "states": [
                    {"name": "ready", "terminal": False},
                    {"name": "complete", "terminal": True},
                    {"name": "failed", "terminal": True},
                ],
                "transitions": [
                    {
                        "from": "ready",
                        "to": "complete",
                        "failure_ref": None,
                    },
                    {
                        "from": "ready",
                        "to": "failed",
                        "failure_ref": failure_id,
                    },
                ],
            }
        ],
        "verification_plan": [
            {
                "id": "00000000-0000-4000-8000-000000000206",
                "covers_acceptance_refs": [
                    "00000000-0000-4000-8000-000000000103"
                ],
            }
        ],
        "quality_budgets": [
            {
                "spec_quality_ref": "00000000-0000-4000-8000-000000000104",
                "verification_refs": [
                    "00000000-0000-4000-8000-000000000206"
                ],
            }
        ],
        "substrate_dependencies": [],
        "build_units": [{"technical_refs": [component_id]}],
        "rollout_migration_recovery": {"owner": "Delivery owner"},
        "engineering_decisions": [],
        "review_obligations": [],
    }


def _with_governed_change_bundle():
    spec = _spec()
    technical = _technical()
    ask_ref = "00000000-0000-4000-8000-000000000107"
    spec["behaviour_contract"]["ask_first"] = [{"id": ask_ref}]

    component = technical["components"][0]
    interface = technical["interfaces"][0]
    data_contract = technical["data_contracts"][0]
    workflow = technical["workflows"][0]
    verification = technical["verification_plan"][0]
    workflow_id = "00000000-0000-4000-8000-000000000207"
    control_id = "00000000-0000-4000-8000-000000000208"
    audit_id = "00000000-0000-4000-8000-000000000209"

    component["implements_spec_refs"].append(ask_ref)
    data_contract["implements_spec_refs"].append(ask_ref)
    interface["implements_spec_refs"] = [ask_ref]
    interface["errors"] = [{"code": "APPROVAL_REQUIRED"}]
    workflow.update({"id": workflow_id, "implements_spec_refs": [ask_ref]})
    technical["security_privacy_contract"] = {
        "controls": [
            {
                "id": control_id,
                "implements_spec_refs": [ask_ref],
                "verification_refs": [verification["id"]],
            }
        ]
    }
    verification["technical_refs"] = [
        component["id"],
        interface["id"],
        data_contract["id"],
        workflow_id,
        control_id,
        audit_id,
    ]
    technical["observability_audit"] = {"audit_records": [{"id": audit_id}]}
    technical["traceability"] = [
        {"from_ref": component["id"], "relation": "implements", "to_ref": ask_ref},
        {"from_ref": interface["id"], "relation": "realizes", "to_ref": ask_ref},
        {
            "from_ref": data_contract["id"],
            "relation": "realizes",
            "to_ref": ask_ref,
        },
        {"from_ref": workflow_id, "relation": "realizes", "to_ref": ask_ref},
        {"from_ref": control_id, "relation": "mitigates", "to_ref": ask_ref},
        {
            "from_ref": verification["id"],
            "relation": "verifies",
            "to_ref": ask_ref,
        },
        {"from_ref": workflow_id, "relation": "produces", "to_ref": audit_id},
    ]
    return spec, technical, ask_ref


def test_complete_generic_mapping_is_ready_for_semantic_audit():
    report = technical_contract_preflight(
        spec_payload=_spec(), technical_payload=_technical()
    )

    assert report.ready_for_semantic_audit
    assert report.as_dict()["summary"] == {
        "item_count": 6,
        "covered_count": 6,
        "uncovered_count": 0,
        "closure_rule_count": 6,
        "closure_rule_pass_count": 6,
        "sanity_issue_count": 0,
        "open_governance_count": 0,
        "ready_for_semantic_audit": True,
    }
    assert all(item.satisfied for item in report.closure_rules)


def test_ask_first_reference_alone_does_not_claim_technical_coverage():
    spec = _spec()
    technical = _technical()
    ask_ref = "00000000-0000-4000-8000-000000000107"
    spec["behaviour_contract"]["ask_first"] = [{"id": ask_ref}]
    technical["components"][0]["implements_spec_refs"].append(ask_ref)

    report = technical_contract_preflight(
        spec_payload=spec, technical_payload=technical
    )

    item = next(item for item in report.checklist if item.source_id == ask_ref)
    assert not item.covered
    assert item.covered_by == ()
    assert {
        issue.code for issue in report.sanity_issues
    } >= {
        "MISSING_GOVERNED_CHANGE_RECEIPT",
        "MISSING_GOVERNED_CHANGE_INTERFACE",
        "MISSING_GOVERNED_CHANGE_LIFECYCLE",
        "MISSING_GOVERNED_CHANGE_FAIL_CLOSED_CONTROL",
        "MISSING_GOVERNED_CHANGE_VERIFICATION",
        "MISSING_GOVERNED_CHANGE_AUDIT_RECORD",
        "MISSING_GOVERNED_CHANGE_TRACEABILITY",
    }


def test_complete_governed_change_bundle_closes_exact_ask_first_identity():
    spec, technical, ask_ref = _with_governed_change_bundle()

    report = technical_contract_preflight(
        spec_payload=spec, technical_payload=technical
    )

    assert report.ready_for_semantic_audit
    item = next(item for item in report.checklist if item.source_id == ask_ref)
    assert item.covered
    assert item.source_pointer == "/behaviour_contract/ask_first/0"
    assert len(item.covered_by) == 14


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    (
        (
            lambda technical: technical["components"][0]["implements_spec_refs"].pop(),
            "MISSING_GOVERNED_CHANGE_OWNER",
        ),
        (
            lambda technical: technical["data_contracts"][0]["implements_spec_refs"].pop(),
            "MISSING_GOVERNED_CHANGE_RECEIPT",
        ),
        (
            lambda technical: technical["interfaces"][0].update(errors=[]),
            "MISSING_GOVERNED_CHANGE_INTERFACE",
        ),
        (
            lambda technical: technical["workflows"][0].update(
                trigger_interface_ref=None
            ),
            "MISSING_GOVERNED_CHANGE_LIFECYCLE",
        ),
        (
            lambda technical: technical["security_privacy_contract"].update(
                controls=[]
            ),
            "MISSING_GOVERNED_CHANGE_FAIL_CLOSED_CONTROL",
        ),
        (
            lambda technical: technical["verification_plan"][0].update(
                technical_refs=[]
            ),
            "MISSING_GOVERNED_CHANGE_VERIFICATION",
        ),
        (
            lambda technical: technical["observability_audit"].update(
                audit_records=[]
            ),
            "MISSING_GOVERNED_CHANGE_AUDIT_RECORD",
        ),
        (
            lambda technical: technical.update(traceability=[]),
            "MISSING_GOVERNED_CHANGE_TRACEABILITY",
        ),
    ),
)
def test_incomplete_governed_change_bundle_fails_closed(mutation, expected_code):
    spec, technical, ask_ref = _with_governed_change_bundle()
    mutation(technical)

    report = technical_contract_preflight(
        spec_payload=spec, technical_payload=technical
    )

    item = next(item for item in report.checklist if item.source_id == ask_ref)
    assert not item.covered
    assert expected_code in {issue.code for issue in report.sanity_issues}


def test_one_shared_bundle_may_close_multiple_ask_first_identities():
    spec, technical, first_ref = _with_governed_change_bundle()
    second_ref = "00000000-0000-4000-8000-000000000108"
    spec["behaviour_contract"]["ask_first"].append({"id": second_ref})
    for collection in (
        technical["components"],
        technical["interfaces"],
        technical["data_contracts"],
        technical["workflows"],
        technical["security_privacy_contract"]["controls"],
    ):
        collection[0]["implements_spec_refs"].append(second_ref)
    relations = (
        (technical["components"][0]["id"], "implements"),
        (technical["interfaces"][0]["id"], "realizes"),
        (technical["data_contracts"][0]["id"], "realizes"),
        (technical["workflows"][0]["id"], "realizes"),
        (
            technical["security_privacy_contract"]["controls"][0]["id"],
            "mitigates",
        ),
        (technical["verification_plan"][0]["id"], "verifies"),
    )
    technical["traceability"].extend(
        {"from_ref": source, "relation": relation, "to_ref": second_ref}
        for source, relation in relations
    )

    report = technical_contract_preflight(
        spec_payload=spec, technical_payload=technical
    )

    assert report.ready_for_semantic_audit
    assert {
        item.source_id for item in report.checklist if item.covered
    } >= {first_ref, second_ref}


def test_manifest_is_exactly_bound_to_confirmed_spec_obligations():
    manifest = build_technical_closure_manifest(_spec())

    assert [item.section for item in manifest.rules] == [
        "ARCHITECTURE",
        "RESPONSIBILITY",
        "INTERFACE",
        "DATA",
        "WORKFLOW",
        "DELIVERY_GOVERNANCE",
    ]
    assert [item.source_pointer for item in manifest.obligations] == [
        "/requirements/0",
        "/data_rules/0",
        "/acceptance_checks/0",
        "/quality_attributes/0",
        "/behaviour_contract/always/0",
        "/behaviour_contract/never/0",
    ]


def test_technical_request_rejects_a_manifest_that_drops_a_spec_obligation():
    target = c.ArtifactDraftTarget(
        artifact_type="TECHNICAL_CONTRACT",
        foundation_artifact_id=UUID("00000000-0000-4000-8000-000000000301"),
        artifact_key="CONTRACT-TEST",
        next_artifact_version=1,
    )
    identity = c.PlannedArtifactIdentity(
        foundation_id=UUID("00000000-0000-4000-8000-000000000302"),
        foundation_version=1,
        entity_kind="COMPONENT",
    )
    slot = c.ArtifactIdentitySlot(
        slot_key="COMPONENT:0001",
        entity_kind="COMPONENT",
        ordinal=1,
        owner="ANALYZER",
        foundation_id=identity.foundation_id,
        allocation_mode="NEW_ENTITY",
    )
    plan = c.ArtifactSynthesisIdentityPlan(
        identity_plan_id=UUID("00000000-0000-4000-8000-000000000303"),
        identity_plan_version=1,
        target=target,
        based_on_case_revision=1,
        semantic_state_hash="sha256:" + "1" * 64,
        source_entity_refs=(),
        planned_identities=(identity,),
        slots=(slot,),
    )
    blueprint = c.ArtifactConstructionBlueprint(
        blueprint_version="1.0.0",
        slots=(
            c.ArtifactConstructionSlot(
                slot_key=slot.slot_key,
                foundation_id=identity.foundation_id,
                foundation_version=1,
                entity_kind="COMPONENT",
                ordinal=1,
                owner="ANALYZER",
                allocation_mode="NEW_ENTITY",
                purpose="Own one responsibility.",
            ),
        ),
    )
    spec_payload = _spec()
    manifest = build_technical_closure_manifest(spec_payload)
    values = {
        "protocol_version": "1.0.0",
        "request_type": "TECHNICAL_CONTRACT_SYNTHESIS",
        "client_request_id": "technical-closure-test",
        "analyzer_run_id": UUID("00000000-0000-4000-8000-000000000304"),
        "context_id": UUID("00000000-0000-4000-8000-000000000305"),
        "provider_conversation_id": "conversation-test",
        "request_hash": "sha256:" + "0" * 64,
        "source_set_hash": "sha256:" + "2" * 64,
        "analyzer_contract": c.AnalyzerContractBinding(
            protocol_version="1.0.0",
            instruction_set_id="specops-workshop-analyzer",
            instruction_set_version=1,
            instruction_set_hash="sha256:" + "3" * 64,
            semantic_quality_contract_id="SEMANTIC-QUALITY-CONTRACT",
            semantic_quality_contract_version="2.2.0",
            semantic_quality_contract_hash="sha256:" + "4" * 64,
            provider_schema_version="1.0.0",
            model="gpt-5.6-terra",
            reasoning_effort="medium",
        ),
        "based_on_case_revision": 1,
        "target": target,
        "identity_plan": plan,
        "construction_blueprint": blueprint,
        "technical_closure_manifest": manifest,
        "confirmed_spec": c.ConfirmedSpecSynthesisBinding(
            foundation_artifact_id=UUID(
                "00000000-0000-4000-8000-000000000306"
            ),
            artifact_key="SPEC-TEST",
            artifact_version=1,
            record_revision=1,
            confirmed_case_revision=1,
            payload_hash="sha256:" + "5" * 64,
            confirmation_id=UUID("00000000-0000-4000-8000-000000000307"),
            canonical_payload_json=json.dumps(spec_payload),
        ),
        "foundation_snapshot": c.FoundationSemanticSnapshot(
            case_revision=1,
            source_set_hash="sha256:" + "2" * 64,
            readiness=c.Readiness.FORMULATING,
            review_obligation=c.ReviewObligation.NONE,
            evidence=(),
            problems=(),
            questions=(),
            facts=(),
            decisions=(),
            evidence_findings=(),
            revision_requests=(),
        ),
        "payload_schema_id": "technical-contract-payload",
        "payload_schema_version": "4.0.0",
        "requested_output": "TECHNICAL_CONTRACT_SYNTHESIS_CANDIDATE",
    }

    c.TechnicalContractSynthesisRequest.model_validate(values)
    values["technical_closure_manifest"] = manifest.model_copy(
        update={"obligations": manifest.obligations[:-1]}
    )
    with pytest.raises(ValueError, match="changed confirmed Spec obligations"):
        c.TechnicalContractSynthesisRequest.model_validate(values)


def test_bounded_revision_schema_is_strict_openai_compatible():
    validate_openai_strict_schema(artifact_quality_revision_native_schema())


def test_missing_coverage_is_pointer_specific_and_content_agnostic():
    technical = _technical()
    technical["verification_plan"][0]["covers_acceptance_refs"] = []
    technical["quality_budgets"] = []

    report = technical_contract_preflight(
        spec_payload=_spec(), technical_payload=technical
    )

    assert [(item.category, item.source_pointer) for item in report.uncovered] == [
        ("acceptance_check", "/acceptance_checks/0"),
        ("quality_attribute", "/quality_attributes/0"),
    ]


def test_sanity_check_rejects_unresolved_schema_and_graph_references():
    technical = _technical()
    technical["interfaces"][0]["output"]["schema_ref"] = "missing.v1"
    technical["workflows"][0]["transitions"][0]["to"] = "unknown"
    technical["workflows"][0]["transitions"][1]["failure_ref"] = "missing"
    technical["quality_budgets"][0]["verification_refs"] = ["missing"]

    report = technical_contract_preflight(
        spec_payload=_spec(), technical_payload=technical
    )

    assert [(item.code, item.pointer) for item in report.sanity_issues] == [
        (
            "UNKNOWN_FAILURE_CONTRACT",
            "/workflows/0/transitions/1/failure_ref",
        ),
        (
            "UNKNOWN_VERIFICATION_ITEM",
            "/quality_budgets/0/verification_refs/0",
        ),
        ("UNKNOWN_WORKFLOW_STATE", "/workflows/0/transitions/0/to"),
        ("UNRESOLVED_INTERFACE_SCHEMA", "/interfaces/0/output/schema_ref"),
    ]


def test_closure_manifest_exposes_structural_and_governance_gaps_without_settling_them():
    technical = deepcopy(_technical())
    technical["architecture_context"]["nodes"][0].pop("technical_ref")
    technical["workflows"][0].pop("initial_state")
    technical["rollout_migration_recovery"].pop("owner")
    technical["engineering_decisions"] = [
        {
            "status": "validation_required",
            "evidence_refs": [],
        }
    ]
    technical["review_obligations"] = [
        {"status": "open", "blocking": True}
    ]

    report = technical_contract_preflight(
        spec_payload=_spec(), technical_payload=technical
    )

    assert {
        (item.code, item.section)
        for item in report.sanity_issues
    } == {
        ("MISSING_ARCHITECTURE_TECHNICAL_REF", "ARCHITECTURE"),
        ("UNMAPPED_COMPONENT", "ARCHITECTURE"),
        ("MISSING_WORKFLOW_INITIAL_STATE", "WORKFLOW"),
        ("MISSING_ROLLOUT_OWNER", "DELIVERY_GOVERNANCE"),
        (
            "UNRESOLVED_CHOICE_IN_ENGINEERING_DECISIONS",
            "DELIVERY_GOVERNANCE",
        ),
    }
    assert report.governance_notices[0].code == "OPEN_BLOCKING_REVIEW_OBLIGATION"
    assert not report.ready_for_semantic_audit


def test_decision_and_substrate_hygiene_never_promotes_unproven_choices():
    technical = deepcopy(_technical())
    dependency_id = "00000000-0000-4000-8000-000000000290"
    technical["substrate_dependencies"] = [
        {
            "id": dependency_id,
            "status": "approved",
            "evidence_refs": [],
        }
    ]
    technical["engineering_decisions"] = [
        {
            "status": "accepted",
            "evidence_refs": [],
        }
    ]

    report = technical_contract_preflight(
        spec_payload=_spec(), technical_payload=technical
    )

    assert {
        item.code for item in report.sanity_issues
    } >= {
        "UNEVIDENCED_APPROVED_SUBSTRATE_DEPENDENCY",
        "UNEVIDENCED_ENGINEERING_DECISION",
    }


def test_unresolved_substrate_is_honest_only_when_bound_to_open_review():
    technical = deepcopy(_technical())
    dependency_id = "00000000-0000-4000-8000-000000000290"
    technical["substrate_dependencies"] = [
        {
            "id": dependency_id,
            "status": "validation_required",
            "evidence_refs": [],
        }
    ]

    ungoverned = technical_contract_preflight(
        spec_payload=_spec(), technical_payload=technical
    )
    assert "UNGOVERNED_UNRESOLVED_SUBSTRATE_DEPENDENCY" in {
        item.code for item in ungoverned.sanity_issues
    }

    technical["review_obligations"] = [
        {
            "status": "open",
            "blocking": True,
            "related_refs": [dependency_id],
        }
    ]
    governed = technical_contract_preflight(
        spec_payload=_spec(), technical_payload=technical
    )
    assert "UNGOVERNED_UNRESOLVED_SUBSTRATE_DEPENDENCY" not in {
        item.code for item in governed.sanity_issues
    }


def test_external_architecture_node_may_consume_an_interface_without_owning_a_component():
    technical = deepcopy(_technical())
    actor_node_id = "00000000-0000-4000-8000-000000000299"
    service_node_id = technical["architecture_context"]["nodes"][0]["id"]
    technical["architecture_context"]["nodes"].append(
        {
            "id": actor_node_id,
            "kind": "actor",
            "technical_ref": None,
            "inside_system_boundary": False,
        }
    )
    technical["architecture_context"]["interactions"] = [
        {"from_ref": actor_node_id, "to_ref": service_node_id}
    ]
    technical["components"][0]["consumes_interface_refs"] = []
    technical["interfaces"][0]["consumer_refs"] = [actor_node_id]

    report = technical_contract_preflight(
        spec_payload=_spec(), technical_payload=technical
    )

    assert report.ready_for_semantic_audit
