"""Fail-closed semantic adapters for pre-V4 Workshop records."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, TypeVar


class MigrationRebindingRequired(ValueError):
    """Historical state is absent or ambiguous and must be re-confirmed."""


T = TypeVar("T")


def require_unique_historical_binding(values: Iterable[T], *, binding_kind: str) -> T:
    candidates = tuple(values)
    if len(candidates) != 1:
        raise MigrationRebindingRequired(
            f"{binding_kind} requires exactly one historical binding; found {len(candidates)}"
        )
    return candidates[0]


@dataclass(frozen=True)
class ReadinessMigration:
    readiness: str
    review_obligation: str


def migrate_legacy_readiness(
    legacy_state: str,
    *,
    still_being_synthesized: bool = False,
    human_decision_missing: bool = False,
    authority_or_evidence_missing: bool = False,
) -> ReadinessMigration:
    flags = sum(
        (still_being_synthesized, human_decision_missing, authority_or_evidence_missing)
    )
    if flags > 1:
        raise MigrationRebindingRequired("legacy readiness has conflicting semantic evidence")
    if still_being_synthesized:
        return ReadinessMigration("FORMULATING", "NONE")
    if human_decision_missing:
        return ReadinessMigration("NEEDS_CLARIFICATION", "DECISION_REQUIRED")
    if authority_or_evidence_missing:
        return ReadinessMigration("BLOCKED", "DECISION_REQUIRED")
    if legacy_state == "LATER_REVIEW":
        return ReadinessMigration("READY", "LATER_REVIEW")
    if legacy_state in {"READY", "NEEDS_CLARIFICATION", "BLOCKED", "FORMULATING"}:
        return ReadinessMigration(legacy_state, "NONE")
    raise MigrationRebindingRequired("unknown legacy readiness state")
