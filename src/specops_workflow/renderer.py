from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from .errors import DomainError, ErrorCode
from .models import StructuredWorkBody

SENTINEL_JIRA_KEY = "AAAAAAAAAA-999999999999999999"


def render_structured_body(body: StructuredWorkBody, dependency_keys: Mapping[UUID, str], *, jira_key: str | None = None, sizing: bool = False) -> str:
    resolved_jira = jira_key or body.jira_key or (SENTINEL_JIRA_KEY if sizing else "NONE")
    lines = [
        f"Spec-Package: {body.package_id}@{body.package_version}",
        f"Spec-Hash: sha256:{body.package_hash}",
        f"Generation-Key: {body.generation_key}",
        f"Jira-Key: {resolved_jira}",
        "Source-Units:",
    ]
    lines.extend(f"- {value}" for value in sorted(body.source_unit_ids, key=lambda value: value.bytes))
    lines.append("Acceptance:")
    if body.acceptance_checks:
        lines.extend(f"- [ ] {item.check_id}: {item.statement}" for item in sorted(body.acceptance_checks, key=lambda value: value.check_id.bytes))
    else: lines.append("- NONE")
    lines.append("Dependencies:")
    if body.dependency_item_ids:
        for item_id in sorted(body.dependency_item_ids, key=lambda value: value.bytes):
            key = dependency_keys.get(item_id)
            if key is None:
                if not sizing: raise DomainError(ErrorCode.INVALID_PROJECTION_PLAN, "unconfirmed dependency Jira key")
                key = SENTINEL_JIRA_KEY
            lines.append(f"- {key}")
    else: lines.append("- NONE")
    lines.append(f"Provisional: {'YES' if body.provisional else 'NO'}")
    rendered = "\n".join(lines) + "\n"
    if len(rendered) > 250_000: raise DomainError(ErrorCode.INVALID_PROJECTION_PLAN, "rendered body exceeds 250000 code points")
    return rendered

