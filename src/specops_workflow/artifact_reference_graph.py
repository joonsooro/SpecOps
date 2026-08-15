"""Content-safe, deterministic reference-graph validation for artifact payloads."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable
from uuid import UUID


_IGNORED_BINDING_KEYS = {
    "confirmation_id",
    "decision_batch_view_id",
    "review_item_id",
    "authority_validation_id",
    "foundation_validation_id",
    "delegation_ref",
    "transcript_event_id",
}
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
class ArtifactReferenceIssue:
    """One unresolved UUID reference, identified without exposing its value."""

    pointer: str
    code: str = "UNRESOLVED_REFERENCE"


def validate_artifact_reference_graph(
    payload: dict[str, Any], *, allowed_reference_ids: Iterable[str]
) -> tuple[ArtifactReferenceIssue, ...]:
    """Return stable JSON-pointer diagnostics for unresolved UUID references.

    Planned-but-unused identities are intentionally absent from
    ``allowed_reference_ids``. A candidate can reference an identity only when
    that identity is materialized in this payload or is already owned by
    Foundation state.
    """

    allowed = {_canonical_uuid(value) for value in allowed_reference_ids}
    issues: list[ArtifactReferenceIssue] = []

    def walk(value: Any, path: tuple[str, ...], key: str = "") -> None:
        if isinstance(value, dict):
            for child_key, child in value.items():
                if child_key == "id" or child_key in _IGNORED_BINDING_KEYS:
                    continue
                walk(child, (*path, child_key), child_key)
            return
        if isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, (*path, str(index)), key)
            return
        if not isinstance(value, str) or not (
            key in _REFERENCE_KEYS or key.endswith("_ref") or key.endswith("_refs")
        ):
            return
        try:
            referenced = _canonical_uuid(value)
        except ValueError:
            return
        if referenced not in allowed:
            issues.append(
                ArtifactReferenceIssue(
                    pointer="/" + "/".join(_escape_pointer(part) for part in path)
                )
            )

    walk(payload, ())
    return tuple(issues)


def _canonical_uuid(value: str) -> str:
    return str(UUID(value))


def _escape_pointer(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")
