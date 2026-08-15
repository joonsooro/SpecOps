"""Typed, one-attempt quality revision of an existing artifact payload."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Callable
from uuid import UUID, uuid4

from specops_contracts import artifact_quality_v1 as q
from specops_contracts.canonical import domain_hash, payload_hash


def prepare_quality_revision_request(
    *,
    artifact_id: UUID,
    artifact_version: int,
    record_revision: int,
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
        "payload_hash": payload_hash(payload),
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
        "actors": revised.get("actors", []),
        "decisions": revised.get("decisions", []),
    }
    if domain_hash(
        "SPECOPS:ARTIFACT_IMMUTABLE_PROJECTION:v1", immutable_projection
    ) != request.immutable_projection_hash:
        raise ValueError("quality revision changed Foundation-owned projection")

    original = identity_kinds(payload)
    updated = identity_kinds(revised)
    allocated = {
        str(item.foundation_id): item.entity_kind for item in request.allocated_identities
    }
    new_identities = set(updated) - set(original)
    if new_identities != set(allocated) or any(
        updated[identity] != allocated[identity] for identity in new_identities
    ):
        raise ValueError("quality revision used identities outside its exact allocation")
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
    tokens = [part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")]
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
