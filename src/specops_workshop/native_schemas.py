from __future__ import annotations

from copy import deepcopy
from typing import Any, Literal

from pydantic import Field, TypeAdapter, ValidationError

from .analyzer import ProviderSemanticTurnDraft
from .contracts import WorkshopModel
from .privacy_egress import assert_identity_free_schema


_SAFE_PATH_SEGMENTS = frozenset({
    "outcome", "findings", "package_delta", "control_intent", "edit_instruction",
    "acknowledgement", "next_question", "uncertainty", "domain", "question",
    "item_title", "business_requirement", "technical_decision", "acceptance_check",
    "text", "evidence_aliases", "supporting_excerpts", "alias", "excerpt",
})
class ValidationDiagnostic(WorkshopModel):
    """A bounded, content-free explanation of local schema rejection."""

    path: tuple[str | int, ...] = Field(max_length=8)
    code: Literal["missing", "extra_forbidden", "invalid_type", "invalid_value"]


def safe_validation_diagnostics(exc: Exception) -> tuple[ValidationDiagnostic, ...]:
    """Retain only declared field paths and normalized Pydantic error classes."""

    if not isinstance(exc, ValidationError):
        return ()
    diagnostics: list[ValidationDiagnostic] = []
    for error in exc.errors(include_url=False):
        raw_path = error.get("loc", ())
        if not isinstance(raw_path, tuple):
            raw_path = ()
        safe_path: list[str | int] = []
        for segment in raw_path[:8]:
            if isinstance(segment, int) and 0 <= segment <= 500:
                safe_path.append(segment)
            elif isinstance(segment, str) and segment in _SAFE_PATH_SEGMENTS:
                safe_path.append(segment)
            else:
                # Unknown keys can originate from provider output and are not safe
                # to retain even as a field path.
                safe_path = []
                break
        error_type = str(error.get("type", ""))
        if error_type == "missing":
            code = "missing"
        elif error_type == "extra_forbidden":
            code = "extra_forbidden"
        elif error_type.endswith("_type"):
            code = "invalid_type"
        else:
            code = "invalid_value"
        diagnostic = ValidationDiagnostic(path=tuple(safe_path), code=code)
        if diagnostic not in diagnostics:
            diagnostics.append(diagnostic)
        if len(diagnostics) == 8:
            break
    return tuple(diagnostics)


def _strict_objects(value: Any) -> Any:
    if isinstance(value, dict):
        result = {
            ("anyOf" if key == "oneOf" else key): _strict_objects(item)
            for key, item in value.items()
            if key != "discriminator"
        }
        properties = result.get("properties")
        if isinstance(properties, dict):
            result["required"] = list(properties)
            result["additionalProperties"] = False
        return result
    if isinstance(value, list):
        return [_strict_objects(item) for item in value]
    return value


def local_semantic_turn_schema() -> dict[str, object]:
    """The identity-free schema from which every provider-native form derives."""

    schema = deepcopy(TypeAdapter(ProviderSemanticTurnDraft).json_schema(mode="validation"))
    assert_identity_free_schema(schema)
    return schema


def openai_semantic_turn_schema() -> dict[str, object]:
    schema = _strict_objects(local_semantic_turn_schema())
    assert_identity_free_schema(schema)
    return schema


def claude_semantic_turn_tool() -> dict[str, object]:
    input_schema = local_semantic_turn_schema()
    tool = {
        "name": "semantic_turn_draft_v1",
        "description": "Return exactly one alias-sealed Workshop semantic outcome.",
        "input_schema": input_schema,
    }
    assert_identity_free_schema(input_schema)
    return tool
