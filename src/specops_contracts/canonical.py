"""RFC 8785 canonicalization and Workshop Protocol hash recipes."""

from __future__ import annotations

import hashlib
import unicodedata
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import UUID

import rfc8785
from pydantic import BaseModel


def _json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _json_value(value.model_dump(mode="json", exclude_none=False))
    if isinstance(value, Enum):
        return _json_value(value.value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("canonical timestamps must be timezone-aware")
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def canonical_bytes(value: Any) -> bytes:
    return rfc8785.dumps(_json_value(value))


def domain_hash(domain: str, value: Any) -> str:
    digest = hashlib.sha256(domain.encode("utf-8") + b"\x00" + canonical_bytes(value)).hexdigest()
    return "sha256:" + digest


def payload_hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


def analyzer_request_hash(value: dict[str, Any]) -> str:
    material = {key: item for key, item in value.items() if key != "request_hash"}
    return domain_hash("SPECOPS:ANALYZER_REQUEST:v1", material)


def foundation_command_fingerprint(value: dict[str, Any]) -> str:
    omitted = {
        "command_id",
        "idempotency_key",
        "correlation_id",
        "causation_id",
        "issued_at",
    }
    material = {key: item for key, item in value.items() if key not in omitted}
    return domain_hash("SPECOPS:FOUNDATION_COMMAND:v1", material)


def decision_view_hash(value: dict[str, Any]) -> str:
    material = {key: item for key, item in value.items() if key != "view_hash"}
    return domain_hash("SPECOPS:DECISION_BATCH_VIEW:v1", material)


def transcript_hash(text: str) -> str:
    normalized = unicodedata.normalize("NFC", text)
    material = b"SPECOPS:TRANSCRIPT:v1\x00" + normalized.encode("utf-8")
    return "sha256:" + hashlib.sha256(material).hexdigest()


def artifact_review_view_hash(value: dict[str, Any]) -> str:
    material = _json_value(value)
    integrity = dict(material["projection_integrity"])
    integrity.pop("view_hash", None)
    material = dict(material)
    material["projection_integrity"] = integrity
    return "sha256:" + hashlib.sha256(canonical_bytes(material)).hexdigest()
