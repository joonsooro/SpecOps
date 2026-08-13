"""Normative Workshop V4 artifact-envelope and read-model construction."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from specops_contracts import workshop_v1 as c
from specops_contracts.canonical import artifact_review_view_hash


SCHEMA_ROOT = Path(__file__).resolve().parents[1] / "specops_contracts" / "schemas"


def validator(filename: str) -> Draft202012Validator:
    schema = json.loads((SCHEMA_ROOT / filename).read_text(encoding="utf-8"))
    registry = Registry()
    for path in SCHEMA_ROOT.glob("*.json"):
        value = json.loads(path.read_text(encoding="utf-8"))
        registry = registry.with_resource(
            value.get("$id", path.as_uri()), Resource.from_contents(value)
        )
    return Draft202012Validator(schema, registry=registry)


def validate_exact(filename: str, value: dict[str, Any]) -> None:
    errors = sorted(validator(filename).iter_errors(value), key=lambda item: list(item.path))
    if errors:
        raise ValueError(f"{filename} validation failed at /{'/'.join(map(str, errors[0].path))}")


def validate_governance(artifact_type: str, value: dict[str, Any]) -> None:
    filename = (
        "spec-package.schema.json"
        if artifact_type == "SPEC_PACKAGE"
        else "technical-contract.schema.json"
    )
    root = json.loads((SCHEMA_ROOT / filename).read_text(encoding="utf-8"))
    base = validator(filename)
    errors = sorted(
        base.evolve(schema=root["properties"]["governance"]).iter_errors(value),
        key=lambda item: list(item.path),
    )
    if errors:
        raise ValueError(
            f"{filename} governance validation failed at /{'/'.join(map(str, errors[0].path))}"
        )


def draft_governance(
    *,
    artifact_type: str,
    payload: dict[str, Any],
    target,
    quality_hash: str,
    now: datetime,
    new_id,
) -> dict[str, Any]:
    instant = now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    quality = {
        "contract_id": "SEMANTIC-QUALITY-CONTRACT",
        "version": "2.1.0",
        "content_hash": quality_hash,
    }
    if artifact_type == "SPEC_PACKAGE":
        return {
            "quality_contract": quality,
            "readiness_audit": {
                "audit_id": str(new_id()),
                "quality_contract_version": "2.1.0",
                "based_on_artifact_version": target.next_artifact_version,
                "run_at": instant,
                "ambiguity_findings": [],
                "rule_results": [],
                "blocker_refs": [],
                "result": "needs_clarification",
            },
            "item_governance": [
                {
                    "item_ref": item["id"],
                    "item_version": item["item_version"],
                    "foundation_record_version": 1,
                    "readiness": "FORMULATING",
                    "review_obligation": "NONE",
                    "approvals": [],
                    "review_obligations": [],
                    "last_committed_at": instant,
                }
                for item in payload["package_items"]
            ],
            "authority_validation_ids": [],
            "latest_review_view": None,
            "confirmation": None,
        }
    return {
        "quality_contract": quality,
        "contract_readiness": {
            "audit_id": str(new_id()),
            "quality_contract_version": "2.1.0",
            "based_on_contract_version": target.next_artifact_version,
            "run_at": instant,
            "rule_results": [],
            "blocker_refs": [],
            "result": "blocked",
            "downstream_handoff_allowed": False,
            "handoff_reasons": ["Human technical approval is not yet recorded."],
        },
        "approvals": [],
        "latest_review_view": None,
        "handoff_authorization": None,
    }


def artifact_envelope(
    *,
    artifact_type: str,
    command,
    payload: dict[str, Any],
    governance: dict[str, Any],
    payload_hash: str,
    now: datetime,
) -> dict[str, Any]:
    instant = now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    is_spec = artifact_type == "SPEC_PACKAGE"
    lineage = []
    if not is_spec:
        binding = command.confirmed_spec
        lineage.append(
            {
                "relation": "confirmed_from",
                "artifact": {
                    "artifact_type": "spec_package",
                    "artifact_id": str(binding.foundation_artifact_id),
                    "artifact_key": binding.artifact_key,
                    "artifact_version": binding.artifact_version,
                    "record_revision": binding.record_revision,
                    "payload_hash": binding.payload_hash,
                    "confirmation_id": str(binding.confirmation_id),
                },
            }
        )
    return {
        "envelope": {
            "protocol_version": "2.0.0",
            "content_type": "application/json",
            "schema_id": "spec-package" if is_spec else "technical-contract",
            "schema_version": "4.0.0",
            "artifact_type": "spec_package" if is_spec else "technical_contract",
            "artifact_id": str(command.target.foundation_artifact_id),
            "artifact_key": command.target.artifact_key,
            "artifact_version": command.target.next_artifact_version,
            "record_revision": 1,
            "lifecycle_status": "draft",
            "title": (
                payload["product_thesis"]["feature"]
                if is_spec
                else payload["contract_intent"]["implementation_objective"]
            ),
            "language": "en",
            "created_at": instant,
            "updated_at": instant,
            "foundation": {
                "case_id": str(command.case_id),
                "session_id": str(command.session_id),
                "based_on_case_revision": command.expected_case_revision,
                "committed_case_revision": command.expected_case_revision + 1,
                "mutation_id": str(command.command_id),
                "idempotency_key": command.idempotency_key,
                "correlation_id": str(command.correlation_id),
            },
            "generated_by": {
                "component": "analyzer",
                "component_version": "1.0.0",
                "run_id": str(command.candidate.analyzer_run_id),
                "model": "gpt-5.6-terra",
            },
            "integrity": {
                "hash_algorithm": "sha256",
                "canonicalization": "RFC8785_JCS",
                "payload_hash": payload_hash,
            },
            "lineage": lineage,
        },
        "payload": payload,
        "governance": governance,
    }


def _pointer(group: str, index: int) -> str:
    return f"/payload/{group}/{index}"


def _evidence_index(payload, snapshot, context):
    by_id = {str(item.evidence_id): item for item in snapshot.evidence}
    source_by_id = {
        str(item.source.source_id): item.source for item in context.source_set.ordered_sources
    }
    finding_by_evidence: dict[str, list] = {}
    for item in snapshot.evidence_findings:
        finding_by_evidence.setdefault(str(item.evidence_id), []).append(item)
    result = []
    for item in payload["evidence_catalog"]:
        admitted = by_id.get(item["id"])
        if admitted is None:
            admitted = next(
                (
                    candidate
                    for candidate in snapshot.evidence
                    if candidate.source_hash == item["source_hash"]
                    and candidate.excerpt_hash == item["excerpt_hash"]
                ),
                None,
            )
        source = source_by_id.get(item["source_id"])
        if admitted is None or source is None:
            raise ValueError("artifact evidence does not resolve to Foundation evidence")
        locator = admitted.locator
        if isinstance(locator, c.SourceLineLocator):
            focus = {
                "kind": "source_lines",
                "start_line": locator.start_line,
                "end_line": locator.end_line,
                "json_pointer": None,
                "anchor": None,
                "quote": None,
            }
            label = f"Lines {locator.start_line}-{locator.end_line}"
        elif isinstance(locator, c.JsonPointerLocator):
            focus = {"kind": "json_pointer", "start_line": None, "end_line": None, "json_pointer": locator.pointer, "anchor": None, "quote": None}
            label = locator.pointer
        elif isinstance(locator, c.DocumentAnchorLocator):
            focus = {"kind": "document_anchor", "start_line": None, "end_line": None, "json_pointer": None, "anchor": locator.anchor, "quote": None}
            label = locator.anchor
        else:
            focus = {"kind": "quote_search", "start_line": None, "end_line": None, "json_pointer": None, "anchor": None, "quote": locator.exact_quote}
            label = "Exact quote"
        result.append(
            {
                "evidence_id": item["id"],
                "source_document_id": item["source_id"],
                "source_document_name": source.filename,
                "source_document_version": source.version,
                "source_document_hash": item["source_hash"],
                "locator_label": label,
                "claim_refs": item["claim_refs"],
                "semantic_assessments": [
                    {
                        "finding_id": str(finding.finding_id),
                        "finding_version": finding.finding_version,
                        "claim_ref": str(finding.claim_id),
                        "assessment": finding.assessment.value,
                        "confidence": finding.confidence,
                    }
                    for finding in finding_by_evidence.get(item["id"], [])
                ],
                "focus_target": focus,
            }
        )
    return result


def build_review_view(
    *,
    artifact_type: str,
    record: dict[str, Any],
    payload: dict[str, Any],
    governance: dict[str, Any],
    confirmed_from: dict[str, Any] | None,
    view_id: UUID,
    view_mode: str,
    case_revision: int,
    context: c.AnalyzerContextBinding,
    snapshot: c.FoundationSemanticSnapshot,
    now: datetime,
) -> dict[str, Any]:
    mode = view_mode.lower()
    instant = now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    evidence = _evidence_index(payload, snapshot, context)
    integrity = {
        "profile_id": "artifact-review-view",
        "profile_version": "3.0.0",
        "view_id": str(view_id),
        "payload_hash": record["payload_hash"],
        "view_hash": "sha256:" + "0" * 64,
        "all_material_items_included": True,
    }
    if artifact_type == "SPEC_PACKAGE":
        requirements = {item["id"]: (index, item) for index, item in enumerate(payload["requirements"])}
        decisions = {item["id"]: (index, item) for index, item in enumerate(payload["decisions"])}
        checks = {item["id"]: (index, item) for index, item in enumerate(payload["acceptance_checks"])}
        dependencies = {item["id"]: (index, item) for index, item in enumerate(payload["dependencies"])}
        governance_by_id = {item["item_ref"]: item for item in governance["item_governance"]}
        items = []
        for package_index, item in enumerate(payload["package_items"]):
            item_governance = governance_by_id[item["id"]]
            items.append(
                {
                    "id": item["id"], "item_version": item["item_version"], "title": item["title"], "summary": item["summary"], "domains": item["domains"], "display_order": item["display_order"],
                    "requirements": [dict(id=value["id"], title=value["title"], priority=value["priority"], domains=value["domains"], trigger=value["trigger"], preconditions=value["preconditions"], behaviour=value["behaviour"], outcome=value["outcome"], exclusions=value["exclusions"], evidence_refs=value["source_evidence_refs"], canonical_pointer=_pointer("requirements", index)) for index, value in (requirements[ref] for ref in item["requirement_refs"])],
                    "decisions": [dict(id=value["id"], decision_version=value["decision_version"], classification=value["classification"], domains=value["domains"], question=value["question"], decision=value["decision"], rationale=value["rationale"], alternatives_considered=value["alternatives_considered"], status=value["status"], confirmation_binding=value["confirmation_binding"], evidence_refs=value["evidence_refs"], canonical_pointer=_pointer("decisions", index)) for index, value in (decisions[ref] for ref in item["decision_refs"])],
                    "acceptance_checks": [dict(id=value["id"], title=value["title"], type=value["type"], given=value["given"], when=value["when"], then=value["then"], negative_assertions=value["negative_assertions"], evidence_method=value["evidence_method"], evidence_refs=sorted({e for req in value["requirement_refs"] for e in requirements[req][1]["source_evidence_refs"]}), canonical_pointer=_pointer("acceptance_checks", index)) for index, value in (checks[ref] for ref in item["acceptance_check_refs"])],
                    "dependencies": [dict(id=value["id"], name=value["name"], type=value["type"], purpose=value["purpose"], status=value["status"], owner=value["owner"], failure_impact=value["failure_impact"], evidence_refs=value["evidence_refs"], canonical_pointer=_pointer("dependencies", index)) for index, value in (dependencies[ref] for ref in item["dependency_refs"])],
                    "governance": {"readiness": item_governance["readiness"], "review_obligation": item_governance["review_obligation"], "approvals": [{"domain": value["domain"], "status": value["status"], "actor_ref": value["actor_ref"], "confirmed_at": value["confirmed_at"]} for value in item_governance["approvals"]], "review_obligations": [{key: value[key] for key in ("obligation_id", "type", "status", "blocking", "owner", "due_date", "required_evidence")} for value in item_governance["review_obligations"]], "last_committed_at": item_governance["last_committed_at"]},
                    "evidence_refs": sorted({*([e for ref in item["requirement_refs"] for e in requirements[ref][1]["source_evidence_refs"]]), *([e for ref in item["decision_refs"] for e in decisions[ref][1]["evidence_refs"]])}),
                    "canonical_pointer": _pointer("package_items", package_index),
                }
            )
        counts = {name: sum(item["governance"]["readiness"] == key for item in items) for name, key in (("ready", "READY"), ("needs_clarification", "NEEDS_CLARIFICATION"), ("blocked", "BLOCKED"), ("formulating", "FORMULATING"))}
        obligations = {name: sum(item["governance"]["review_obligation"] == key for item in items) for name, key in (("none", "NONE"), ("later_review", "LATER_REVIEW"), ("decision_required", "DECISION_REQUIRED"))}
        open_items = payload["open_items"]
        queue = lambda values: [dict(id=value["id"], title=value["question"], reason=value["reason"], owner=value["owner"], due_date=value["due_date"], related_refs=value["related_refs"], canonical_pointer=_pointer("open_items", index)) for index, value in enumerate(values)]
        view = {
            "view_schema_version": "3.0.0", "view_type": "spec_package_view", "view_id": str(view_id), "mode": mode,
            "source": {"artifact_id": record["artifact_id"], "artifact_key": record["artifact_key"], "artifact_version": record["artifact_version"], "record_revision": record["record_revision"], "payload_hash": record["payload_hash"], "lifecycle_status": record["status"].lower().replace("_", "_"), "case_revision": case_revision},
            "generated_at": instant,
            "header": {"package_name": payload["product_thesis"]["feature"], "version_label": f"v{record['artifact_version']}.{record['record_revision']}", "overall_readiness": "FORMULATING", "last_confirmed_at": None, "readiness_counts": counts, "review_obligation_counts": obligations, "short_payload_hash": record["payload_hash"].removeprefix("sha256:")[:12]},
            "product_thesis": {
                key: payload["product_thesis"][key]
                for key in (
                    "product",
                    "feature",
                    "primary_customer",
                    "problem_statement",
                    "opportunity",
                    "customer_promise",
                    "negative_contract",
                )
            }
            | {"canonical_pointer": "/payload/product_thesis"},
            "items": items,
            "queues": {"blocking_ambiguities": queue([x for x in open_items if x["blocking"]]), "decisions_requiring_answers": queue([x for x in open_items if x["status"] == "open"]), "later_review": queue([x for x in open_items if not x["blocking"]]), "excluded_or_deferred_scope": [dict(id=x["id"], title=x["statement"], reason=x["rationale"], owner=None, due_date=None, related_refs=[], canonical_pointer=_pointer("scope/non_goals", i)) for i, x in enumerate(payload["scope"]["non_goals"])]},
            "evidence_index": evidence, "projection_integrity": integrity,
        }
    else:
        obligations = payload["review_obligations"]
        dependencies = payload["substrate_dependencies"]
        rejected = payload["engineering_decisions"]
        queue_entry = lambda value, title, reason, owner, due, pointer: {"id": value["id"], "title": title, "reason": reason, "owner": owner, "due_date": due, "related_refs": value.get("related_refs", value.get("spec_constraint_refs", [])), "canonical_pointer": pointer}
        lineage = confirmed_from
        view = {
            "view_schema_version": "3.0.0", "view_type": "technical_contract_view", "view_id": str(view_id), "mode": mode,
            "source": {"artifact_id": record["artifact_id"], "artifact_key": record["artifact_key"], "artifact_version": record["artifact_version"], "record_revision": record["record_revision"], "payload_hash": record["payload_hash"], "lifecycle_status": record["status"].lower(), "case_revision": case_revision, "confirmed_spec_artifact_id": lineage["foundation_artifact_id"], "confirmed_spec_artifact_key": lineage["artifact_key"], "confirmed_spec_version": lineage["artifact_version"], "confirmed_spec_record_revision": lineage["record_revision"], "confirmed_spec_payload_hash": lineage["payload_hash"], "confirmed_spec_confirmation_id": lineage["confirmation_id"]},
            "generated_at": instant,
            "header": {"contract_name": payload["contract_intent"]["implementation_objective"], "version_label": f"v{record['artifact_version']}.{record['record_revision']}", "readiness": governance["contract_readiness"]["result"], "downstream_handoff_allowed": governance["contract_readiness"]["downstream_handoff_allowed"], "component_count": len(payload["components"]), "interface_count": len(payload["interfaces"]), "build_unit_count": len(payload["build_units"]), "open_blocking_obligation_count": sum(x["status"] == "open" and x["blocking"] for x in obligations), "open_nonblocking_obligation_count": sum(x["status"] == "open" and not x["blocking"] for x in obligations), "short_payload_hash": record["payload_hash"].removeprefix("sha256:")[:12]},
            "contract_content": payload,
            "governance_summary": {"quality_contract_version": governance["quality_contract"]["version"], "readiness_audit_id": governance["contract_readiness"]["audit_id"], "approvals": [{"domain": x["domain"], "decision": x["decision"], "actor_ref": x["actor_ref"], "approved_at": x["approved_at"]} for x in governance["approvals"]], "handoff_authorization_id": None if governance["handoff_authorization"] is None else governance["handoff_authorization"]["authorization_id"], "handoff_restrictions": governance["contract_readiness"]["handoff_reasons"]},
            "queues": {"blocking_obligations": [queue_entry(x, x["question_or_obligation"], x["downstream_effect"], str(x["owner"]), x["due_date"], _pointer("review_obligations", i)) for i, x in enumerate(obligations) if x["blocking"] and x["status"] == "open"], "later_review": [queue_entry(x, x["question_or_obligation"], x["downstream_effect"], str(x["owner"]), x["due_date"], _pointer("review_obligations", i)) for i, x in enumerate(obligations) if not x["blocking"] and x["status"] == "open"], "open_dependencies": [queue_entry(x, x["choice"], x["purpose"], str(x["owner"]), x["due_date"], _pointer("substrate_dependencies", i)) for i, x in enumerate(dependencies) if x["status"] != "confirmed"], "rejected_or_superseded_decisions": [queue_entry(x, x["question"], x["rationale"], str(x["owner"]), "9999-12-31", _pointer("engineering_decisions", i)) for i, x in enumerate(rejected) if x["status"] in {"rejected", "superseded"}]},
            "evidence_index": evidence, "projection_integrity": integrity,
        }
    view["projection_integrity"]["view_hash"] = artifact_review_view_hash(view)
    validate_exact(
        "final-spec-package-view.schema.json" if artifact_type == "SPEC_PACKAGE" else "technical-contract-view.schema.json",
        view,
    )
    return view
