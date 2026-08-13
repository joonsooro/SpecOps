"""Compile normative Pydantic candidates to OpenAI strict JSON Schemas.

OpenAI Structured Outputs accepts a deliberately smaller JSON Schema dialect.
The compiler is intentionally fail-closed: it removes presentation-only
metadata, translates Pydantic discriminated ``oneOf`` nodes to supported
``anyOf`` nodes, closes every object, requires every property, and rejects any
remaining unsupported keyword or provider-limit overflow.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, TypeVar

from pydantic import BaseModel
from specops_contracts import artifact_quality_v1 as quality

from . import contracts


ModelT = TypeVar("ModelT", bound=BaseModel)


@dataclass(frozen=True)
class NativeSchemaSpec:
    operation: contracts.AnalyzerOperation
    name: str
    candidate_type: type[BaseModel]


NATIVE_SCHEMA_SPECS: tuple[NativeSchemaSpec, ...] = (
    NativeSchemaSpec(
        contracts.AnalyzerOperation.BOOTSTRAP,
        "interview_brief_candidate_v1",
        contracts.InterviewBriefCandidate,
    ),
    NativeSchemaSpec(
        contracts.AnalyzerOperation.TURN_ANALYSIS,
        "turn_analysis_candidate_v1",
        contracts.TurnAnalysisCandidate,
    ),
    NativeSchemaSpec(
        contracts.AnalyzerOperation.GUIDANCE,
        "guidance_candidate_v1",
        contracts.GuidanceCandidate,
    ),
    NativeSchemaSpec(
        contracts.AnalyzerOperation.REVIEW_NARRATION,
        "review_narration_candidate_v1",
        contracts.ReviewNarrationCandidate,
    ),
    NativeSchemaSpec(
        contracts.AnalyzerOperation.SPEC_PACKAGE_SYNTHESIS,
        "spec_package_synthesis_candidate_v1",
        contracts.SpecPackageSynthesisCandidate,
    ),
    NativeSchemaSpec(
        contracts.AnalyzerOperation.TECHNICAL_CONTRACT_SYNTHESIS,
        "technical_contract_synthesis_candidate_v1",
        contracts.TechnicalContractSynthesisCandidate,
    ),
)


_PRESENTATION_KEYS = frozenset({"title", "description", "examples", "default", "$schema"})
_UNSUPPORTED_KEYS = frozenset(
    {
        "allOf",
        "not",
        "dependentRequired",
        "dependentSchemas",
        "if",
        "then",
        "else",
        "patternProperties",
        "unevaluatedProperties",
        "propertyNames",
        "contains",
        "minContains",
        "maxContains",
    }
)


def _compile_node(value: Any) -> Any:
    if isinstance(value, list):
        return [_compile_node(item) for item in value]
    if not isinstance(value, dict):
        return value

    compiled: dict[str, Any] = {}
    for key, item in value.items():
        if key in _PRESENTATION_KEYS or key == "discriminator":
            continue
        translated = "anyOf" if key == "oneOf" else key
        compiled[translated] = _compile_node(item)

    properties = compiled.get("properties")
    if isinstance(properties, dict):
        compiled["required"] = list(properties)
        compiled["additionalProperties"] = False
    return compiled


def _walk(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _resolved_depth(schema: dict[str, Any]) -> int:
    definitions = schema.get("$defs", {})

    def depth(value: Any, level: int, resolving: frozenset[str]) -> int:
        if isinstance(value, list):
            return max((depth(item, level, resolving) for item in value), default=level)
        if not isinstance(value, dict):
            return level
        reference = value.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/$defs/"):
            name = reference.removeprefix("#/$defs/")
            if name in resolving:
                return level
            target = definitions.get(name)
            if target is None:
                raise ValueError(f"unresolved native-schema reference: {reference}")
            return depth(target, level, resolving | {name})
        next_level = level + (1 if value.get("type") in {"object", "array"} else 0)
        return max(
            (depth(child, next_level, resolving) for child in value.values()),
            default=next_level,
        )

    return depth(schema, 0, frozenset())


def validate_openai_strict_schema(schema: dict[str, Any]) -> None:
    """Validate the documented Structured Outputs subset and provider limits."""

    if schema.get("type") != "object" or "anyOf" in schema:
        raise ValueError("OpenAI native schema root must be one object")

    property_count = 0
    enum_count = 0
    string_budget = 0
    for node in _walk(schema):
        forbidden = _UNSUPPORTED_KEYS.intersection(node)
        if forbidden:
            raise ValueError(f"unsupported OpenAI schema keyword(s): {sorted(forbidden)}")
        properties = node.get("properties")
        if isinstance(properties, dict):
            if node.get("additionalProperties") is not False:
                raise ValueError("every OpenAI schema object must be closed")
            if node.get("required") != list(properties):
                raise ValueError("every OpenAI schema object field must be required")
            property_count += len(properties)
            string_budget += sum(len(name) for name in properties)
        definitions = node.get("$defs")
        if isinstance(definitions, dict):
            string_budget += sum(len(name) for name in definitions)
        enum = node.get("enum")
        if isinstance(enum, list):
            enum_count += len(enum)
            string_budget += sum(len(item) for item in enum if isinstance(item, str))
            if len(enum) > 250 and sum(len(item) for item in enum if isinstance(item, str)) > 15_000:
                raise ValueError("one enum exceeds the OpenAI string budget")
        const = node.get("const")
        if isinstance(const, str):
            string_budget += len(const)
    if property_count > 5_000:
        raise ValueError("OpenAI schema exceeds 5,000 object properties")
    if enum_count > 1_000:
        raise ValueError("OpenAI schema exceeds 1,000 enum values")
    if string_budget > 120_000:
        raise ValueError("OpenAI schema exceeds the 120,000-character budget")
    if _resolved_depth(schema) > 10:
        raise ValueError("OpenAI schema exceeds ten resolved nesting levels")


def compile_openai_strict_schema(model: type[ModelT]) -> dict[str, Any]:
    local_schema = model.model_json_schema(mode="validation")
    compiled = _compile_node(deepcopy(local_schema))
    validate_openai_strict_schema(compiled)
    return compiled


def native_schema_for(operation: contracts.AnalyzerOperation) -> tuple[str, dict[str, Any], type[BaseModel]]:
    try:
        spec = next(item for item in NATIVE_SCHEMA_SPECS if item.operation is operation)
    except StopIteration as exc:  # pragma: no cover - closed enum defense
        raise ValueError(f"unsupported Analyzer operation: {operation}") from exc
    return spec.name, compile_openai_strict_schema(spec.candidate_type), spec.candidate_type


def all_native_schemas() -> dict[str, dict[str, Any]]:
    return {
        spec.operation.value: compile_openai_strict_schema(spec.candidate_type)
        for spec in NATIVE_SCHEMA_SPECS
    }


def artifact_quality_native_schema() -> dict[str, Any]:
    """Compile the separate provider-neutral quality attestation output.

    This schema is intentionally not included in ``all_native_schemas``: that
    mapping is the closed six-operation Workshop Protocol surface.
    """

    return compile_openai_strict_schema(quality.ArtifactSemanticAttestationCandidate)
