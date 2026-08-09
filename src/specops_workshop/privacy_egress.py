from __future__ import annotations

import json
from typing import Literal

from pydantic import Field

from .analyzer import SemanticAnalyzerRequest, SemanticTurnDraft
from .contracts import WorkshopModel


TERRA_EGRESS_POLICY_VERSION = "terra-alias-semantic-v2"
_FORBIDDEN_CONTRACT_FIELDS = frozenset(
    {
        "request_id",
        "session_id",
        "artifact_id",
        "case_id",
        "actor_id",
        "package_id",
        "item_id",
        "proposal_id",
        "proposal_key",
        "proposal_ref",
        "command_id",
        "content_hash",
        "source_version",
        "source_ref",
        "source_refs",
        "binding",
        "package_binding",
        "item_binding",
        "line",
        "line_number",
        "line_range",
        "location",
        "json_pointer",
        "workbook_location",
    }
)


def assert_identity_free_schema(schema: dict[str, object]) -> None:
    """Reject authoritative addressing fields anywhere in a provider schema."""
    properties = schema.get("properties")
    if isinstance(properties, dict):
        forbidden = _FORBIDDEN_CONTRACT_FIELDS.intersection(properties)
        if forbidden:
            raise ValueError(
                "provider semantic schema contains authoritative fields: "
                + ", ".join(sorted(forbidden))
            )
    for value in schema.values():
        if isinstance(value, dict):
            assert_identity_free_schema(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    assert_identity_free_schema(item)


class TerraEgressBlockSummary(WorkshopModel):
    label: str = Field(pattern=r"^[A-Z_]+$")
    byte_count: int = Field(ge=0)


class TerraEgressManifest(WorkshopModel):
    destination: Literal["OPENAI_TERRA"] = "OPENAI_TERRA"
    policy_version: Literal["terra-alias-semantic-v2"] = TERRA_EGRESS_POLICY_VERSION
    blocks: tuple[TerraEgressBlockSummary, ...]


class TerraPrivacyEgressGateway:
    """Identity-free framing for the sole authorized semantic destination."""

    labels = ("INSTRUCTIONS_AND_PHASE", "SEMANTIC_ANALYZER_REQUEST")

    def content_blocks(
        self, request: SemanticAnalyzerRequest
    ) -> tuple[dict[str, object], ...]:
        assert_identity_free_schema(
            SemanticAnalyzerRequest.model_json_schema(mode="validation")
        )
        assert_identity_free_schema(
            SemanticTurnDraft.model_json_schema(mode="validation")
        )
        instruction = (
            "Reason only over the identity-free PM business context, final PM turn, committed "
            "semantics, and selected technical evidence aliases in the request. Return exactly "
            "one strict SemanticTurnDraft. Every grounded statement or finding question must cite "
            "a unique selected alias and copy an exact supporting excerpt from that alias text. "
            "Never invent authoritative identities, source locations, evidence aliases, or "
            "downstream platform actions."
        )
        values = (
            instruction + f"\nPurpose: {request.purpose}\nPhase: {request.phase.value}",
            request.model_dump(mode="json"),
        )
        return tuple(
            {
                "type": "input_text",
                "text": label
                + "\n"
                + (
                    value
                    if isinstance(value, str)
                    else json.dumps(value, sort_keys=True, separators=(",", ":"))
                ),
            }
            for label, value in zip(self.labels, values, strict=True)
        )

    def manifest(
        self, blocks: tuple[dict[str, object], ...]
    ) -> TerraEgressManifest:
        if len(blocks) != len(self.labels):
            raise ValueError("Terra egress block count does not match the closed policy")
        summaries = []
        for expected, block in zip(self.labels, blocks, strict=True):
            if set(block) != {"type", "text"} or block["type"] != "input_text":
                raise ValueError("Terra egress blocks must be closed input_text values")
            text = block["text"]
            if not isinstance(text, str):
                raise ValueError("Terra egress block text must be a string")
            label, separator, _ = text.partition("\n")
            if not separator or label != expected:
                raise ValueError("Terra egress block order does not match the closed policy")
            summaries.append(
                TerraEgressBlockSummary(
                    label=label,
                    byte_count=len(text.encode("utf-8")),
                )
            )
        return TerraEgressManifest(blocks=tuple(summaries))
