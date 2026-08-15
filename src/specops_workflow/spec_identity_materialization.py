"""Deterministic Foundation ownership of Spec Package identities and references."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable
from uuid import UUID

from specops_contracts import workshop_v1 as c


SPEC_ANALYZER_COLLECTIONS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("package_items",), "PACKAGE_ITEM"),
    (("glossary",), "GLOSSARY_TERM"),
    (("outcomes",), "OUTCOME"),
    (("scope", "in_scope"), "SCOPE_ITEM"),
    (("scope", "non_goals"), "SCOPE_ITEM"),
    (("scope", "boundaries"), "SCOPE_BOUNDARY"),
    (("journeys",), "JOURNEY"),
    (("behaviour_contract", "always"), "BEHAVIOUR_RULE"),
    (("behaviour_contract", "ask_first"), "BEHAVIOUR_RULE"),
    (("behaviour_contract", "never"), "BEHAVIOUR_RULE"),
    (("requirements",), "REQUIREMENT"),
    (("data_rules",), "DATA_RULE"),
    (("experience_states",), "EXPERIENCE_STATE"),
    (("scenarios",), "SCENARIO"),
    (("quality_attributes",), "QUALITY_ATTRIBUTE"),
    (("constraints",), "CONSTRAINT"),
    (("dependencies",), "DEPENDENCY"),
    (("risks",), "RISK"),
    (("open_items",), "OPEN_ITEM"),
    (("acceptance_checks",), "ACCEPTANCE_CHECK"),
)

# Kinds with sibling semantic collections own one disjoint ordinal partition.
# Single-path kinds implicitly own their complete Analyzer slot set.
SPEC_ANALYZER_PATH_ORDINAL_RANGES: dict[tuple[str, ...], range] = {
    ("scope", "in_scope"): range(1, 9),
    ("scope", "non_goals"): range(9, 17),
    ("behaviour_contract", "always"): range(1, 9),
    ("behaviour_contract", "ask_first"): range(9, 11),
    ("behaviour_contract", "never"): range(11, 17),
}

# Exact direct text leaves that may be proposed as evidence-backed claims.  The
# provider schema and Foundation normalization both consume this one map.  A
# collection with more than one field has no safe object-level default.
SPEC_EVIDENCE_CLAIM_TEXT_FIELDS: dict[tuple[str, ...], tuple[str, ...]] = {
    ("glossary",): ("definition",),
    ("outcomes",): ("statement",),
    ("scope", "in_scope"): ("statement",),
    ("scope", "non_goals"): ("statement",),
    ("scope", "boundaries"): ("inside", "outside"),
    ("behaviour_contract", "always"): ("obligation",),
    ("behaviour_contract", "ask_first"): ("obligation",),
    ("behaviour_contract", "never"): ("obligation",),
    ("requirements",): ("behaviour",),
    ("data_rules",): ("semantic_rule",),
    ("quality_attributes",): ("target",),
    ("constraints",): ("statement",),
    ("dependencies",): ("purpose",),
    ("risks",): ("statement",),
}

SPEC_EVIDENCE_OWNER_PATHS = frozenset(SPEC_EVIDENCE_CLAIM_TEXT_FIELDS)

_REFERENCE_KEYS = {
    "primary_customer",
    "beneficiary_actor_ref",
    "actor_ref",
    "from_ref",
    "to_ref",
    "source_id",
    "trigger_interface_ref",
    "failure_ref",
    "component_ref",
}


@dataclass(frozen=True)
class SpecIdentityMaterializationError(ValueError):
    reason: str
    pointers: tuple[str, ...] = ()

    def __str__(self) -> str:
        return self.reason


def spec_collection_slot_assignments(
    slots: Iterable[c.ArtifactIdentitySlot | c.ArtifactConstructionSlot],
) -> dict[tuple[str, ...], tuple[c.ArtifactIdentitySlot | c.ArtifactConstructionSlot, ...]]:
    """Assign every Analyzer slot to exactly one ordered canonical collection path."""

    by_kind: dict[str, list[c.ArtifactIdentitySlot | c.ArtifactConstructionSlot]] = (
        defaultdict(list)
    )
    for slot in slots:
        if slot.owner == "ANALYZER" and slot.allocation_mode == "NEW_ENTITY":
            if slot.slot_key != f"{slot.entity_kind}:{slot.ordinal:04d}":
                raise SpecIdentityMaterializationError(
                    "Analyzer slot key does not match its kind and ordinal"
                )
            by_kind[slot.entity_kind].append(slot)
    for values in by_kind.values():
        values.sort(key=lambda item: item.ordinal)
        ordinals = [item.ordinal for item in values]
        if len(ordinals) != len(set(ordinals)):
            raise SpecIdentityMaterializationError(
                "Analyzer kind contains duplicate slot ordinals"
            )

    paths_by_kind: dict[str, list[tuple[str, ...]]] = defaultdict(list)
    for path, kind in SPEC_ANALYZER_COLLECTIONS:
        paths_by_kind[kind].append(path)

    result: dict[
        tuple[str, ...],
        tuple[c.ArtifactIdentitySlot | c.ArtifactConstructionSlot, ...],
    ] = {}
    for path, kind in SPEC_ANALYZER_COLLECTIONS:
        values = by_kind.get(kind, [])
        ordinal_range = SPEC_ANALYZER_PATH_ORDINAL_RANGES.get(path)
        if len(paths_by_kind[kind]) > 1 and ordinal_range is None:
            raise SpecIdentityMaterializationError(
                "Analyzer sibling collection lacks an explicit slot partition",
                (_pointer(path),),
            )
        result[path] = tuple(
            item
            for item in values
            if ordinal_range is None or item.ordinal in ordinal_range
        )

    for kind, values in by_kind.items():
        complete = {item.slot_key for item in values}
        assigned: set[str] = set()
        for path in paths_by_kind.get(kind, []):
            path_keys = {item.slot_key for item in result[path]}
            if assigned.intersection(path_keys):
                raise SpecIdentityMaterializationError(
                    "Analyzer sibling slot partitions overlap",
                    (_pointer(path),),
                )
            assigned.update(path_keys)
        if assigned != complete:
            raise SpecIdentityMaterializationError(
                "Analyzer slot partition does not exactly cover its kind"
            )
    return result


def materialize_spec_identities(
    *,
    payload: dict[str, Any],
    proposals: tuple[c.SpecEvidenceSupportProposalCandidate, ...],
    identity_plan: c.ArtifactSynthesisIdentityPlan,
) -> tuple[
    dict[str, Any],
    tuple[c.SpecEvidenceSupportProposalCandidate, ...],
    tuple[c.ArtifactIdentityAssignment, ...],
]:
    """Assign canonical IDs and rewrite local references without semantic repair."""

    if not identity_plan.slots:
        raise SpecIdentityMaterializationError("identity plan lacks typed slots")
    assignments_by_path = spec_collection_slot_assignments(identity_plan.slots)
    all_slots = {item.slot_key: item for item in identity_plan.slots}
    new_entity_ids = {
        str(item.foundation_id)
        for item in identity_plan.slots
        if item.allocation_mode == "NEW_ENTITY"
    }
    consumed: dict[str, c.ArtifactIdentitySlot] = {}
    consumed_paths: dict[str, tuple[str, ...]] = {}
    pointer_aliases: dict[str, str] = {}
    assignment_map: list[c.ArtifactIdentityAssignment] = []

    for path, expected_slots in assignments_by_path.items():
        parent, field = _parent_for(payload, path)
        collection = parent.get(field)
        if not isinstance(collection, dict):
            raise SpecIdentityMaterializationError(
                "Analyzer collection is not a local-handle object",
                (_pointer(path),),
            )
        expected_keys = {item.slot_key for item in expected_slots}
        if set(collection) != expected_keys:
            raise SpecIdentityMaterializationError(
                "Analyzer collection does not exactly match its typed slot capacity",
                (_pointer(path),),
            )
        canonical: list[dict[str, Any]] = []
        for slot in expected_slots:
            body = collection[slot.slot_key]
            if body is None:
                continue
            if not isinstance(body, dict) or "id" in body:
                raise SpecIdentityMaterializationError(
                    "Analyzer item attempted to own a canonical identity",
                    (_pointer((*path, slot.slot_key)),),
                )
            if slot.slot_key in consumed:
                raise SpecIdentityMaterializationError(
                    "Analyzer reused one local identity slot",
                    (
                        pointer_aliases[
                            _pointer((*consumed_paths[slot.slot_key], slot.slot_key))
                        ],
                        _pointer((*path, slot.slot_key)),
                    ),
                )
            canonical_index = len(canonical)
            canonical.append({"id": str(slot.foundation_id), **body})
            consumed[slot.slot_key] = slot
            consumed_paths[slot.slot_key] = path
            local_pointer = _pointer((*path, slot.slot_key))
            canonical_pointer = _pointer((*path, str(canonical_index)))
            pointer_aliases[local_pointer] = canonical_pointer
            assignment_map.append(
                c.ArtifactIdentityAssignment(
                    slot_key=slot.slot_key,
                    entity_kind=slot.entity_kind,
                    ordinal=slot.ordinal,
                    foundation_id=slot.foundation_id,
                    canonical_pointer=canonical_pointer,
                )
            )
        parent[field] = canonical

    unresolved: list[str] = []

    def rewrite(value: Any, path: tuple[str, ...], key: str = "") -> None:
        if isinstance(value, dict):
            for child_key, child in tuple(value.items()):
                rewrite(child, (*path, child_key), child_key)
            return
        if isinstance(value, list):
            for index, child in enumerate(value):
                rewrite(child, (*path, str(index)), key)
            return
        if not isinstance(value, str) or not _is_reference_key(key):
            return
        if value in consumed:
            _replace_at(payload, path, str(consumed[value].foundation_id))
        elif value in all_slots:
            unresolved.append(_pointer(path))
        elif value in new_entity_ids:
            unresolved.append(_pointer(path))

    rewrite(payload, ())
    if unresolved:
        raise SpecIdentityMaterializationError(
            "semantic reference is ambiguous, unused, or bypasses its local handle",
            tuple(sorted(set(unresolved))),
        )

    canonical_proposals: list[c.SpecEvidenceSupportProposalCandidate] = []
    for index, proposal in enumerate(proposals):
        proposal_pointer = ("evidence_support_proposals", str(index))
        evidence = _proposal_slot(
            proposal.evidence_ref,
            all_slots,
            expected_kind="EVIDENCE",
            expected_owner="FOUNDATION",
            pointer=(*proposal_pointer, "evidence_ref"),
        )
        finding = _proposal_slot(
            proposal.finding_ref,
            all_slots,
            expected_kind="SEMANTIC_EVIDENCE_FINDING",
            expected_owner="FOUNDATION",
            pointer=(*proposal_pointer, "finding_ref"),
        )
        claim_key = str(proposal.claim_ref)
        claim = consumed.get(claim_key)
        if claim is None or consumed_paths[claim_key] not in SPEC_EVIDENCE_OWNER_PATHS:
            raise SpecIdentityMaterializationError(
                "evidence claim handle is absent or not evidence-owning",
                (_pointer((*proposal_pointer, "claim_ref")),),
            )
        local_prefix = _pointer((*consumed_paths[claim_key], claim_key))
        if not (
            proposal.claim_pointer == local_prefix
            or proposal.claim_pointer.startswith(local_prefix + "/")
        ):
            raise SpecIdentityMaterializationError(
                "claim pointer does not bind its exact local claim handle",
                (_pointer((*proposal_pointer, "claim_pointer")),),
            )
        canonical_prefix = pointer_aliases[local_prefix]
        canonical_proposals.append(
            proposal.model_copy(
                update={
                    "evidence_ref": evidence.foundation_id,
                    "finding_ref": finding.foundation_id,
                    "claim_ref": claim.foundation_id,
                    "claim_pointer": canonical_prefix
                    + proposal.claim_pointer[len(local_prefix) :],
                }
            )
        )

    return payload, tuple(canonical_proposals), tuple(assignment_map)


def validate_materialized_assignment(
    *,
    payload: dict[str, Any],
    identity_plan: c.ArtifactSynthesisIdentityPlan,
    assignment_map: tuple[c.ArtifactIdentityAssignment, ...],
) -> None:
    """Prove an assignment receipt exactly matches the canonical payload and plan."""

    slots = {item.slot_key: item for item in identity_plan.slots}
    seen: set[str] = set()
    for assignment in assignment_map:
        slot = slots.get(assignment.slot_key)
        if (
            slot is None
            or slot.owner != "ANALYZER"
            or slot.allocation_mode != "NEW_ENTITY"
            or assignment.slot_key in seen
            or assignment.entity_kind != slot.entity_kind
            or assignment.ordinal != slot.ordinal
            or assignment.foundation_id != slot.foundation_id
        ):
            raise SpecIdentityMaterializationError("assignment map does not match its plan")
        item = _value_at_pointer(payload, assignment.canonical_pointer)
        if not isinstance(item, dict) or item.get("id") != str(slot.foundation_id):
            raise SpecIdentityMaterializationError(
                "assignment map does not match the canonical payload",
                (assignment.canonical_pointer,),
            )
        seen.add(assignment.slot_key)

    canonical_analyzer_ids = {
        identity
        for identity, kind in _identity_kinds(payload).items()
        if any(
            str(slot.foundation_id) == identity
            and slot.entity_kind == kind
            and slot.owner == "ANALYZER"
            and slot.allocation_mode == "NEW_ENTITY"
            for slot in identity_plan.slots
        )
    }
    if canonical_analyzer_ids != {
        str(item.foundation_id) for item in assignment_map
    }:
        raise SpecIdentityMaterializationError(
            "assignment map does not exactly cover materialized Analyzer identities"
        )


def derive_materialized_assignment(
    *, payload: dict[str, Any], identity_plan: c.ArtifactSynthesisIdentityPlan
) -> tuple[c.ArtifactIdentityAssignment, ...]:
    """Derive the deterministic receipt for an already-canonical internal fixture."""

    slots_by_identity = {
        str(item.foundation_id): item
        for item in identity_plan.slots
        if item.owner == "ANALYZER" and item.allocation_mode == "NEW_ENTITY"
    }
    result: list[c.ArtifactIdentityAssignment] = []
    for path, kind in SPEC_ANALYZER_COLLECTIONS:
        parent, field = _parent_for(payload, path)
        collection = parent.get(field)
        if not isinstance(collection, list):
            continue
        for index, item in enumerate(collection):
            identity = item.get("id") if isinstance(item, dict) else None
            slot = slots_by_identity.get(identity) if isinstance(identity, str) else None
            if slot is None or slot.entity_kind != kind:
                continue
            result.append(
                c.ArtifactIdentityAssignment(
                    slot_key=slot.slot_key,
                    entity_kind=slot.entity_kind,
                    ordinal=slot.ordinal,
                    foundation_id=slot.foundation_id,
                    canonical_pointer=_pointer((*path, str(index))),
                )
            )
    result.sort(key=lambda item: (item.entity_kind, item.ordinal))
    assignment_map = tuple(result)
    validate_materialized_assignment(
        payload=payload,
        identity_plan=identity_plan,
        assignment_map=assignment_map,
    )
    return assignment_map


def _proposal_slot(
    value: UUID | str,
    slots: dict[str, c.ArtifactIdentitySlot],
    *,
    expected_kind: str,
    expected_owner: str,
    pointer: tuple[str, ...],
) -> c.ArtifactIdentitySlot:
    slot = slots.get(str(value))
    if (
        slot is None
        or slot.entity_kind != expected_kind
        or slot.owner != expected_owner
        or slot.allocation_mode != "NEW_ENTITY"
    ):
        raise SpecIdentityMaterializationError(
            "evidence proposal does not use its typed local handle",
            (_pointer(pointer),),
        )
    return slot


def _parent_for(payload: dict[str, Any], path: tuple[str, ...]) -> tuple[dict[str, Any], str]:
    current: Any = payload
    for field in path[:-1]:
        if not isinstance(current, dict) or not isinstance(current.get(field), dict):
            raise SpecIdentityMaterializationError(
                "Analyzer collection parent is malformed", (_pointer(path),)
            )
        current = current[field]
    return current, path[-1]


def _replace_at(payload: dict[str, Any], path: tuple[str, ...], value: str) -> None:
    parent: Any = payload
    for part in path[:-1]:
        parent = parent[int(part)] if isinstance(parent, list) else parent[part]
    if isinstance(parent, list):
        parent[int(path[-1])] = value
    else:
        parent[path[-1]] = value


def _value_at_pointer(payload: dict[str, Any], pointer: str) -> Any:
    value: Any = payload
    for encoded in pointer.removeprefix("/").split("/"):
        part = encoded.replace("~1", "/").replace("~0", "~")
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


def _is_reference_key(key: str) -> bool:
    return key in _REFERENCE_KEYS or key.endswith("_ref") or key.endswith("_refs")


def _pointer(path: tuple[str, ...]) -> str:
    return "/" + "/".join(part.replace("~", "~0").replace("/", "~1") for part in path)


def _identity_kinds(payload: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for path, kind in SPEC_ANALYZER_COLLECTIONS:
        parent, field = _parent_for(payload, path)
        collection = parent.get(field)
        if isinstance(collection, list):
            for item in collection:
                if isinstance(item, dict) and isinstance(item.get("id"), str):
                    result[item["id"]] = kind
    return result
