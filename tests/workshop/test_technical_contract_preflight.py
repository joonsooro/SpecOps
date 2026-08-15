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
