"""Deterministic minimal-valid V4 payload fixtures generated from normative schemas."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID


ROOT = Path(__file__).resolve().parents[2] / "src/specops_contracts/schemas"
SCHEMAS = {
    path.name: json.loads(path.read_text(encoding="utf-8")) for path in ROOT.glob("*.json")
}


class PayloadFactory:
    def __init__(self, *, full_identity_plan: bool = False) -> None:
        self.counter = 500
        self.full_identity_plan = full_identity_plan

    def uuid(self) -> str:
        self.counter += 1
        return f"50000000-0000-4000-8000-{self.counter:012d}"

    def _resolve(self, ref: str, current: dict):
        filename, _, fragment = ref.partition("#")
        root = SCHEMAS[filename] if filename else current
        value = root
        for token in fragment.strip("/").split("/") if fragment.strip("/") else ():
            value = value[token.replace("~1", "/").replace("~0", "~")]
        return value, SCHEMAS[filename] if filename else current

    def _items_define_identity(self, schema: dict, current: dict) -> bool:
        if "$ref" in schema:
            resolved, next_root = self._resolve(schema["$ref"], current)
            return self._items_define_identity(resolved, next_root)
        if "oneOf" in schema:
            return any(self._items_define_identity(item, current) for item in schema["oneOf"])
        identity_fields = {"id", "finding_id"}
        return bool(
            identity_fields.intersection(schema.get("required", ()))
            and identity_fields.intersection(schema.get("properties", {}))
        )

    def value(self, schema: dict, current: dict, key: str = ""):
        if "$ref" in schema:
            resolved, next_root = self._resolve(schema["$ref"], current)
            return self.value(resolved, next_root, key)
        if "const" in schema:
            return schema["const"]
        if "enum" in schema:
            return schema["enum"][0]
        if "oneOf" in schema:
            options = [item for item in schema["oneOf"] if item.get("type") != "null"]
            return self.value((options or schema["oneOf"])[0], current, key)
        kind = schema.get("type")
        if isinstance(kind, list):
            kind = next((item for item in kind if item != "null"), "null")
        if kind == "object" or "properties" in schema:
            return {
                name: self.value(schema["properties"][name], current, name)
                for name in schema.get("required", ())
            }
        if kind == "array":
            minimum = schema.get("minItems", 0)
            if self.full_identity_plan and self._items_define_identity(
                schema.get("items", {}), current
            ):
                minimum = max(1, minimum)
            return [
                self.value(schema.get("items", {}), current, key)
                for _ in range(minimum)
            ]
        if kind == "integer":
            return max(1, schema.get("minimum", 1))
        if kind == "number":
            return 0.5
        if kind == "boolean":
            return False
        if kind == "null":
            return None
        if schema.get("format") == "uuid" or key.endswith(("_id", "_ref")):
            return self.uuid()
        if schema.get("format") == "date-time":
            return "2026-08-13T00:00:00Z"
        if schema.get("format") == "date":
            return "2026-08-13"
        pattern = schema.get("pattern", "")
        if "sha256" in pattern:
            return "sha256:" + "a" * 64
        if "^/" in pattern or key.endswith("json_pointer"):
            return "/product_thesis/customer_promise"
        return "value"

    def payload(self, filename: str) -> dict:
        schema = SCHEMAS[filename]
        value = self.value(schema, schema)
        if filename == "technical-contract-payload.schema.json":
            for item in value["contract_intent"].values():
                if isinstance(item, dict) and "spec_json_pointer" in item:
                    item["spec_json_pointer"] = "/product_thesis/customer_promise"
        return value


def bind_planned_identities(
    payload: dict, mapping: dict[str, UUID]
) -> dict:
    replacement = {key: str(value) for key, value in mapping.items()}

    def replace(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if isinstance(item, str) and item in replacement:
                    value[key] = replacement[item]
                elif isinstance(item, list):
                    value[key] = [
                        replacement.get(entry, entry)
                        if isinstance(entry, str)
                        else entry
                        for entry in item
                    ]
                    for entry in value[key]:
                        replace(entry)
                else:
                    replace(item)
        elif isinstance(value, list):
            for item in value:
                replace(item)

    replace(payload)
    return payload


def bind_fixture_references(
    payload: dict, confirmed_spec_payload: dict | None = None
) -> dict:
    """Resolve schema-generated placeholders into a valid closed reference graph."""

    from specops_workflow.workshop_protocol import WorkshopFoundationService

    by_group = {
        "requirement_refs": payload.get("requirements", []),
        "decision_refs": payload.get("decisions", []),
        "acceptance_check_refs": payload.get("acceptance_checks", []),
        "dependency_refs": payload.get("dependencies", []),
        "constraint_refs": payload.get("constraints", []),
        "risk_refs": payload.get("risks", []),
        "open_item_refs": payload.get("open_items", []),
        "scenario_refs": payload.get("scenarios", []),
        "outcome_refs": payload.get("outcomes", []),
        "actor_refs": payload.get("actors", []),
    }
    if "components" in payload:
        by_group.update(
            {
                "consumer_refs": payload.get("components", []),
                "consumes_interface_refs": payload.get("interfaces", []),
                "covers_acceptance_refs": (
                    []
                    if confirmed_spec_payload is None
                    else confirmed_spec_payload.get("acceptance_checks", [])
                ),
                "dependency_refs": payload.get("substrate_dependencies", []),
                "evidence_refs": payload.get("evidence_catalog", []),
                "owns_data_refs": payload.get("data_contracts", []),
                "prerequisite_unit_refs": payload.get("build_units", []),
                "provides_interface_refs": payload.get("interfaces", []),
                "related_refs": payload.get("review_obligations", []),
                "spec_constraint_refs": (
                    []
                    if confirmed_spec_payload is None
                    else confirmed_spec_payload.get("constraints", [])
                ),
                "technical_refs": payload.get("components", []),
                "verification_refs": payload.get("verification_plan", []),
            }
        )
    actor_id = next((item["id"] for item in payload.get("actors", [])), None)
    owned_ids = list(WorkshopFoundationService._artifact_identity_kinds(payload))
    spec_ids = (
        list(WorkshopFoundationService._artifact_identity_kinds(confirmed_spec_payload))
        if confirmed_spec_payload is not None
        else []
    )
    singular_groups = {
        "owner_component_ref": payload.get("components", []),
        "producer_ref": payload.get("components", []),
        "schema_ref": payload.get("data_contracts", []),
        "from_ref": payload.get("architecture_context", {}).get("nodes", []),
        "to_ref": payload.get("architecture_context", {}).get("nodes", []),
        "trigger_interface_ref": payload.get("interfaces", []),
        "failure_ref": payload.get("failure_contracts", []),
        "component_ref": payload.get("components", []),
        "claim_ref": payload.get("components", []),
        "evidence_ref": payload.get("evidence_catalog", []),
    }

    def bind(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in by_group:
                    group = by_group[key]
                    value[key] = (
                        [entry["id"] for entry in group][: max(1, len(item))]
                        if group
                        else []
                    )
                elif key in {
                    "primary_customer", "beneficiary_actor_ref",
                    "primary_actor_ref", "actor_ref",
                } and actor_id is not None:
                    value[key] = actor_id
                elif key == "implements_spec_refs" and spec_ids:
                    value[key] = spec_ids[: max(1, len(item))] if item else []
                elif key == "spec_quality_ref" and spec_ids:
                    value[key] = spec_ids[0]
                elif key in singular_groups and singular_groups[key]:
                    value[key] = singular_groups[key][0]["id"]
                elif key in {
                    "from_ref", "trigger_interface_ref", "failure_ref", "component_ref",
                    "claim_ref", "evidence_ref",
                } and owned_ids:
                    value[key] = owned_ids[0]
                elif key == "to_ref" and owned_ids:
                    value[key] = owned_ids[min(1, len(owned_ids) - 1)]
                else:
                    bind(item)
        elif isinstance(value, list):
            for item in value:
                bind(item)

    bind(payload)
    for requirement in payload.get("requirements", []):
        if not requirement["source_evidence_refs"] and not requirement["decision_refs"]:
            requirement["priority"] = "should"
    if confirmed_spec_payload is not None:
        promise = confirmed_spec_payload["product_thesis"]["customer_promise"]
        for value in payload["contract_intent"].values():
            if isinstance(value, dict) and "spec_json_pointer" in value:
                value["spec_json_pointer"] = "/product_thesis/customer_promise"
                value["value"] = promise
    return payload
