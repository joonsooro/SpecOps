"""Small, deterministic Spec-to-Technical coverage preflight for V0."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterator


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


@dataclass(frozen=True)
class TechnicalContractPreflight:
    checklist: tuple[TechnicalCoverageItem, ...]
    sanity_issues: tuple[TechnicalSanityIssue, ...]

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
            "summary": {
                "item_count": len(self.checklist),
                "covered_count": len(self.checklist) - len(self.uncovered),
                "uncovered_count": len(self.uncovered),
                "sanity_issue_count": len(self.sanity_issues),
                "ready_for_semantic_audit": self.ready_for_semantic_audit,
            },
            "sanity_issues": [asdict(item) for item in self.sanity_issues],
        }


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


def _id_items(
    value: Any, pointer: str, category: str
) -> Iterator[tuple[str, str, str]]:
    if not isinstance(value, list):
        return
    for index, item in enumerate(value):
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            yield category, item["id"], f"{pointer}/{index}"


def _spec_obligations(spec_payload: dict[str, Any]) -> tuple[tuple[str, str, str], ...]:
    items: list[tuple[str, str, str]] = []
    for key, category in (
        ("requirements", "requirement"),
        ("data_rules", "data_rule"),
        ("acceptance_checks", "acceptance_check"),
        ("quality_attributes", "quality_attribute"),
    ):
        items.extend(_id_items(spec_payload.get(key), f"/{key}", category))
    behaviour = spec_payload.get("behaviour_contract")
    if isinstance(behaviour, dict):
        for key in ("always", "ask_first", "never"):
            items.extend(
                _id_items(
                    behaviour.get(key),
                    f"/behaviour_contract/{key}",
                    "behaviour_rule",
                )
            )
    return tuple(items)


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


def _sanity_issues(technical_payload: dict[str, Any]) -> tuple[TechnicalSanityIssue, ...]:
    issues: set[tuple[str, str]] = set()
    data_contracts = technical_payload.get("data_contracts", [])
    schema_names = {
        str(item.get("name"))
        for item in data_contracts
        if isinstance(item, dict) and item.get("name")
    }
    schema_names.update(
        str(item.get("id"))
        for item in data_contracts
        if isinstance(item, dict) and item.get("id")
    )
    for index, interface in enumerate(technical_payload.get("interfaces", [])):
        if not isinstance(interface, dict):
            continue
        for direction in ("input", "output"):
            contract = interface.get(direction)
            schema_ref = contract.get("schema_ref") if isinstance(contract, dict) else None
            if not isinstance(schema_ref, str) or schema_ref not in schema_names:
                issues.add(
                    (
                        "UNRESOLVED_INTERFACE_SCHEMA",
                        f"/interfaces/{index}/{direction}/schema_ref",
                    )
                )

    failure_ids = {
        str(item.get("id"))
        for item in technical_payload.get("failure_contracts", [])
        if isinstance(item, dict) and item.get("id")
    }
    for workflow_index, workflow in enumerate(technical_payload.get("workflows", [])):
        if not isinstance(workflow, dict):
            continue
        states = {
            str(item.get("name"))
            for item in workflow.get("states", [])
            if isinstance(item, dict) and item.get("name")
        }
        for transition_index, transition in enumerate(workflow.get("transitions", [])):
            if not isinstance(transition, dict):
                continue
            pointer = f"/workflows/{workflow_index}/transitions/{transition_index}"
            for endpoint in ("from", "to"):
                if transition.get(endpoint) not in states:
                    issues.add(("UNKNOWN_WORKFLOW_STATE", f"{pointer}/{endpoint}"))
            failure_ref = transition.get("failure_ref")
            if failure_ref is not None and str(failure_ref) not in failure_ids:
                issues.add(("UNKNOWN_FAILURE_CONTRACT", f"{pointer}/failure_ref"))

    verification_ids = {
        str(item.get("id"))
        for item in technical_payload.get("verification_plan", [])
        if isinstance(item, dict) and item.get("id")
    }
    for index, budget in enumerate(technical_payload.get("quality_budgets", [])):
        if not isinstance(budget, dict):
            continue
        for ref_index, identity in enumerate(budget.get("verification_refs", [])):
            if str(identity) not in verification_ids:
                issues.add(
                    (
                        "UNKNOWN_VERIFICATION_ITEM",
                        f"/quality_budgets/{index}/verification_refs/{ref_index}",
                    )
                )
    return tuple(
        TechnicalSanityIssue(code=code, pointer=pointer)
        for code, pointer in sorted(issues)
    )


def technical_contract_preflight(
    *, spec_payload: dict[str, Any], technical_payload: dict[str, Any]
) -> TechnicalContractPreflight:
    """Build the V0 coverage checklist and deterministic sanity report."""

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
    return TechnicalContractPreflight(
        checklist=checklist,
        sanity_issues=_sanity_issues(technical_payload),
    )


__all__ = [
    "TechnicalContractPreflight",
    "TechnicalCoverageItem",
    "TechnicalSanityIssue",
    "technical_contract_preflight",
]
