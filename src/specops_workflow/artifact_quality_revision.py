"""Typed, one-attempt quality revision of an existing artifact payload."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Callable
from uuid import UUID, uuid4

from specops_contracts import artifact_quality_v1 as q
from specops_contracts.canonical import domain_hash, payload_hash
from specops_contracts.canonical import canonical_bytes


def normalize_quality_revision_candidate_wire(
    *,
    raw_candidate_json: str,
    payload: dict,
    excluded_pointer_prefixes: tuple[str, ...] = (),
) -> tuple[q.ArtifactQualityRevisionCandidate, dict[str, tuple[str, ...]]]:
    """Normalize only JSON-in-string wire values at known string leaves.

    Provider proposals outside the caller's declared focus are discarded before
    validation. An invalid ``replacement_value_json`` is normalized only when
    its exact existing target is a string, making the intended replacement
    unambiguous without interpreting or repairing its content.
    """

    value = json.loads(raw_candidate_json)
    patches = value.get("patches") if isinstance(value, dict) else None
    if not isinstance(patches, list):
        raise ValueError("quality revision wire candidate has no patch list")
    retained: list[dict[str, Any]] = []
    excluded: list[str] = []
    normalized: list[str] = []
    for patch in patches:
        if not isinstance(patch, dict) or not isinstance(patch.get("pointer"), str):
            raise ValueError("quality revision wire patch is invalid")
        pointer = patch["pointer"]
        if any(
            pointer == prefix or pointer.startswith(prefix + "/")
            for prefix in excluded_pointer_prefixes
        ):
            excluded.append(pointer)
            continue
        replacement = patch.get("replacement_value_json")
        if not isinstance(replacement, str):
            raise ValueError("quality revision replacement is not a string")
        try:
            json.loads(replacement)
        except json.JSONDecodeError:
            if not isinstance(_resolve_pointer(payload, pointer), str):
                raise ValueError(
                    "quality revision replacement is ambiguous non-JSON content"
                ) from None
            patch = {**patch, "replacement_value_json": json.dumps(replacement)}
            normalized.append(pointer)
        retained.append(patch)
    value["patches"] = retained
    return q.ArtifactQualityRevisionCandidate.model_validate_json(json.dumps(value)), {
        "excluded_pointers": tuple(excluded),
        "normalized_string_pointers": tuple(normalized),
    }


def quality_revision_pointer_closure(
    *,
    payload: dict,
    finding_pointers: tuple[str, ...],
    allocated_identity_kinds: tuple[str, ...],
) -> tuple[str, ...]:
    """Add only the reference closure needed by Foundation-allocated splits."""

    result = set(finding_pointers)
    allocated = set(allocated_identity_kinds)
    collections = {
        "ACTOR": ("actors",),
        "REQUIREMENT": ("requirements",),
        "ACCEPTANCE_CHECK": ("acceptance_checks",),
        "GLOSSARY_TERM": ("glossary",),
        "EXPERIENCE_STATE": ("experience_states",),
        "SCENARIO": ("scenarios",),
        "ARCHITECTURE_NODE": ("architecture_context", "nodes"),
        "COMPONENT": ("components",),
        "INTERFACE": ("interfaces",),
        "DATA_CONTRACT": ("data_contracts",),
        "WORKFLOW": ("workflows",),
        "FAILURE_CONTRACT": ("failure_contracts",),
        "QUALITY_BUDGET": ("quality_budgets",),
        "SUBSTRATE_DEPENDENCY": ("substrate_dependencies",),
        "ROLLOUT_STEP": ("rollout_migration_recovery", "rollout_steps"),
        "BUILD_UNIT": ("build_units",),
        "VERIFICATION_ITEM": ("verification_plan",),
        "ENGINEERING_DECISION": ("engineering_decisions",),
        "REVIEW_OBLIGATION": ("review_obligations",),
    }

    def resolve_collection(path: tuple[str, ...]) -> list[Any]:
        value: Any = payload
        for token in path:
            value = value.get(token) if isinstance(value, dict) else None
        if not isinstance(value, list):
            raise ValueError("quality revision split collection does not resolve")
        return value

    def collection_pointer(path: tuple[str, ...]) -> str:
        return "/" + "/".join(_escape_pointer_token(token) for token in path)

    anchor_ids: set[str] = set()
    requirement_anchors: list[dict[str, Any]] = []
    for kind, collection_path in collections.items():
        if kind not in allocated:
            continue
        values = resolve_collection(collection_path)
        result.add(collection_pointer(collection_path))
        for pointer in finding_pointers:
            tokens = _pointer_tokens(pointer)
            if (
                len(tokens) <= len(collection_path)
                or tuple(tokens[: len(collection_path)]) != collection_path
            ):
                continue
            try:
                item = values[int(tokens[len(collection_path)])]
            except (IndexError, TypeError, ValueError):
                raise ValueError("quality revision split pointer does not resolve") from None
            identity = item.get("id") if isinstance(item, dict) else None
            if not isinstance(identity, str):
                raise ValueError("quality revision split pointer has no identity")
            anchor_ids.add(identity)
            if collection_path == ("requirements",):
                requirement_anchors.append(item)

    if "ACCEPTANCE_CHECK" in allocated:
        for requirement in requirement_anchors:
            refs = requirement.get("acceptance_check_refs", [])
            if not isinstance(refs, list) or any(
                not isinstance(value, str) for value in refs
            ):
                raise ValueError("quality revision acceptance-check refs are invalid")
            anchor_ids.update(refs)

    if allocated.intersection(
        {"REQUIREMENT", "ACCEPTANCE_CHECK", "EXPERIENCE_STATE", "SCENARIO"}
    ):
        result.add("/traceability")
    if "ACCEPTANCE_CHECK" in allocated:
        package_items = payload.get("package_items", [])
        if not isinstance(package_items, list):
            raise ValueError("quality revision package-item collection does not resolve")
        result.update(
            f"/package_items/{index}" for index in range(len(package_items))
        )

    for key, value in payload.items():
        if key == "decisions":
            continue
        escaped_key = _escape_pointer_token(key)
        if isinstance(value, list):
            for index, item in enumerate(value):
                if _contains_exact_value(item, anchor_ids):
                    result.add(f"/{escaped_key}/{index}")
        elif _contains_exact_value(value, anchor_ids):
            result.add(f"/{escaped_key}")
    return tuple(sorted(result))


def quality_revision_enum_constraints(
    schema: dict[str, Any],
) -> dict[str, tuple[str | int | float | bool | None, ...]]:
    """Project canonical enum vocabularies for JSON-in-string revision patches.

    Structured Outputs can validate the revision envelope but cannot inspect a
    JSON document encoded inside ``replacement_value_json``.  This compact,
    deterministic projection gives the provider the canonical vocabulary while
    the unmodified artifact schema remains authoritative during admission.
    """

    definitions = schema.get("$defs", {})
    if not isinstance(definitions, dict):
        raise ValueError("artifact schema definitions are invalid")
    result: dict[str, tuple[str | int | float | bool | None, ...]] = {}

    def walk(value: Any, pointer: str, resolving: frozenset[str]) -> None:
        if not isinstance(value, dict):
            return
        reference = value.get("$ref")
        if isinstance(reference, str):
            prefix = "#/$defs/"
            if not reference.startswith(prefix):
                raise ValueError("artifact enum projection has an external reference")
            name = reference[len(prefix) :]
            target = definitions.get(name)
            if not isinstance(target, dict):
                raise ValueError("artifact enum projection reference is unresolved")
            if name not in resolving:
                walk(target, pointer, resolving | {name})
        enum = value.get("enum")
        if isinstance(enum, list):
            if not pointer or not enum or any(
                not isinstance(item, (str, int, float, bool)) and item is not None
                for item in enum
            ):
                raise ValueError("artifact enum projection contains an invalid enum")
            projected = tuple(enum)
            existing = result.get(pointer)
            if existing is not None and existing != projected:
                raise ValueError("artifact enum projection is ambiguous")
            result[pointer] = projected
        properties = value.get("properties")
        if isinstance(properties, dict):
            for name, child in properties.items():
                walk(child, f"{pointer}/{_escape_pointer_token(name)}", resolving)
        items = value.get("items")
        if isinstance(items, dict):
            walk(items, f"{pointer}/*", resolving)
        for keyword in ("allOf", "anyOf", "oneOf"):
            variants = value.get(keyword)
            if isinstance(variants, list):
                for child in variants:
                    walk(child, pointer, resolving)

    walk(schema, "", frozenset())
    return dict(sorted(result.items()))


def prepare_quality_revision_request(
    *,
    artifact_id: UUID,
    artifact_version: int,
    record_revision: int,
    based_on_case_revision: int,
    payload: dict,
    audit_id: UUID,
    finding_ids: tuple[UUID, ...],
    failed_rule_ids: tuple[str, ...],
    canonical_artifact_pointers: tuple[str, ...],
    allocated_identity_kinds: tuple[str, ...],
    new_id: Callable[[], UUID] = uuid4,
) -> q.ArtifactQualityRevisionRequest:
    """Bind one bounded repair to exact findings, rules, pointers, and payload."""

    immutable_projection = {
        "actors": payload.get("actors", []),
        "decisions": payload.get("decisions", []),
    }
    base = {
        "protocol_version": q.PROTOCOL_VERSION,
        "revision_request_id": new_id(),
        "revision_request_version": 1,
        "request_hash": "sha256:" + "0" * 64,
        "artifact_id": artifact_id,
        "artifact_version": artifact_version,
        "record_revision": record_revision,
        "based_on_case_revision": based_on_case_revision,
        "payload_hash": payload_hash(payload),
        "canonical_payload_json": canonical_bytes(payload).decode("utf-8"),
        "audit_id": audit_id,
        "finding_ids": finding_ids,
        "failed_rule_ids": failed_rule_ids,
        "canonical_artifact_pointers": canonical_artifact_pointers,
        "allocated_identities": tuple(
            q.AllocatedRevisionIdentity(
                foundation_id=new_id(),
                foundation_version=1,
                entity_kind=kind,
            )
            for kind in allocated_identity_kinds
        ),
        "immutable_projection_hash": domain_hash(
            "SPECOPS:ARTIFACT_IMMUTABLE_PROJECTION:v1", immutable_projection
        ),
        "max_revision_attempts": 1,
    }
    base["request_hash"] = domain_hash(
        "SPECOPS:ARTIFACT_QUALITY_REVISION_REQUEST:v1",
        {key: value for key, value in base.items() if key != "request_hash"},
    )
    return q.ArtifactQualityRevisionRequest(**base)


def apply_quality_revision_candidate(
    *,
    payload: dict,
    request: q.ArtifactQualityRevisionRequest,
    candidate: q.ArtifactQualityRevisionCandidate,
    identity_kinds: Callable[[dict], dict[str, str]],
) -> dict:
    """Apply only pointer-bounded edits while preserving server-owned records."""

    echoes = (
        candidate.revision_request_id == request.revision_request_id,
        candidate.revision_request_version == request.revision_request_version,
        candidate.request_hash == request.request_hash,
        candidate.artifact_id == request.artifact_id,
        candidate.artifact_version == request.artifact_version,
        candidate.record_revision == request.record_revision,
        candidate.payload_hash == request.payload_hash,
        candidate.attempt <= request.max_revision_attempts,
        payload_hash(payload) == request.payload_hash,
        canonical_bytes(payload).decode("utf-8") == request.canonical_payload_json,
    )
    if not all(echoes):
        raise ValueError("quality revision candidate binding changed")
    allowed = set(request.canonical_artifact_pointers)
    if any(item.pointer not in allowed for item in candidate.patches):
        raise ValueError("quality revision patch is outside admitted artifact pointers")

    revised = deepcopy(payload)
    for patch in candidate.patches:
        _replace_pointer(revised, patch.pointer, json.loads(patch.replacement_value_json))
    immutable_projection = {
        "actors": payload.get("actors", []),
        "decisions": payload.get("decisions", []),
    }
    if domain_hash(
        "SPECOPS:ARTIFACT_IMMUTABLE_PROJECTION:v1", immutable_projection
    ) != request.immutable_projection_hash:
        raise ValueError("quality revision base Foundation projection changed")

    original_actors = payload.get("actors", [])
    revised_actors = revised.get("actors", [])
    if revised.get("decisions", []) != payload.get("decisions", []):
        raise ValueError("quality revision changed Foundation-owned decisions")
    if not isinstance(original_actors, list) or not isinstance(revised_actors, list):
        raise ValueError("quality revision actor collection is invalid")
    expected_existing_actors = deepcopy(original_actors)
    expected_document = {"actors": expected_existing_actors}
    for pointer in request.canonical_artifact_pointers:
        tokens = _pointer_tokens(pointer)
        if (
            len(tokens) == 4
            and tokens[0] == "actors"
            and tokens[2] == "responsibilities"
        ):
            try:
                replacement = _resolve_pointer(revised, pointer)
                _replace_pointer(expected_document, pointer, replacement)
            except (IndexError, KeyError, TypeError, ValueError):
                raise ValueError(
                    "quality revision actor responsibility pointer does not resolve"
                ) from None
    if revised_actors[: len(original_actors)] != expected_existing_actors:
        raise ValueError("quality revision changed an existing Foundation-owned actor")
    allocated_actor_ids = {
        str(item.foundation_id)
        for item in request.allocated_identities
        if item.entity_kind == "ACTOR"
    }
    appended_actor_ids = {
        item.get("id")
        for item in revised_actors[len(original_actors) :]
        if isinstance(item, dict)
    }
    if not appended_actor_ids.issubset(allocated_actor_ids) or any(
        not isinstance(item, dict)
        for item in revised_actors[len(original_actors) :]
    ):
        raise ValueError("quality revision actor addition differs from Foundation allocation")

    original = identity_kinds(payload)
    updated = identity_kinds(revised)
    allocated = {
        str(item.foundation_id): item.entity_kind for item in request.allocated_identities
    }
    new_identities = set(updated) - set(original)
    if not new_identities.issubset(allocated) or any(
        updated[identity] != allocated[identity] for identity in new_identities
    ):
        raise ValueError("quality revision used identities outside its allocation ceiling")
    if any(updated.get(identity) != kind for identity, kind in original.items()):
        raise ValueError("quality revision removed or retyped an existing identity")
    return revised


def require_monotonic_quality_improvement(
    *,
    prior_finding_ids: tuple[UUID, ...],
    prior_failed_rule_ids: tuple[str, ...],
    revised_finding_ids: tuple[UUID, ...],
    revised_failed_rule_ids: tuple[str, ...],
) -> None:
    """Reject a bounded revision that adds or fails to reduce quality defects."""

    prior_findings = set(prior_finding_ids)
    revised_findings = set(revised_finding_ids)
    prior_rules = set(prior_failed_rule_ids)
    revised_rules = set(revised_failed_rule_ids)
    if (
        not revised_findings.issubset(prior_findings)
        or not revised_rules.issubset(prior_rules)
        or (
            len(revised_findings) >= len(prior_findings)
            and len(revised_rules) >= len(prior_rules)
        )
    ):
        raise ValueError("quality revision did not monotonically reduce admitted defects")


def _replace_pointer(document: dict, pointer: str, replacement) -> None:
    if not pointer or not pointer.startswith("/"):
        raise ValueError("quality revision cannot replace the complete artifact root")
    tokens = _pointer_tokens(pointer)
    target = document
    for token in tokens[:-1]:
        target = target[int(token)] if isinstance(target, list) else target[token]
    final = tokens[-1]
    if isinstance(target, list):
        target[int(final)] = replacement
    else:
        if final not in target:
            raise ValueError("quality revision pointer does not resolve")
        target[final] = replacement


def _resolve_pointer(document: dict, pointer: str):
    target: Any = document
    for token in _pointer_tokens(pointer):
        target = target[int(token)] if isinstance(target, list) else target[token]
    return target


def _pointer_tokens(pointer: str) -> list[str]:
    if not pointer or not pointer.startswith("/"):
        raise ValueError("quality revision pointer must be a non-root JSON pointer")
    return [
        part.replace("~1", "/").replace("~0", "~")
        for part in pointer[1:].split("/")
    ]


def _escape_pointer_token(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _contains_exact_value(value: Any, expected: set[str]) -> bool:
    if isinstance(value, dict):
        return any(_contains_exact_value(item, expected) for item in value.values())
    if isinstance(value, list):
        return any(_contains_exact_value(item, expected) for item in value)
    return isinstance(value, str) and value in expected
