"""Deterministic Spec-to-Technical closure manifest and V0 preflight."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from typing import Any, Iterator

from specops_contracts import workshop_v1 as c


@dataclass(frozen=True)
class TechnicalCoverageItem:
    category: str
    source_id: str
    source_pointer: str
    covered_by: tuple[str, ...]

    @property
    def covered(self) -> bool:
        return bool(self.covered_by)


@dataclass(frozen=True)
class TechnicalSanityIssue:
    code: str
    pointer: str
    section: str


@dataclass(frozen=True)
class TechnicalGovernanceNotice:
    code: str
    pointer: str


@dataclass(frozen=True)
class TechnicalClosureRuleResult:
    rule_key: str
    section: str
    requirement: str
    issue_pointers: tuple[str, ...]

    @property
    def satisfied(self) -> bool:
        return not self.issue_pointers


@dataclass(frozen=True)
class TechnicalContractPreflight:
    checklist: tuple[TechnicalCoverageItem, ...]
    closure_rules: tuple[TechnicalClosureRuleResult, ...]
    sanity_issues: tuple[TechnicalSanityIssue, ...]
    governance_notices: tuple[TechnicalGovernanceNotice, ...]

    @property
    def uncovered(self) -> tuple[TechnicalCoverageItem, ...]:
        return tuple(item for item in self.checklist if not item.covered)

    @property
    def ready_for_semantic_audit(self) -> bool:
        return not self.uncovered and not self.sanity_issues

    def as_dict(self) -> dict[str, Any]:
        return {
            "checklist": [
                {**asdict(item), "covered": item.covered} for item in self.checklist
            ],
            "closure_manifest": [
                {**asdict(item), "satisfied": item.satisfied}
                for item in self.closure_rules
            ],
            "summary": {
                "item_count": len(self.checklist),
                "covered_count": len(self.checklist) - len(self.uncovered),
                "uncovered_count": len(self.uncovered),
                "closure_rule_count": len(self.closure_rules),
                "closure_rule_pass_count": sum(
                    item.satisfied for item in self.closure_rules
                ),
                "sanity_issue_count": len(self.sanity_issues),
                "open_governance_count": len(self.governance_notices),
                "ready_for_semantic_audit": self.ready_for_semantic_audit,
            },
            "sanity_issues": [asdict(item) for item in self.sanity_issues],
            "governance_notices": [
                asdict(item) for item in self.governance_notices
            ],
        }


def build_technical_closure_manifest(
    spec_payload: dict[str, Any],
) -> c.TechnicalClosureManifest:
    """Bind the exact confirmed Spec obligations to six fixed closure rules."""

    return c.TechnicalClosureManifest(
        manifest_version="1.0.0",
        rules=tuple(
            c.TechnicalClosureRule(
                rule_key=rule_key,
                section=section,
                requirement=requirement,
            )
            for rule_key, section, requirement in c.TECHNICAL_CLOSURE_RULE_LAYOUT
        ),
        obligations=tuple(
            c.TechnicalClosureObligation(
                source_kind=source_kind,
                source_ref=source_ref,
                source_pointer=source_pointer,
            )
            for source_kind, source_ref, source_pointer in (
                c.technical_closure_obligations_from_spec_payload(spec_payload)
            )
        ),
    )


def _escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _walk(value: Any, pointer: str = "") -> Iterator[tuple[str, Any]]:
    yield pointer, value
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _walk(child, f"{pointer}/{_escape(str(key))}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk(child, f"{pointer}/{index}")


def _spec_obligations(spec_payload: dict[str, Any]) -> tuple[tuple[str, str, str], ...]:
    return tuple(
        (source_kind, str(source_ref), source_pointer)
        for source_kind, source_ref, source_pointer in (
            c.technical_closure_obligations_from_spec_payload(spec_payload)
        )
    )


def _coverage_index(technical_payload: dict[str, Any]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for pointer, value in _walk(technical_payload):
        if pointer.endswith("/implements_spec_refs") and isinstance(value, list):
            for identity in value:
                if isinstance(identity, str):
                    result.setdefault(identity, set()).add(pointer)
    for index, item in enumerate(technical_payload.get("verification_plan", [])):
        if not isinstance(item, dict):
            continue
        for identity in item.get("covers_acceptance_refs", []):
            if isinstance(identity, str):
                result.setdefault(identity, set()).add(
                    f"/verification_plan/{index}/covers_acceptance_refs"
                )
    for index, item in enumerate(technical_payload.get("quality_budgets", [])):
        if not isinstance(item, dict):
            continue
        identity = item.get("spec_quality_ref")
        if isinstance(identity, str):
            result.setdefault(identity, set()).add(
                f"/quality_budgets/{index}/spec_quality_ref"
            )
    return result


def _identified_items(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    values = payload.get(key, [])
    return [item for item in values if isinstance(item, dict)] if isinstance(values, list) else []


def _identity_index(items: list[dict[str, Any]]) -> dict[str, tuple[int, dict[str, Any]]]:
    return {
        str(item["id"]): (index, item)
        for index, item in enumerate(items)
        if isinstance(item.get("id"), str)
    }


def _sanity_issues(technical_payload: dict[str, Any]) -> tuple[TechnicalSanityIssue, ...]:
    issues: set[tuple[str, str, str]] = set()

    def add(code: str, pointer: str, section: str) -> None:
        issues.add((code, pointer, section))

    components = _identified_items(technical_payload, "components")
    component_index = _identity_index(components)
    component_ids = set(component_index)
    dependencies = _identified_items(technical_payload, "substrate_dependencies")
    dependency_index = _identity_index(dependencies)
    dependency_ids = set(dependency_index)

    architecture = technical_payload.get("architecture_context")
    architecture = architecture if isinstance(architecture, dict) else {}
    nodes = architecture.get("nodes", [])
    nodes = [item for item in nodes if isinstance(item, dict)] if isinstance(nodes, list) else []
    node_ids = {
        str(item["id"])
        for item in nodes
        if isinstance(item.get("id"), str)
    }
    mapped_refs: list[str] = []
    for index, node in enumerate(nodes):
        pointer = f"/architecture_context/nodes/{index}"
        technical_ref = node.get("technical_ref")
        if technical_ref is None:
            if node.get("kind") in {"service", "data_store", "external_provider"}:
                add("MISSING_ARCHITECTURE_TECHNICAL_REF", pointer, "ARCHITECTURE")
            continue
        if not isinstance(technical_ref, str) or technical_ref not in component_ids | dependency_ids:
            add("UNKNOWN_ARCHITECTURE_TECHNICAL_REF", f"{pointer}/technical_ref", "ARCHITECTURE")
            continue
        mapped_refs.append(technical_ref)
        if technical_ref in component_ids and node.get("inside_system_boundary") is not True:
            add("COMPONENT_OUTSIDE_SYSTEM_BOUNDARY", pointer, "ARCHITECTURE")
        if technical_ref in dependency_ids and node.get("inside_system_boundary") is not False:
            add("DEPENDENCY_INSIDE_SYSTEM_BOUNDARY", pointer, "ARCHITECTURE")
    for identity, (index, _item) in component_index.items():
        if identity not in mapped_refs:
            add("UNMAPPED_COMPONENT", f"/components/{index}", "ARCHITECTURE")
    for identity, (index, _item) in dependency_index.items():
        if identity not in mapped_refs:
            add("UNMAPPED_SUBSTRATE_DEPENDENCY", f"/substrate_dependencies/{index}", "ARCHITECTURE")
    for identity, count in Counter(mapped_refs).items():
        if count > 1:
            for index, node in enumerate(nodes):
                if node.get("technical_ref") == identity:
                    add("DUPLICATE_ARCHITECTURE_TECHNICAL_REF", f"/architecture_context/nodes/{index}/technical_ref", "ARCHITECTURE")
    interactions = architecture.get("interactions", [])
    interactions = interactions if isinstance(interactions, list) else []
    if len(nodes) > 1 and not interactions:
        add("MISSING_ARCHITECTURE_INTERACTIONS", "/architecture_context/interactions", "ARCHITECTURE")
    for index, interaction in enumerate(interactions):
        if not isinstance(interaction, dict):
            continue
        for endpoint in ("from_ref", "to_ref"):
            if str(interaction.get(endpoint)) not in node_ids:
                add("UNKNOWN_ARCHITECTURE_NODE", f"/architecture_context/interactions/{index}/{endpoint}", "ARCHITECTURE")

    interfaces = _identified_items(technical_payload, "interfaces")
    interface_index = _identity_index(interfaces)
    data_contracts = _identified_items(technical_payload, "data_contracts")
    data_index = _identity_index(data_contracts)
    failures = _identified_items(technical_payload, "failure_contracts")
    failure_index = _identity_index(failures)

    provided_by: dict[str, set[str]] = {}
    consumed_by: dict[str, set[str]] = {}
    owned_by: dict[str, set[str]] = {}
    for component_id, (index, component) in component_index.items():
        for ref in component.get("provides_interface_refs", []):
            if isinstance(ref, str):
                provided_by.setdefault(ref, set()).add(component_id)
                if ref not in interface_index:
                    add("UNKNOWN_PROVIDED_INTERFACE", f"/components/{index}/provides_interface_refs", "RESPONSIBILITY")
        for ref in component.get("consumes_interface_refs", []):
            if isinstance(ref, str):
                consumed_by.setdefault(ref, set()).add(component_id)
                if ref not in interface_index:
                    add("UNKNOWN_CONSUMED_INTERFACE", f"/components/{index}/consumes_interface_refs", "RESPONSIBILITY")
        for ref in component.get("owns_data_refs", []):
            if isinstance(ref, str):
                owned_by.setdefault(ref, set()).add(component_id)
                if ref not in data_index:
                    add("UNKNOWN_OWNED_DATA_CONTRACT", f"/components/{index}/owns_data_refs", "RESPONSIBILITY")
        for ref in component.get("dependency_refs", []):
            if isinstance(ref, str) and ref not in dependency_ids:
                add("UNKNOWN_COMPONENT_DEPENDENCY", f"/components/{index}/dependency_refs", "RESPONSIBILITY")

    schema_names = set(data_index)
    schema_names.update(
        str(item.get("name")) for item in data_contracts if item.get("name")
    )
    for index, interface in enumerate(interfaces):
        interface_id = str(interface.get("id", ""))
        producer = interface.get("producer_ref")
        if not isinstance(producer, str) or producer not in component_ids:
            add("UNKNOWN_INTERFACE_PRODUCER", f"/interfaces/{index}/producer_ref", "RESPONSIBILITY")
        elif provided_by.get(interface_id) != {producer}:
            add("INTERFACE_PRODUCER_OWNERSHIP_MISMATCH", f"/interfaces/{index}/producer_ref", "RESPONSIBILITY")
        consumers = {
            value for value in interface.get("consumer_refs", []) if isinstance(value, str)
        }
        if not consumers or not consumers.issubset(component_ids | node_ids):
            add("UNKNOWN_INTERFACE_CONSUMER", f"/interfaces/{index}/consumer_refs", "RESPONSIBILITY")
        elif consumed_by.get(interface_id, set()) != consumers.intersection(component_ids):
            add("INTERFACE_CONSUMER_OWNERSHIP_MISMATCH", f"/interfaces/{index}/consumer_refs", "RESPONSIBILITY")
        for direction in ("input", "output"):
            contract = interface.get(direction)
            schema_ref = contract.get("schema_ref") if isinstance(contract, dict) else None
            if not isinstance(schema_ref, str) or schema_ref not in schema_names:
                add("UNRESOLVED_INTERFACE_SCHEMA", f"/interfaces/{index}/{direction}/schema_ref", "INTERFACE")

    for identity, (index, contract) in data_index.items():
        owner = contract.get("owner_component_ref")
        if not isinstance(owner, str) or owner not in component_ids:
            add("UNKNOWN_DATA_CONTRACT_OWNER", f"/data_contracts/{index}/owner_component_ref", "RESPONSIBILITY")
        elif owned_by.get(identity) != {owner}:
            add("DATA_CONTRACT_OWNERSHIP_MISMATCH", f"/data_contracts/{index}/owner_component_ref", "RESPONSIBILITY")
    for _identity, (index, failure) in failure_index.items():
        if str(failure.get("owner_component_ref")) not in component_ids:
            add("UNKNOWN_FAILURE_OWNER", f"/failure_contracts/{index}/owner_component_ref", "RESPONSIBILITY")
    for index, unit in enumerate(_identified_items(technical_payload, "build_units")):
        refs = {value for value in unit.get("technical_refs", []) if isinstance(value, str)}
        if not refs.intersection(component_ids):
            add("BUILD_UNIT_WITHOUT_COMPONENT", f"/build_units/{index}/technical_refs", "RESPONSIBILITY")

    failure_ids = set(failure_index)
    interface_ids = set(interface_index)
    for workflow_index, workflow in enumerate(_identified_items(technical_payload, "workflows")):
        pointer = f"/workflows/{workflow_index}"
        states = {
            str(item.get("name")): item
            for item in workflow.get("states", [])
            if isinstance(item, dict) and item.get("name")
        }
        initial_state = workflow.get("initial_state")
        if not isinstance(initial_state, str):
            add("MISSING_WORKFLOW_INITIAL_STATE", pointer, "WORKFLOW")
        elif initial_state not in states:
            add("UNKNOWN_WORKFLOW_INITIAL_STATE", f"{pointer}/initial_state", "WORKFLOW")
        trigger_ref = workflow.get("trigger_interface_ref")
        if trigger_ref is not None and str(trigger_ref) not in interface_ids:
            add("UNKNOWN_WORKFLOW_TRIGGER", f"{pointer}/trigger_interface_ref", "WORKFLOW")
        outgoing: dict[str, list[dict[str, Any]]] = {}
        for transition_index, transition in enumerate(workflow.get("transitions", [])):
            if not isinstance(transition, dict):
                continue
            transition_pointer = f"{pointer}/transitions/{transition_index}"
            for endpoint in ("from", "to"):
                if transition.get(endpoint) not in states:
                    add("UNKNOWN_WORKFLOW_STATE", f"{transition_pointer}/{endpoint}", "WORKFLOW")
            if isinstance(transition.get("from"), str):
                outgoing.setdefault(transition["from"], []).append(transition)
            failure_ref = transition.get("failure_ref")
            if failure_ref is not None and str(failure_ref) not in failure_ids:
                add("UNKNOWN_FAILURE_CONTRACT", f"{transition_pointer}/failure_ref", "WORKFLOW")
        has_failure_path = False
        for state_name, state in states.items():
            transitions = outgoing.get(state_name, [])
            if state.get("terminal") is True:
                if transitions:
                    add("TERMINAL_WORKFLOW_STATE_HAS_TRANSITION", pointer, "WORKFLOW")
                continue
            if not transitions:
                add("NONTERMINAL_WORKFLOW_STATE_HAS_NO_TRANSITION", pointer, "WORKFLOW")
            has_failure_path = has_failure_path or any(
                item.get("failure_ref") is not None for item in transitions
            )
        if not has_failure_path:
            add("WORKFLOW_HAS_NO_FAILURE_PATH", pointer, "WORKFLOW")

    verification_ids = {
        str(item.get("id"))
        for item in _identified_items(technical_payload, "verification_plan")
        if item.get("id")
    }
    for index, budget in enumerate(_identified_items(technical_payload, "quality_budgets")):
        for ref_index, identity in enumerate(budget.get("verification_refs", [])):
            if str(identity) not in verification_ids:
                add("UNKNOWN_VERIFICATION_ITEM", f"/quality_budgets/{index}/verification_refs/{ref_index}", "INTERFACE")

    rollout = technical_payload.get("rollout_migration_recovery")
    if not isinstance(rollout, dict) or not isinstance(rollout.get("owner"), str):
        add("MISSING_ROLLOUT_OWNER", "/rollout_migration_recovery", "DELIVERY_GOVERNANCE")
    for index, decision in enumerate(_identified_items(technical_payload, "engineering_decisions")):
        status = decision.get("status")
        if status in {"validation_required", "deferred"}:
            add("UNRESOLVED_CHOICE_IN_ENGINEERING_DECISIONS", f"/engineering_decisions/{index}", "DELIVERY_GOVERNANCE")
        if status == "accepted" and not decision.get("evidence_refs"):
            add("UNEVIDENCED_ENGINEERING_DECISION", f"/engineering_decisions/{index}/evidence_refs", "DELIVERY_GOVERNANCE")

    return tuple(
        TechnicalSanityIssue(code=code, pointer=pointer, section=section)
        for code, pointer, section in sorted(issues)
    )


def _governance_notices(
    technical_payload: dict[str, Any],
) -> tuple[TechnicalGovernanceNotice, ...]:
    return tuple(
        TechnicalGovernanceNotice(
            code="OPEN_BLOCKING_REVIEW_OBLIGATION",
            pointer=f"/review_obligations/{index}",
        )
        for index, item in enumerate(
            _identified_items(technical_payload, "review_obligations")
        )
        if item.get("status") == "open" and item.get("blocking") is True
    )


def technical_contract_preflight(
    *, spec_payload: dict[str, Any], technical_payload: dict[str, Any]
) -> TechnicalContractPreflight:
    """Build exact coverage plus the shared six-rule Technical closure report."""

    coverage = _coverage_index(technical_payload)
    checklist = tuple(
        TechnicalCoverageItem(
            category=category,
            source_id=identity,
            source_pointer=pointer,
            covered_by=tuple(sorted(coverage.get(identity, set()))),
        )
        for category, identity, pointer in _spec_obligations(spec_payload)
    )
    issues = _sanity_issues(technical_payload)
    uncovered_by_section: dict[str, set[str]] = {}
    category_sections = {
        "requirement": "RESPONSIBILITY",
        "data_rule": "DATA",
        "acceptance_check": "INTERFACE",
        "quality_attribute": "DELIVERY_GOVERNANCE",
        "behaviour_rule": "WORKFLOW",
    }
    for item in checklist:
        if not item.covered:
            uncovered_by_section.setdefault(category_sections[item.category], set()).add(
                item.source_pointer
            )
    closure_rules = tuple(
        TechnicalClosureRuleResult(
            rule_key=rule_key,
            section=section,
            requirement=requirement,
            issue_pointers=tuple(
                sorted(
                    {
                        item.pointer for item in issues if item.section == section
                    }
                    | uncovered_by_section.get(section, set())
                )
            ),
        )
        for rule_key, section, requirement in c.TECHNICAL_CLOSURE_RULE_LAYOUT
    )
    return TechnicalContractPreflight(
        checklist=checklist,
        closure_rules=closure_rules,
        sanity_issues=issues,
        governance_notices=_governance_notices(technical_payload),
    )


__all__ = [
    "TechnicalClosureRuleResult",
    "TechnicalContractPreflight",
    "TechnicalCoverageItem",
    "TechnicalGovernanceNotice",
    "TechnicalSanityIssue",
    "build_technical_closure_manifest",
    "technical_contract_preflight",
]
