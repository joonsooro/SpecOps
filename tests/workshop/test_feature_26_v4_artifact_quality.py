from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from specops_contracts import artifact_quality_v1 as q
from specops_workshop.v4.artifact_quality_adapter import (
    ArtifactQualityEvaluatorError,
    FreshConversationTerraQualityEvaluator,
    PreparedArtifactQualityContext,
)
from specops_workshop.v4.schema_compiler import artifact_quality_native_schema
from v4_quality_factory import all_pass_candidate


NOW = datetime(2026, 8, 13, 8, 0, tzinfo=timezone.utc)
HASH = "sha256:" + "a" * 64


def _bundle() -> q.ArtifactQualityAuditBundle:
    rules = tuple(
        q.QualityRuleManifestEntry(
            rule_id=f"SPEC-Q-{index:03d}",
            name=f"Rule {index}",
            dimension="semantic quality",
            check_types=(
                (q.QualityCheckType.SEMANTIC, q.QualityCheckType.REFERENTIAL)
                if index <= 5
                else (q.QualityCheckType.SEMANTIC,)
                if index <= 21
                else (q.QualityCheckType.STRUCTURAL,)
            ),
            gate=q.QualityGate.BLOCK_CONFIRMATION,
            primary_evaluator=(
                q.PrimaryEvaluator.ANALYZER
                if index <= 21
                else q.PrimaryEvaluator.FOUNDATION
            ),
            requirement="The rule must be satisfied.",
            pass_condition="The supplied universe demonstrates the rule.",
            evidence_of_pass="Artifact pointers or exact Foundation checks.",
            failure_effect=q.FailureEffect.BLOCKED,
        )
        for index in range(1, 27)
    )
    sources = (
        q.AuditSourceDocument(
            source_id=UUID("10000000-0000-4000-8000-000000000001"),
            role=q.SourceRole.PM_SPEC,
            version=1,
            payload_hash=HASH,
            canonical_locator="/source/pm.md",
            filename="pm.md",
            media_type="text/markdown",
            complete_text="Complete PM source text.",
        ),
        q.AuditSourceDocument(
            source_id=UUID("10000000-0000-4000-8000-000000000002"),
            role=q.SourceRole.TECHNICAL_CONTRACT,
            version=1,
            payload_hash=HASH,
            canonical_locator="/source/technical.md",
            filename="technical.md",
            media_type="text/markdown",
            complete_text="Complete technical source text.",
        ),
    )
    transcripts = tuple(
        q.AuditTranscript(
            event_id=UUID(f"20000000-0000-4000-8000-{index:012d}"),
            sequence_number=index,
            transcript_artifact_id=UUID(
                f"30000000-0000-4000-8000-{index:012d}"
            ),
            transcript_version=1,
            transcript_hash=HASH,
            actor=q.TranscriptActor.PM,
            speaker_actor_id=UUID("40000000-0000-4000-8000-000000000001"),
            complete_text=f"Finalized transcript {index}.",
        )
        for index in (1, 2)
    )
    return q.ArtifactQualityAuditBundle(
        protocol_version=q.PROTOCOL_VERSION,
        audit_id=UUID("50000000-0000-4000-8000-000000000001"),
        evaluator_run_id=UUID("50000000-0000-4000-8000-000000000002"),
        case_id=UUID("50000000-0000-4000-8000-000000000003"),
        session_id=UUID("50000000-0000-4000-8000-000000000004"),
        based_on_case_revision=12,
        subject=q.ArtifactAuditSubject(
            artifact_type=q.ArtifactType.SPEC_PACKAGE,
            artifact_id=UUID("50000000-0000-4000-8000-000000000005"),
            artifact_key="SPEC-QUALITY",
            artifact_version=1,
            record_revision=1,
            payload_hash=HASH,
            canonical_payload_json="{}",
        ),
        quality_contract=q.QualityContractBinding(
            contract_id="SEMANTIC-QUALITY-CONTRACT",
            version="2.1.0",
            content_hash=HASH,
        ),
        source_set_hash=HASH,
        sources=sources,
        transcript_count=2,
        first_transcript_sequence=1,
        last_transcript_sequence=2,
        transcript_manifest_hash=HASH,
        transcripts=transcripts,
        semantic_state_hash=HASH,
        canonical_semantic_snapshot_json="{}",
        confirmed_spec=None,
        rule_manifest=rules,
        semantic_rule_ids=tuple(f"SPEC-Q-{index:03d}" for index in range(1, 22)),
        audit_scope_manifest_hash=HASH,
        request_hash=HASH,
    )


class _Endpoint:
    def __init__(self, result):
        self.result = result
        self.calls: list[dict] = []

    async def create(self, **values):
        self.calls.append(values)
        return self.result


class _Conversations(_Endpoint):
    def __init__(self, result):
        super().__init__(result)
        self.deleted: list[tuple[str, dict]] = []

    async def delete(self, conversation_id, **values):
        self.deleted.append((conversation_id, values))


class _Clock:
    def __init__(self):
        self.elapsed = 0.0

    def monotonic(self):
        return self.elapsed

    async def sleep(self, _seconds):
        self.elapsed += 12.0


class _BackgroundResponses:
    def __init__(self, bundle, *, complete_after=6):
        self.bundle = bundle
        self.complete_after = complete_after
        self.create_calls: list[dict] = []
        self.retrieve_calls: list[tuple[str, dict]] = []

    async def create(self, **values):
        self.create_calls.append(values)
        return SimpleNamespace(
            id="resp_quality_background",
            _request_id="req_quality_create",
            status="queued",
            usage=None,
        )

    async def retrieve(self, response_id, **values):
        self.retrieve_calls.append((response_id, values))
        completed = len(self.retrieve_calls) >= self.complete_after
        return SimpleNamespace(
            id=response_id,
            _request_id=f"req_quality_retrieve_{len(self.retrieve_calls)}",
            status="completed" if completed else "in_progress",
            usage=None,
            output_text=(
                all_pass_candidate(self.bundle).model_dump_json()
                if completed
                else ""
            ),
        )


def _client(bundle, *, conversation_id="conv_quality_isolated", output_text=None):
    conversations = _Conversations(SimpleNamespace(id=conversation_id))
    responses = _Endpoint(
        SimpleNamespace(
            id="resp_quality_1",
            _request_id="req_quality_1",
            output_text=(
                all_pass_candidate(bundle).model_dump_json()
                if output_text is None
                else output_text
            ),
        )
    )
    return SimpleNamespace(conversations=conversations, responses=responses)


def test_fresh_terra_audit_serializes_the_closed_universe_without_network():
    bundle = _bundle()
    client = _client(bundle)
    evaluator = FreshConversationTerraQualityEvaluator(
        api_key="not-called", client=client, now=lambda: NOW
    )

    prepared = asyncio.run(
        evaluator.prepare(bundle, prohibited_conversation_id="conv_workshop_active")
    )
    result = asyncio.run(evaluator.evaluate(bundle, prepared=prepared))
    asyncio.run(evaluator.release(prepared))

    assert prepared.provider_conversation_id == "conv_quality_isolated"
    assert prepared.provider_conversation_id != "conv_workshop_active"
    request = client.responses.calls[0]
    assert request["model"] == "gpt-5.6-terra"
    assert request["reasoning"] == {"effort": "medium", "context": "current_turn"}
    assert request["store"] is True
    assert request["conversation"] == "conv_quality_isolated"
    assert request["text"]["format"]["schema"] == artifact_quality_native_schema()
    serialized = request["input"][0]["content"][0]["text"]
    assert serialized == bundle.model_dump_json(exclude_none=False)
    assert "Complete PM source text." in serialized
    assert "Complete technical source text." in serialized
    assert "Finalized transcript 1." in serialized
    assert "Finalized transcript 2." in serialized
    assert result.execution.store_enabled is True
    assert result.candidate.request_hash == bundle.request_hash
    assert client.conversations.deleted[0][0] == "conv_quality_isolated"


def test_quality_response_crosses_slow_observation_without_recreate_or_hard_stop():
    bundle = _bundle()
    clock = _Clock()
    conversations = _Conversations(SimpleNamespace(id="conv_quality_isolated"))
    responses = _BackgroundResponses(bundle)
    evaluator = FreshConversationTerraQualityEvaluator(
        api_key="not-called",
        client=SimpleNamespace(conversations=conversations, responses=responses),
        now=lambda: NOW,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    prepared = asyncio.run(
        evaluator.prepare(bundle, prohibited_conversation_id="conv_workshop_active")
    )
    checkpoints: list[tuple[str, str]] = []

    result = asyncio.run(
        evaluator.evaluate(
            bundle,
            prepared=prepared,
            response_checkpoint=lambda response_id, client_request_id: checkpoints.append(
                (response_id, client_request_id)
            ),
        )
    )

    assert len(responses.create_calls) == 1
    assert responses.create_calls[0]["background"] is True
    assert len(responses.retrieve_calls) == 6
    assert checkpoints == [
        (
            "resp_quality_background",
            f"aqa-response-{bundle.request_hash[7:39]}",
        )
    ]
    assert [event.event for event in evaluator.lifecycle_events] == [
        "provider_request.started",
        "provider_request.accepted",
        "provider_request.timeout",
        "provider_request.completed",
    ]
    assert evaluator.lifecycle_events[2].duration_ms == 60_000
    assert result.execution.provider_response_id == "resp_quality_background"


def test_quality_response_resume_polls_checkpointed_identity_without_create():
    bundle = _bundle()
    responses = _BackgroundResponses(bundle, complete_after=1)
    evaluator = FreshConversationTerraQualityEvaluator(
        api_key="not-called",
        client=SimpleNamespace(
            conversations=_Conversations(SimpleNamespace(id="unused")),
            responses=responses,
        ),
    )
    prepared = PreparedArtifactQualityContext(
        provider="OPENAI",
        model="gpt-5.6-terra",
        reasoning_effort="medium",
        provider_conversation_id="conv_quality_isolated",
        client_request_id="aqa-conversation-known",
        started_at=NOW,
        provider_response_id="resp_quality_background",
    )

    result = asyncio.run(evaluator.evaluate(bundle, prepared=prepared))

    assert responses.create_calls == []
    assert len(responses.retrieve_calls) == 1
    assert [event.event for event in evaluator.lifecycle_events] == [
        "provider_request.started",
        "provider_request.resumed",
        "provider_request.completed",
    ]
    assert result.execution.provider_response_id == prepared.provider_response_id


def test_audit_rejects_workshop_conversation_reuse_with_bounded_diagnostics():
    bundle = _bundle()
    client = _client(bundle, conversation_id="conv_workshop_active")
    evaluator = FreshConversationTerraQualityEvaluator(
        api_key="not-called", client=client, now=lambda: NOW
    )
    with pytest.raises(ArtifactQualityEvaluatorError) as caught:
        asyncio.run(
            evaluator.prepare(bundle, prohibited_conversation_id="conv_workshop_active")
        )
    receipt = caught.value.receipt
    assert receipt.stage == "CONVERSATION_CREATE"
    assert receipt.code is q.QualityProviderFailureCode.UNKNOWN_SAFE
    assert "Workshop" not in receipt.model_dump_json()
    assert "source" not in receipt.model_dump_json().lower()


def test_malformed_attestation_fails_closed_without_echoing_provider_content():
    bundle = _bundle()
    secret = "sk-this-content-must-never-enter-diagnostics"
    client = _client(bundle, output_text=json.dumps({"unexpected": secret}))
    evaluator = FreshConversationTerraQualityEvaluator(
        api_key="not-called", client=client, now=lambda: NOW
    )
    prepared = asyncio.run(
        evaluator.prepare(bundle, prohibited_conversation_id="conv_workshop_active")
    )
    with pytest.raises(ArtifactQualityEvaluatorError) as caught:
        asyncio.run(evaluator.evaluate(bundle, prepared=prepared))
    receipt = caught.value.receipt
    assert receipt.code is q.QualityProviderFailureCode.OUTPUT_INVALID
    assert receipt.stage == "LOCAL_VALIDATION"
    assert secret not in receipt.model_dump_json()
    assert receipt.validation_diagnostics
