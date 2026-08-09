from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from .sources import SourceCatalog, SourceName


DEV_LEAD_ACTOR_ID = UUID("11111111-1111-4111-8111-111111111111")
PM_ACTOR_ID = UUID("22222222-2222-4222-8222-222222222222")
EXPECTED_ATTACHMENT_HASH = "c139587efad026568be17538ca8ac1ea843fbb92d3dcbac519f2f52107171280"
REQUIRED_COMMAND_SCOPE = (
    "approve_projection_plan",
    "approve_spec_package_item",
    "create_projection_plan",
    "create_spec_package",
    "mark_spec_package_item_ready",
    "record_ambiguity_finding",
    "register_source_artifact",
    "resolve_ambiguity_finding",
    "revise_projection_plan",
    "revise_spec_package",
)


class DelegationFixture(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    message_id: str
    delegator_actor_id: UUID
    delegate_actor_id: UUID
    domain: Literal["TECHNICAL"]
    valid_from: datetime
    valid_until: datetime
    command_scope: tuple[str, ...]
    attachment_path: Path
    attachment_media_type: str
    attachment_hash: str
    later_review_required: Literal[True]


def load_delegation_fixture(
    catalog: SourceCatalog,
    *,
    now: datetime,
) -> DelegationFixture:
    if now.tzinfo is None:
        raise ValueError("fixture validation requires a timezone-aware instant")
    raw = catalog.read_text(SourceName.DELEGATION_FIXTURE)
    frontmatter = _frontmatter(raw)
    expected_scalars = {
        "fixture_version": "1",
        "message_id": "specops-demo-delegation-2026-08-08",
        "from": "dev-lead@specops.demo",
        "to": "specops-agent@specops.demo",
        "delegate": "pm@specops.demo",
        "delegator_actor_id": str(DEV_LEAD_ACTOR_ID),
        "delegate_actor_id": str(PM_ACTOR_ID),
        "domain": "TECHNICAL",
        "valid_from": "2026-08-08T00:00:00Z",
        "valid_until": "2026-08-22T23:59:59Z",
        "later_review_required": "true",
    }
    for key, expected in expected_scalars.items():
        if frontmatter["scalars"].get(key) != expected:
            raise ValueError(f"delegation fixture {key} mismatch")
    if set(frontmatter["scalars"]) != set(expected_scalars):
        raise ValueError("delegation fixture contains unexpected fields")
    commands = tuple(sorted(frontmatter["commands"]))
    if commands != REQUIRED_COMMAND_SCOPE:
        raise ValueError("delegation fixture command scope mismatch")
    attachment = frontmatter["attachment"]
    technical = catalog.document(SourceName.TECHNICAL_SPEC)
    if attachment != {
        "path": str(technical.path),
        "media_type": technical.media_type,
        "sha256": EXPECTED_ATTACHMENT_HASH,
    }:
        raise ValueError("delegation fixture attachment mismatch")
    if catalog.digest(SourceName.TECHNICAL_SPEC) != EXPECTED_ATTACHMENT_HASH:
        raise ValueError("delegation attachment content hash mismatch")
    valid_from = _instant(frontmatter["scalars"]["valid_from"])
    valid_until = _instant(frontmatter["scalars"]["valid_until"])
    current = now.astimezone(timezone.utc)
    if not valid_from <= current <= valid_until:
        raise ValueError("delegation fixture is not active at the foundation clock")
    return DelegationFixture(
        message_id=frontmatter["scalars"]["message_id"],
        delegator_actor_id=DEV_LEAD_ACTOR_ID,
        delegate_actor_id=PM_ACTOR_ID,
        domain="TECHNICAL",
        valid_from=valid_from,
        valid_until=valid_until,
        command_scope=commands,
        attachment_path=technical.path,
        attachment_media_type=technical.media_type,
        attachment_hash=EXPECTED_ATTACHMENT_HASH,
        later_review_required=True,
    )


def _instant(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("delegation fixture contains an invalid instant") from exc
    if parsed.tzinfo is None:
        raise ValueError("delegation fixture instants must include a timezone")
    return parsed.astimezone(timezone.utc)


def _frontmatter(raw: str) -> dict[str, object]:
    lines = raw.splitlines()
    if len(lines) < 4 or lines[0] != "---":
        raise ValueError("delegation fixture frontmatter is missing")
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise ValueError("delegation fixture frontmatter is unterminated") from exc
    scalars: dict[str, str] = {}
    commands: list[str] = []
    attachment: dict[str, str] = {}
    section: str | None = None
    for line in lines[1:end]:
        if line == "command_scope:":
            section = "commands"
            continue
        if line == "attachment:":
            section = "attachment"
            continue
        if line.startswith("  - ") and section == "commands":
            commands.append(line[4:])
            continue
        if line.startswith("  ") and section == "attachment":
            key, separator, value = line.strip().partition(": ")
            if not separator or key in attachment:
                raise ValueError("invalid delegation attachment frontmatter")
            attachment[key] = value
            continue
        if line.startswith(" ") or ": " not in line:
            raise ValueError("invalid delegation fixture frontmatter")
        key, value = line.split(": ", 1)
        if key in scalars:
            raise ValueError("duplicate delegation fixture field")
        scalars[key] = value
        section = None
    if len(commands) != len(set(commands)) or set(attachment) != {"path", "media_type", "sha256"}:
        raise ValueError("invalid delegation fixture collections")
    return {"scalars": scalars, "commands": commands, "attachment": attachment}
