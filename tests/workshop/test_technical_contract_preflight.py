from specops_workflow.technical_contract_preflight import technical_contract_preflight
from specops_workshop.v4.schema_compiler import (
    artifact_quality_revision_native_schema,
    validate_openai_strict_schema,
)


def _spec():
    return {
        "requirements": [{"id": "req-1"}],
        "data_rules": [{"id": "data-1"}],
        "acceptance_checks": [{"id": "check-1"}],
        "quality_attributes": [{"id": "quality-1"}],
        "behaviour_contract": {
            "always": [{"id": "always-1"}],
            "ask_first": [],
            "never": [{"id": "never-1"}],
        },
    }


def _technical():
    return {
        "components": [
            {"implements_spec_refs": ["req-1", "always-1", "never-1"]}
        ],
        "data_contracts": [
            {
                "id": "data-contract-1",
                "name": "canonical-payload.v1",
                "implements_spec_refs": ["data-1"],
            }
        ],
        "interfaces": [
            {
                "input": {"schema_ref": "canonical-payload.v1"},
                "output": {"schema_ref": "data-contract-1"},
            }
        ],
        "failure_contracts": [{"id": "failure-1"}],
        "workflows": [
            {
                "states": [
                    {"name": "ready"},
                    {"name": "complete"},
                    {"name": "failed"},
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
                        "failure_ref": "failure-1",
                    },
                ],
            }
        ],
        "verification_plan": [
            {"id": "verification-1", "covers_acceptance_refs": ["check-1"]}
        ],
        "quality_budgets": [
            {
                "spec_quality_ref": "quality-1",
                "verification_refs": ["verification-1"],
            }
        ],
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
        "sanity_issue_count": 0,
        "ready_for_semantic_audit": True,
    }


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
