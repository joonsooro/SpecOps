from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from .enums import ArtifactKind
from .errors import DomainError, ErrorCode


def canonical_data(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return canonical_data(value.model_dump(mode="python", by_alias=True, exclude_none=False))
    if isinstance(value, Enum): return value.value
    if isinstance(value, UUID): return str(value).lower()
    if isinstance(value, datetime):
        if value.tzinfo is None: raise ValueError("naive datetime is not canonical UTC")
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    if isinstance(value, str): return unicodedata.normalize("NFC", value)
    if value is None or isinstance(value, (bool, int)): return value
    if isinstance(value, float):
        if not math.isfinite(value): raise ValueError("non-finite numbers are forbidden")
        raise ValueError("floats are outside CanonicalJSON")
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value): raise ValueError("object keys must be strings")
        return {unicodedata.normalize("NFC", key): canonical_data(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)): return [canonical_data(item) for item in value]
    raise TypeError(f"unsupported canonical value: {type(value).__name__}")


def canonical_json(value: Any) -> bytes:
    return json.dumps(canonical_data(value), ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def artifact_hash(kind: ArtifactKind, content_schema_version: int, hash_schema_version: int, semantic_payload: Any) -> str:
    return sha256({
        "artifact_kind": kind.value,
        "content_schema_version": content_schema_version,
        "hash_schema_version": hash_schema_version,
        "semantic_payload": semantic_payload,
    })


@dataclass(frozen=True)
class HashRecipe:
    artifact_kind: ArtifactKind
    content_schema_version: int
    hash_schema_version: int
    validator: Callable[[Any], Any]


class SchemaRegistry:
    """Closed, historical registry: old recipes remain addressable after new ones register."""

    def __init__(self) -> None:
        self._recipes: dict[tuple[ArtifactKind, int, int], HashRecipe] = {}

    def register(self, recipe: HashRecipe) -> None:
        key = (recipe.artifact_kind, recipe.content_schema_version, recipe.hash_schema_version)
        if key in self._recipes: raise ValueError(f"recipe already registered: {key}")
        self._recipes[key] = recipe

    def validate(self, kind: ArtifactKind, content_version: int, hash_version: int, payload: Any) -> Any:
        recipe = self._recipes.get((kind, content_version, hash_version))
        if recipe is None: raise DomainError(ErrorCode.INVALID_TRANSITION, "unregistered content/hash recipe")
        return recipe.validator(payload)

    def hash(self, kind: ArtifactKind, content_version: int, hash_version: int, payload: Any) -> str:
        validated = self.validate(kind, content_version, hash_version, payload)
        if isinstance(validated, BaseModel): validated = validated.model_dump(mode="python", exclude_none=False)
        return artifact_hash(kind, content_version, hash_version, validated)


def default_registry() -> SchemaRegistry:
    from .models import ProjectionPlanPayload, SpecPackagePayload, StatusPolicyPayload
    registry = SchemaRegistry()
    registry.register(HashRecipe(ArtifactKind.SPEC_PACKAGE, 1, 1, SpecPackagePayload.model_validate))
    registry.register(HashRecipe(ArtifactKind.PROJECTION_PLAN, 1, 1, ProjectionPlanPayload.model_validate))
    registry.register(HashRecipe(ArtifactKind.STATUS_POLICY, 1, 1, StatusPolicyPayload.model_validate))
    registry.register(HashRecipe(ArtifactKind.STATUS_POLICY, 1, 2, StatusPolicyPayload.model_validate))
    return registry
