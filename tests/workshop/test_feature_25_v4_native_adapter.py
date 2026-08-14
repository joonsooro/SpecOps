from __future__ import annotations

import asyncio
import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest
import httpx
from jsonschema import Draft202012Validator
from openai import AsyncOpenAI

from specops_workshop.v4 import contracts as c
from specops_workshop.v4.canonical import analyzer_request_hash, payload_hash
from specops_workshop.v4.openai_adapter import (
    ProviderAdapterError,
    ProviderSourceUpload,
    StoredConversationOpenAIAdapter,
)
from specops_workshop.v4.schema_compiler import (
    NATIVE_SCHEMA_SPECS,
    compile_openai_strict_schema,
    validate_openai_strict_schema,
)


NOW = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
ZERO_HASH = "sha256:" + "0" * 64
ONE_HASH = "sha256:" + "1" * 64
CASE_ID = UUID("00000000-0000-4000-8000-000000000001")
SESSION_ID = UUID("00000000-0000-4000-8000-000000000002")
CONTEXT_ID = UUID("00000000-0000-4000-8000-000000000003")
RUN_ID = UUID("00000000-0000-4000-8000-000000000004")


def _contract() -> c.AnalyzerContractBinding:
    return c.AnalyzerContractBinding(
        protocol_version="1.0.0",
        instruction_set_id="specops-workshop-analyzer",
        instruction_set_version=1,
        instruction_set_hash=ZERO_HASH,
        semantic_quality_contract_id="SEMANTIC-QUALITY-CONTRACT",
        semantic_quality_contract_version="2.1.0",
        semantic_quality_contract_hash=ONE_HASH,
        provider_schema_version="1.0.0",
        model="gpt-5.6-terra",
        reasoning_effort="medium",
    )


def _source(role: c.SourceRole, source_id: int, filename: str, content: bytes):
    return ProviderSourceUpload(
        source=c.SourceIdentity(
            source_id=UUID(f"00000000-0000-4000-8000-{source_id:012d}"),
            role=role,
            version=1,
            payload_hash="sha256:" + hashlib.sha256(content).hexdigest(),
            canonical_locator=f"/sources/{filename}",
            filename=filename,
            media_type="text/markdown",
        ),
        content=content,
    )


def _snapshot(source_set_hash: str, revision: int = 0) -> c.FoundationSemanticSnapshot:
    return c.FoundationSemanticSnapshot(
        case_revision=revision,
        source_set_hash=source_set_hash,
        readiness=c.Readiness.FORMULATING,
        review_obligation=c.ReviewObligation.NONE,
        evidence=(),
        problems=(),
        questions=(),
        facts=(),
        decisions=(),
        evidence_findings=(),
        revision_requests=(),
    )


def _request_hash(data: dict) -> str:
    material = dict(data)
    material["request_hash"] = ZERO_HASH
    return analyzer_request_hash(material)


def _bootstrap_request(prepared) -> c.BootstrapAnalyzerRequest:
    data = {
        "protocol_version": "1.0.0",
        "request_type": c.AnalyzerOperation.BOOTSTRAP,
        "client_request_id": "client-bootstrap",
        "analyzer_run_id": RUN_ID,
        "context_id": CONTEXT_ID,
        "provider_conversation_id": prepared.provider_conversation_id,
        "source_set_hash": prepared.source_set.source_set_hash,
        "analyzer_contract": _contract(),
        "based_on_case_revision": 0,
        "source_set": prepared.source_set,
        "requested_output": "INTERVIEW_BRIEF_CANDIDATE",
    }
    data["request_hash"] = _request_hash(data)
    return c.BootstrapAnalyzerRequest.model_validate(data)


def _brief(request: c.BootstrapAnalyzerRequest) -> c.InterviewBriefCandidate:
    return c.InterviewBriefCandidate(
        protocol_version="1.0.0",
        output_type="INTERVIEW_BRIEF_CANDIDATE",
        analyzer_run_id=request.analyzer_run_id,
        context_id=request.context_id,
        request_hash=request.request_hash,
        source_set_hash=request.source_set_hash,
        based_on_case_revision=request.based_on_case_revision,
        customer_promise_summary="Export filtered orders safely.",
        evidence_candidates=(
            c.EvidenceCandidate(
                candidate_key="evidence-export",
                source_role=c.SourceRole.PM_SPEC,
                locator=c.SourceLineLocator(
                    locator_kind=c.SourceLocatorKind.SOURCE_LINES,
                    start_line=1,
                    end_line=1,
                ),
                relevance_claim="The source defines the export promise.",
                quoted_text_candidate="Export filtered orders.",
            ),
        ),
        problems=(
            c.ProblemCandidate(
                candidate_key="problem-format",
                problem_kind=c.ProblemKind.MISSING_DECISION,
                domain=c.Domain.TECHNICAL,
                severity=c.Severity.HIGH,
                statement="The export format is not fixed.",
                consequence="Implementations could diverge.",
                evidence_candidate_keys=("evidence-export",),
            ),
        ),
        problem_clusters=(),
        questions=tuple(
            c.QuestionCandidate(
                candidate_key=(
                    "question-format" if index == 1 else f"question-format-{index}"
                ),
                text=f"Which CSV encoding should export area {index} use?",
                rationale="The implementation needs one encoding.",
                question_shape=c.QuestionShape.OPEN_TEXT,
                capture_policy=c.CapturePolicy.CLARIFICATION_ONLY,
                answer_options=(),
                addresses_problem_keys=("problem-format",),
                prerequisite_problem_keys=(),
                safe_without_current_turn_interpretation=True,
            )
            for index in range(1, c.INITIAL_RUNWAY_DEPTH + 1)
        ),
        initial_runway=c.QuestionRunwayCandidate(
            recommended_question_key="question-format",
            safe_alternate_question_keys=tuple(
                f"question-format-{index}"
                for index in range(2, c.INITIAL_RUNWAY_DEPTH + 1)
            ),
            do_not_ask_question_keys=(),
        ),
        confirmation_checkpoints=(),
    )


class _FakeFiles:
    def __init__(self, owner):
        self.owner = owner

    async def create(self, **kwargs):
        self.owner.file_calls.append(kwargs)
        if self.owner.fail_file_call == len(self.owner.file_calls):
            raise ConnectionError("fixture content must never escape diagnostics")
        return SimpleNamespace(id=f"file_{len(self.owner.file_calls)}")

    async def delete(self, file_id, **kwargs):
        self.owner.deleted_files.append((file_id, kwargs))
        return SimpleNamespace(id=file_id, deleted=True)


class _FakeConversations:
    def __init__(self, owner):
        self.owner = owner

    async def create(self, **kwargs):
        self.owner.conversation_calls.append(kwargs)
        if self.owner.fail_conversation:
            raise ConnectionError("fixture content must never escape diagnostics")
        return SimpleNamespace(id="conv_workshop")

    async def retrieve(self, conversation_id, **kwargs):
        self.owner.retrieve_calls.append((conversation_id, kwargs))
        return SimpleNamespace(id=conversation_id)

    async def delete(self, conversation_id, **kwargs):
        self.owner.deleted_conversations.append((conversation_id, kwargs))
        return SimpleNamespace(id=conversation_id, deleted=True)


class _FakeResponses:
    def __init__(self, owner):
        self.owner = owner

    async def create(self, **kwargs):
        self.owner.response_calls.append(kwargs)
        response = SimpleNamespace(
            id=f"resp_{len(self.owner.response_calls)}",
            output_text=self.owner.outputs.pop(0),
            _request_id=f"req_{len(self.owner.response_calls)}",
        )
        self.owner.stored_responses[response.id] = response
        return response

    async def retrieve(self, response_id, **kwargs):
        self.owner.response_retrieve_calls.append((response_id, kwargs))
        return self.owner.stored_responses[response_id]

    async def delete(self, response_id, **kwargs):
        self.owner.deleted_responses.append((response_id, kwargs))
        # The installed AsyncOpenAI SDK intentionally casts successful
        # Response DELETE bodies to None.
        return None


class FakeOpenAI:
    def __init__(self):
        self.file_calls = []
        self.conversation_calls = []
        self.retrieve_calls = []
        self.response_calls = []
        self.response_retrieve_calls = []
        self.stored_responses = {}
        self.outputs = []
        self.deleted_files = []
        self.deleted_conversations = []
        self.deleted_responses = []
        self.fail_file_call = None
        self.fail_conversation = False
        self.files = _FakeFiles(self)
        self.conversations = _FakeConversations(self)
        self.responses = _FakeResponses(self)


class _ManualMonotonic:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    async def advance(self, _seconds: float) -> None:
        self.value += 10.0


class _BackgroundResponses:
    def __init__(self, *, cancel_wait_once: bool = False) -> None:
        self.create_calls: list[dict] = []
        self.retrieve_ids: list[str] = []
        self.cancel_wait_once = cancel_wait_once
        self.output_text = ""

    async def create(self, **kwargs):
        self.create_calls.append(kwargs)
        return SimpleNamespace(
            id="resp_background_bootstrap",
            status="queued",
            output_text="",
            _request_id="req_background_create",
        )

    async def retrieve(self, response_id):
        self.retrieve_ids.append(response_id)
        completed = len(self.retrieve_ids) >= 7 or self.cancel_wait_once
        return SimpleNamespace(
            id=response_id,
            status="completed" if completed else "in_progress",
            output_text=self.output_text if completed else "",
            _request_id=f"req_background_retrieve_{len(self.retrieve_ids)}",
            usage=(
                SimpleNamespace(
                    input_tokens=100,
                    input_tokens_details=SimpleNamespace(cached_tokens=20),
                    output_tokens=40,
                    output_tokens_details=SimpleNamespace(reasoning_tokens=10),
                    total_tokens=140,
                )
                if completed
                else None
            ),
        )


def test_all_six_native_schemas_are_openai_strict_root_objects():
    assert len(NATIVE_SCHEMA_SPECS) == 6
    for spec in NATIVE_SCHEMA_SPECS:
        schema = compile_openai_strict_schema(spec.candidate_type)
        validate_openai_strict_schema(schema)
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert "anyOf" not in schema
        encoded = json.dumps(schema)
        assert '"oneOf"' not in encoded
        assert '"discriminator"' not in encoded
        assert '"allOf"' not in encoded


def test_native_schema_compiler_preserves_domain_property_names_at_every_object():
    for spec in NATIVE_SCHEMA_SPECS:
        local = spec.candidate_type.model_json_schema(mode="validation")
        compiled = compile_openai_strict_schema(spec.candidate_type)
        assert set(compiled["properties"]) == set(local["properties"])
        for name, local_definition in local.get("$defs", {}).items():
            local_properties = local_definition.get("properties")
            if not isinstance(local_properties, dict):
                continue
            compiled_definition = compiled["$defs"][name]
            property_maps = (
                [compiled_definition["properties"]]
                if "properties" in compiled_definition
                else [branch["properties"] for branch in compiled_definition["anyOf"]]
            )
            assert property_maps
            assert all(set(properties) == set(local_properties) for properties in property_maps)

    bootstrap = compile_openai_strict_schema(c.InterviewBriefCandidate)
    cluster = bootstrap["$defs"]["ProblemClusterCandidate"]
    assert "title" in cluster["properties"]
    assert "title" in cluster["required"]


def test_bootstrap_provider_schema_enforces_local_question_shape_invariants():
    adapter = StoredConversationOpenAIAdapter(api_key="unused", client=SimpleNamespace())
    sources = (
        _source(c.SourceRole.PM_SPEC, 10, "pm-spec.md", b"PM source"),
        _source(c.SourceRole.TECHNICAL_CONTRACT, 11, "technical.md", b"Technical"),
    )
    prepared = adapter.prepared_from_ids(
        sources, ("file_pm", "file_technical"), "conv_schema_parity"
    )
    payload = _brief(_bootstrap_request(prepared)).model_dump(mode="json")
    schema = compile_openai_strict_schema(c.InterviewBriefCandidate)
    validator = Draft202012Validator(schema)
    validator.validate(payload)

    invalid_options = deepcopy(payload)
    invalid_options["questions"][0]["question_shape"] = "CLOSED_TEXT"
    invalid_options["questions"][0]["answer_options"] = ["one", "two"]
    assert list(validator.iter_errors(invalid_options))

    invalid_open_fact = deepcopy(payload)
    invalid_open_fact["questions"][0]["capture_policy"] = "LOW_RISK_FACT"
    assert list(validator.iter_errors(invalid_open_fact))

    valid_enum = deepcopy(payload)
    valid_enum["questions"][0]["question_shape"] = "CLOSED_ENUM"
    valid_enum["questions"][0]["answer_options"] = ["one", "two"]
    validator.validate(valid_enum)

    valid_boolean = deepcopy(payload)
    valid_boolean["questions"][0]["question_shape"] = "CLOSED_BOOLEAN"
    validator.validate(valid_boolean)


def test_quote_search_grounding_requires_one_exact_terra_quote():
    exact_quote = "Export filtered orders."
    locator = c.QuoteSearchLocator(
        locator_kind=c.SourceLocatorKind.QUOTE_SEARCH,
        exact_quote=exact_quote,
        occurrence=1,
    )
    valid = c.EvidenceCandidate(
        candidate_key="evidence-export",
        source_role=c.SourceRole.PM_SPEC,
        locator=locator,
        relevance_claim="The source defines the export promise.",
        quoted_text_candidate=exact_quote,
    )
    assert valid.quoted_text_candidate == valid.locator.exact_quote

    with pytest.raises(ValueError, match="must exactly equal"):
        c.EvidenceCandidate(
            candidate_key="evidence-export",
            source_role=c.SourceRole.PM_SPEC,
            locator=locator,
            relevance_claim="The source defines the export promise.",
            quoted_text_candidate="A paraphrase is not an exact quote.",
        )
    with pytest.raises(ValueError, match="must exactly equal"):
        c.EvidenceCandidate(
            candidate_key="evidence-export",
            source_role=c.SourceRole.PM_SPEC,
            locator=locator,
            relevance_claim="The source defines the export promise.",
            quoted_text_candidate=None,
        )


def test_provider_instructions_bind_quote_search_fields_without_claiming_authority():
    instructions = StoredConversationOpenAIAdapter._instructions(
        c.AnalyzerOperation.BOOTSTRAP
    )
    assert "locator.exact_quote and quoted_text_candidate" in instructions
    assert "character-for-character identical" in instructions
    assert "occurs verbatim at the requested occurrence" in instructions
    assert "omit that evidence candidate" in instructions
    assert "never claim authority" in instructions

    guidance = StoredConversationOpenAIAdapter._instructions(
        c.AnalyzerOperation.GUIDANCE
    )
    assert "safe_without_current_turn_interpretation field is true" in guidance
    assert "neither already ASKED nor listed in do_not_ask_question_refs" in guidance
    assert "reject that selected branch rather than reinterpret or repeat it" in guidance


def test_stored_conversation_bootstrap_uploads_exactly_two_files_and_reuses_conversation():
    asyncio.run(_stored_conversation_bootstrap_case())


def test_prepare_failure_cleans_partial_upload_without_content_diagnostics():
    async def scenario():
        fake = FakeOpenAI()
        fake.fail_file_call = 2
        adapter = StoredConversationOpenAIAdapter(api_key="unused", client=fake, now=lambda: NOW)
        sources = (
            _source(c.SourceRole.PM_SPEC, 10, "pm-spec.md", b"private PM source"),
            _source(c.SourceRole.TECHNICAL_CONTRACT, 11, "technical-contract.md", b"private technical source"),
        )
        with pytest.raises(ProviderAdapterError) as captured:
            await adapter.prepare_context(sources)
        assert [value[0] for value in fake.deleted_files] == ["file_1"]
        assert "private" not in captured.value.receipt.model_dump_json()

    asyncio.run(scenario())


def test_release_context_deletes_response_conversation_and_both_uploaded_files():
    async def scenario():
        fake = FakeOpenAI()
        adapter = StoredConversationOpenAIAdapter(api_key="unused", client=fake, now=lambda: NOW)
        sources = (
            _source(c.SourceRole.PM_SPEC, 10, "pm-spec.md", b"PM source"),
            _source(c.SourceRole.TECHNICAL_CONTRACT, 11, "technical-contract.md", b"Technical source"),
        )
        prepared = await adapter.prepare_context(sources)
        request = _bootstrap_request(prepared)
        fake.outputs.append(_brief(request).model_dump_json())
        context = (await adapter.bootstrap(request, prepared=prepared, session_id=SESSION_ID)).context
        receipt = await adapter.release_context(context)
        assert receipt.completed is True
        assert {item.outcome for item in receipt.deletions} == {"DELETED"}
        assert [value[0] for value in fake.deleted_responses] == ["resp_1"]
        assert [value[0] for value in fake.deleted_conversations] == ["conv_workshop"]
        assert [value[0] for value in fake.deleted_files] == ["file_1", "file_2"]

    asyncio.run(scenario())


def test_release_keeps_uncertain_resource_identity_and_accepts_explicit_absence():
    class DeleteFailure(RuntimeError):
        def __init__(self, status_code: int | None = None):
            super().__init__("raw provider cleanup body must not enter the receipt")
            self.status_code = status_code
            self.request_id = "req_delete_safe"

    class Conversations:
        async def delete(self, conversation_id, **kwargs):
            raise DeleteFailure(404)

    class Files:
        async def delete(self, file_id, **kwargs):
            if file_id == "file_timeout":
                raise TimeoutError("raw source content")
            return SimpleNamespace(id=file_id, deleted=False, _request_id="req_false")

    async def scenario():
        adapter = StoredConversationOpenAIAdapter(
            api_key="unused",
            client=SimpleNamespace(conversations=Conversations(), files=Files()),
            now=lambda: NOW,
        )
        receipt = await adapter.release_resource_ids(
            "conv_absent", ("file_timeout", "file_unconfirmed")
        )
        assert receipt.completed is False
        assert receipt.retryable is True
        assert [item.outcome for item in receipt.deletions] == [
            "ALREADY_ABSENT",
            "UNCONFIRMED",
            "UNCONFIRMED",
        ]
        assert receipt.deletions[1].safe_error_code == "DELETE_TIMEOUT"
        assert receipt.deletions[2].safe_error_code == "DELETE_NOT_CONFIRMED"
        assert receipt.deletions[1].client_request_id == (
            "specops-file-delete-file_timeout"
        )
        encoded = json.dumps([item.__dict__ for item in adapter.cleanup_events])
        assert "raw provider cleanup body" not in encoded
        assert "raw source content" not in encoded

    asyncio.run(scenario())


def test_response_delete_timeout_is_unconfirmed_and_keeps_stable_identity():
    class Responses:
        async def delete(self, response_id, **kwargs):
            raise TimeoutError("provider body must not enter the cleanup receipt")

    async def scenario():
        adapter = StoredConversationOpenAIAdapter(
            api_key="unused",
            client=SimpleNamespace(responses=Responses()),
            now=lambda: NOW,
        )
        receipt = await adapter.release_resource_ids(
            None, (), response_id="resp_timeout"
        )
        assert receipt.completed is False
        assert receipt.retryable is True
        assert len(receipt.deletions) == 1
        deletion = receipt.deletions[0]
        assert deletion.resource_kind == "RESPONSE"
        assert deletion.resource_id == "resp_timeout"
        assert deletion.client_request_id == "specops-response-delete-resp_timeout"
        assert deletion.outcome == "UNCONFIRMED"
        assert deletion.safe_error_code == "DELETE_TIMEOUT"
        assert "provider body" not in json.dumps(deletion.__dict__)

    asyncio.run(scenario())


async def _stored_conversation_bootstrap_case():
    fake = FakeOpenAI()
    adapter = StoredConversationOpenAIAdapter(api_key="unused", client=fake, now=lambda: NOW)
    sources = (
        _source(c.SourceRole.PM_SPEC, 10, "pm-spec.md", b"PM source"),
        _source(c.SourceRole.TECHNICAL_CONTRACT, 11, "technical-contract.md", b"Technical source"),
    )
    prepared = await adapter.prepare_context(sources)
    request = _bootstrap_request(prepared)
    fake.outputs.append(_brief(request).model_dump_json())
    result = await adapter.bootstrap(request, prepared=prepared, session_id=SESSION_ID)

    assert len(fake.file_calls) == 2
    assert [call["purpose"] for call in fake.file_calls] == ["user_data", "user_data"]
    assert [call["file"][0] for call in fake.file_calls] == ["pm-spec.md", "technical-contract.md"]
    assert len(fake.conversation_calls) == 1
    assert result.context.provider_conversation_id == "conv_workshop"
    assert result.context.session_id == SESSION_ID
    assert result.context.response_store_enabled is True
    assert result.context.conversation_state_persisted is True

    bootstrap_call = fake.response_calls[0]
    assert bootstrap_call["model"] == "gpt-5.6-terra"
    assert bootstrap_call["reasoning"] == {"effort": "medium", "context": "all_turns"}
    assert bootstrap_call["store"] is True
    assert bootstrap_call["conversation"] == "conv_workshop"
    assert "previous_response_id" not in bootstrap_call
    content = bootstrap_call["input"][0]["content"]
    assert [item["type"] for item in content] == ["input_file", "input_file", "input_text"]
    assert [item["file_id"] for item in content[:2]] == ["file_1", "file_2"]

    turn_data = {
        "protocol_version": "1.0.0",
        "request_type": c.AnalyzerOperation.TURN_ANALYSIS,
        "client_request_id": "client-turn",
        "analyzer_run_id": UUID("00000000-0000-4000-8000-000000000020"),
        "context_id": CONTEXT_ID,
        "provider_conversation_id": "conv_workshop",
        "source_set_hash": prepared.source_set.source_set_hash,
        "analyzer_contract": _contract(),
        "based_on_case_revision": 0,
        "transcript": c.FinalizedTranscriptInput(
            transcript_event_id=UUID("00000000-0000-4000-8000-000000000021"),
            transcript_hash=ZERO_HASH,
            speaker_actor_id=UUID("00000000-0000-4000-8000-000000000022"),
            actor=c.TranscriptActor.PM,
            sequence_number=1,
            text="Hello.",
        ),
        "prior_transcript": None,
        "foundation_snapshot": _snapshot(prepared.source_set.source_set_hash),
        "requested_output": "TURN_ANALYSIS_CANDIDATE",
    }
    turn_data["request_hash"] = _request_hash(turn_data)
    turn_request = c.AnalyzeFinalTurnRequest.model_validate(turn_data)
    turn_candidate = c.TurnAnalysisCandidate(
        protocol_version="1.0.0",
        output_type="TURN_ANALYSIS_CANDIDATE",
        analyzer_run_id=turn_request.analyzer_run_id,
        context_id=CONTEXT_ID,
        request_hash=turn_request.request_hash,
        source_set_hash=prepared.source_set.source_set_hash,
        transcript_event_id=turn_request.transcript.transcript_event_id,
        based_on_case_revision=0,
        disposition=c.TurnDisposition.NO_SEMANTIC_CHANGE,
        no_change_reason_code="SOCIAL_ONLY",
        evidence_candidates=(),
        new_problems=(),
        new_problem_clusters=(),
        revised_problem_clusters=(),
        new_questions=(),
        revised_questions=(),
        low_risk_facts=(),
        decisions=(),
        problem_assessments=(),
        evidence_findings=(),
    )
    fake.outputs.append(turn_candidate.model_dump_json())
    checkpoints: list[str] = []
    returned = await adapter.execute_with_response_checkpoint(
        turn_request,
        context=result.context,
        checkpoint=checkpoints.append,
    )
    assert returned == turn_candidate
    assert checkpoints == ["resp_2"]
    turn_call = fake.response_calls[1]
    assert turn_call["conversation"] == "conv_workshop"
    assert turn_call["store"] is True
    assert "previous_response_id" not in turn_call
    assert [item["type"] for item in turn_call["input"][0]["content"]] == ["input_text"]

    fake.outputs.append(turn_candidate.model_dump_json())
    correction_checkpoints: list[str] = []
    corrected = await adapter.execute_turn_analysis_correction_with_response_checkpoint(
        turn_request,
        context=result.context,
        quarantined_candidate_keys=(
            "evidence-invalid",
            "problem-invalid",
            "question-invalid",
        ),
        checkpoint=correction_checkpoints.append,
    )
    assert corrected == turn_candidate
    assert correction_checkpoints == ["resp_3"]
    correction_call = fake.response_calls[2]
    correction_content = correction_call["input"][0]["content"]
    assert [item["type"] for item in correction_content] == [
        "input_text",
        "input_text",
    ]
    assert all(item["type"] != "input_file" for item in correction_content)
    correction_json = json.dumps(correction_call)
    assert "independent verified branch is already admitted" in correction_json
    assert "question-invalid" in correction_json
    assert correction_call["extra_headers"] == {
        "X-Client-Request-Id": turn_request.client_request_id
    }
    resumed = await adapter.resume_stored_response(
        turn_request,
        context=result.context,
        response_id="resp_3",
    )
    assert resumed == turn_candidate
    assert len(fake.response_calls) == 3
    assert fake.response_retrieve_calls == [
        (
            "resp_3",
            {"extra_headers": {"X-Client-Request-Id": turn_request.client_request_id}},
        )
    ]

    fake.outputs.append("not-json")
    invalid_checkpoints: list[str] = []
    with pytest.raises(ProviderAdapterError):
        await adapter.execute_with_response_checkpoint(
            turn_request,
            context=result.context,
            checkpoint=invalid_checkpoints.append,
        )
    assert invalid_checkpoints == ["resp_4"]


def test_background_bootstrap_polls_one_stored_response_past_60_second_slow_observation():
    async def scenario():
        clock = _ManualMonotonic()
        responses = _BackgroundResponses()
        adapter = StoredConversationOpenAIAdapter(
            api_key="unused",
            client=SimpleNamespace(responses=responses),
            now=lambda: NOW,
            monotonic=clock,
            sleep=clock.advance,
        )
        sources = (
            _source(c.SourceRole.PM_SPEC, 10, "pm-spec.md", b"private PM source"),
            _source(
                c.SourceRole.TECHNICAL_CONTRACT,
                11,
                "technical-contract.md",
                b"private technical source",
            ),
        )
        prepared = adapter.prepared_from_ids(
            sources, ("file_pm", "file_technical"), "conv_background"
        )
        request = _bootstrap_request(prepared)
        responses.output_text = _brief(request).model_dump_json()

        response_id = await adapter.start_bootstrap(request, prepared=prepared)
        result = await adapter.finish_bootstrap(
            request,
            prepared=prepared,
            session_id=SESSION_ID,
            response_id=response_id,
        )

        assert result.context.bootstrap_response_id == response_id
        assert len(responses.create_calls) == 1
        assert responses.create_calls[0]["background"] is True
        assert responses.retrieve_ids == [response_id] * 7
        assert [event.event for event in adapter.lifecycle_events] == [
            "provider_request.started",
            "provider_request.accepted",
            "provider_request.timeout",
            "provider_request.completed",
        ]
        assert adapter.lifecycle_events[-1].duration_ms == 70_000
        assert adapter.lifecycle_events[-1].input_tokens == 100
        assert adapter.lifecycle_events[-1].cached_input_tokens == 20
        assert adapter.lifecycle_events[-1].output_tokens == 40
        assert adapter.lifecycle_events[-1].reasoning_output_tokens == 10
        assert adapter.lifecycle_events[-1].total_tokens == 140
        encoded = json.dumps([event.__dict__ for event in adapter.lifecycle_events])
        assert "private PM source" not in encoded
        assert "private technical source" not in encoded

    asyncio.run(scenario())


def test_grounding_correction_reuses_conversation_without_reattaching_source_files():
    async def scenario():
        responses = _BackgroundResponses()
        adapter = StoredConversationOpenAIAdapter(
            api_key="unused",
            client=SimpleNamespace(responses=responses),
            now=lambda: NOW,
        )
        sources = (
            _source(c.SourceRole.PM_SPEC, 10, "pm-spec.md", b"private PM source"),
            _source(
                c.SourceRole.TECHNICAL_CONTRACT,
                11,
                "technical-contract.md",
                b"private technical source",
            ),
        )
        prepared = adapter.prepared_from_ids(
            sources, ("file_pm", "file_technical"), "conv_background"
        )
        request = _bootstrap_request(prepared)

        response_id = await adapter.start_bootstrap_grounding_correction(
            request, prepared=prepared
        )

        assert response_id == "resp_background_bootstrap"
        call = responses.create_calls[0]
        assert call["background"] is True
        assert call["conversation"] == "conv_background"
        assert call["extra_headers"] == {
            "X-Client-Request-Id": request.client_request_id
        }
        content = call["input"][0]["content"]
        assert [item["type"] for item in content] == ["input_text", "input_text"]
        assert all(item["type"] != "input_file" for item in content)
        encoded = json.dumps(call)
        assert "prior completed BOOTSTRAP candidate" in encoded
        assert "private PM source" not in encoded
        assert "private technical source" not in encoded

    asyncio.run(scenario())


def test_graph_correction_reuses_conversation_without_reattaching_source_files():
    async def scenario():
        responses = _BackgroundResponses()
        adapter = StoredConversationOpenAIAdapter(
            api_key="unused",
            client=SimpleNamespace(responses=responses),
            now=lambda: NOW,
        )
        sources = (
            _source(c.SourceRole.PM_SPEC, 10, "pm-spec.md", b"private PM source"),
            _source(
                c.SourceRole.TECHNICAL_CONTRACT,
                11,
                "technical-contract.md",
                b"private technical source",
            ),
        )
        prepared = adapter.prepared_from_ids(
            sources, ("file_pm", "file_technical"), "conv_background"
        )
        request = _bootstrap_request(prepared)

        response_id = await adapter.start_bootstrap_graph_correction(
            request, prepared=prepared
        )

        assert response_id == "resp_background_bootstrap"
        call = responses.create_calls[0]
        assert call["background"] is True
        assert call["conversation"] == "conv_background"
        assert call["extra_headers"] == {
            "X-Client-Request-Id": request.client_request_id
        }
        content = call["input"][0]["content"]
        assert [item["type"] for item in content] == ["input_text", "input_text"]
        assert all(item["type"] != "input_file" for item in content)
        encoded = json.dumps(call)
        assert "question referenced a problem key" in encoded
        assert "problems[].candidate_key" in encoded
        assert "private PM source" not in encoded
        assert "private technical source" not in encoded

    asyncio.run(scenario())


def test_bootstrap_unknown_question_problem_reference_exposes_only_safe_correction_code():
    async def scenario():
        responses = _BackgroundResponses(cancel_wait_once=True)
        adapter = StoredConversationOpenAIAdapter(
            api_key="unused",
            client=SimpleNamespace(responses=responses),
            now=lambda: NOW,
        )
        sources = (
            _source(c.SourceRole.PM_SPEC, 10, "pm-spec.md", b"private PM source"),
            _source(
                c.SourceRole.TECHNICAL_CONTRACT,
                11,
                "technical-contract.md",
                b"private technical source",
            ),
        )
        prepared = adapter.prepared_from_ids(
            sources, ("file_pm", "file_technical"), "conv_background"
        )
        request = _bootstrap_request(prepared)
        payload = _brief(request).model_dump(mode="json")
        payload["questions"][0]["addresses_problem_keys"] = [
            "private-missing-problem-key"
        ]
        responses.output_text = json.dumps(payload)

        response_id = await adapter.start_bootstrap(request, prepared=prepared)
        with pytest.raises(ProviderAdapterError) as captured:
            await adapter.finish_bootstrap(
                request,
                prepared=prepared,
                session_id=SESSION_ID,
                response_id=response_id,
            )

        assert captured.value.receipt.code is c.ProviderFailureCode.OUTPUT_INVALID
        assert (
            captured.value.bootstrap_correction_code
            == "UNKNOWN_QUESTION_PROBLEM_REFERENCE"
        )
        dumped = captured.value.receipt.model_dump_json()
        assert "private-missing-problem-key" not in dumped
        assert "private PM source" not in dumped

    asyncio.run(scenario())


def test_cancelled_background_wait_resumes_the_same_response_without_recreate():
    async def scenario():
        clock = _ManualMonotonic()
        responses = _BackgroundResponses(cancel_wait_once=True)
        cancelled = False

        async def cancel_once(seconds: float) -> None:
            nonlocal cancelled
            if not cancelled:
                cancelled = True
                raise asyncio.CancelledError
            await clock.advance(seconds)

        adapter = StoredConversationOpenAIAdapter(
            api_key="unused",
            client=SimpleNamespace(responses=responses),
            now=lambda: NOW,
            monotonic=clock,
            sleep=cancel_once,
        )
        sources = (
            _source(c.SourceRole.PM_SPEC, 10, "pm-spec.md", b"PM source"),
            _source(c.SourceRole.TECHNICAL_CONTRACT, 11, "technical.md", b"Technical"),
        )
        prepared = adapter.prepared_from_ids(
            sources, ("file_pm", "file_technical"), "conv_background"
        )
        request = _bootstrap_request(prepared)
        responses.output_text = _brief(request).model_dump_json()
        response_id = await adapter.start_bootstrap(request, prepared=prepared)

        with pytest.raises(asyncio.CancelledError):
            await adapter.finish_bootstrap(
                request,
                prepared=prepared,
                session_id=SESSION_ID,
                response_id=response_id,
            )
        result = await adapter.finish_bootstrap(
            request,
            prepared=prepared,
            session_id=SESSION_ID,
            response_id=response_id,
        )

        assert result.context.bootstrap_response_id == response_id
        assert len(responses.create_calls) == 1
        cancelled_event = next(
            event for event in adapter.lifecycle_events if event.event == "provider_request.cancelled"
        )
        assert cancelled_event.response_id == response_id

    asyncio.run(scenario())


def test_installed_openai_sdk_serializes_the_native_stored_conversation_contract():
    """Exercise SDK request serialization without making a network call.

    The light fake above proves adapter behavior.  This transport-level test is
    separate because a permissive fake cannot detect an argument shape that the
    installed OpenAI SDK would reject or serialize differently.
    """

    asyncio.run(_sdk_serialization_case())


async def _sdk_serialization_case():
    captured: list[tuple[str, str, bytes, str]] = []
    candidate_outputs: list[str] = []
    file_number = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal file_number
        body = await request.aread()
        captured.append(
            (
                request.method,
                request.url.path,
                body,
                request.headers.get("content-type", ""),
            )
        )
        if request.url.path == "/v1/files":
            file_number += 1
            return httpx.Response(
                200,
                json={
                    "id": f"file_sdk_{file_number}",
                    "bytes": 9,
                    "created_at": 1,
                    "filename": "source.md",
                    "object": "file",
                    "purpose": "user_data",
                    "status": "processed",
                },
                headers={"x-request-id": f"req_file_{file_number}"},
            )
        if request.url.path == "/v1/conversations":
            return httpx.Response(
                200,
                json={
                    "id": "conv_sdk",
                    "created_at": 1,
                    "metadata": {"protocol": "1.0.0"},
                    "object": "conversation",
                },
                headers={"x-request-id": "req_conversation"},
            )
        if request.url.path == "/v1/responses":
            output = candidate_outputs.pop(0)
            return httpx.Response(
                200,
                json={
                    "id": "resp_sdk",
                    "created_at": 1,
                    "model": "gpt-5.6-terra",
                    "object": "response",
                    "output": [
                        {
                            "id": "msg_sdk",
                            "content": [
                                {"annotations": [], "text": output, "type": "output_text"}
                            ],
                            "role": "assistant",
                            "status": "completed",
                            "type": "message",
                        }
                    ],
                    "parallel_tool_calls": True,
                    "tool_choice": "auto",
                    "tools": [],
                },
                headers={"x-request-id": "req_response"},
            )
        raise AssertionError(f"unexpected SDK request path: {request.url.path}")

    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport)
    sdk = AsyncOpenAI(api_key="test", http_client=http_client)
    try:
        adapter = StoredConversationOpenAIAdapter(api_key="unused", client=sdk, now=lambda: NOW)
        sources = (
            _source(c.SourceRole.PM_SPEC, 10, "pm-spec.md", b"PM source"),
            _source(c.SourceRole.TECHNICAL_CONTRACT, 11, "technical-contract.md", b"Technical source"),
        )
        prepared = await adapter.prepare_context(sources)
        request = _bootstrap_request(prepared)
        candidate_outputs.append(_brief(request).model_dump_json())
        result = await adapter.bootstrap(request, prepared=prepared, session_id=SESSION_ID)
    finally:
        await sdk.close()

    assert result.context.provider_conversation_id == "conv_sdk"
    assert [(method, path) for method, path, _, _ in captured] == [
        ("POST", "/v1/files"),
        ("POST", "/v1/files"),
        ("POST", "/v1/conversations"),
        ("POST", "/v1/responses"),
    ]
    assert all("multipart/form-data" in content_type for _, path, _, content_type in captured if path == "/v1/files")
    response_body = json.loads(next(body for _, path, body, _ in captured if path == "/v1/responses"))
    assert response_body["model"] == "gpt-5.6-terra"
    assert response_body["conversation"] == "conv_sdk"
    assert response_body["reasoning"] == {"context": "all_turns", "effort": "medium"}
    assert response_body["store"] is True
    assert response_body["background"] is True
    assert response_body["text"]["format"]["strict"] is True
    assert [item["type"] for item in response_body["input"][0]["content"]] == [
        "input_file",
        "input_file",
        "input_text",
    ]


def test_adapter_rejects_source_hash_mismatch_before_any_provider_call():
    asyncio.run(_source_hash_mismatch_case())


async def _source_hash_mismatch_case():
    fake = FakeOpenAI()
    adapter = StoredConversationOpenAIAdapter(api_key="unused", client=fake, now=lambda: NOW)
    invalid = _source(c.SourceRole.PM_SPEC, 10, "pm-spec.md", b"PM source")
    invalid = ProviderSourceUpload(
        source=invalid.source.model_copy(update={"payload_hash": ZERO_HASH}),
        content=invalid.content,
    )
    with pytest.raises(ValueError, match="payload hash"):
        await adapter.prepare_context(
            (
                invalid,
                _source(c.SourceRole.TECHNICAL_CONTRACT, 11, "technical-contract.md", b"Technical"),
            )
        )
    assert fake.file_calls == []


def test_invalid_provider_output_emits_only_bounded_safe_diagnostics():
    asyncio.run(_invalid_provider_output_case())


async def _invalid_provider_output_case():
    fake = FakeOpenAI()
    adapter = StoredConversationOpenAIAdapter(api_key="unused", client=fake, now=lambda: NOW)
    prepared = await adapter.prepare_context(
        (
            _source(c.SourceRole.PM_SPEC, 10, "pm-spec.md", b"PM source"),
            _source(c.SourceRole.TECHNICAL_CONTRACT, 11, "technical-contract.md", b"Technical"),
        )
    )
    request = _bootstrap_request(prepared)
    fake.outputs.append('{"secret_unknown_key":"raw transcript and credential"}')
    with pytest.raises(ProviderAdapterError) as failure:
        await adapter.bootstrap(request, prepared=prepared, session_id=SESSION_ID)
    dumped = failure.value.receipt.model_dump_json()
    assert failure.value.receipt.code is c.ProviderFailureCode.OUTPUT_INVALID
    assert failure.value.receipt.validation_diagnostics
    assert "raw transcript" not in dumped
    assert "credential" not in dumped
    assert "secret_unknown_key" not in dumped
    assert any("$unknown" in item.path for item in failure.value.receipt.validation_diagnostics)


def test_unmapped_http_400_is_never_stored_as_message_or_bare_http_400():
    adapter = StoredConversationOpenAIAdapter(api_key="unused", client=FakeOpenAI(), now=lambda: NOW)
    error = RuntimeError("full provider body with source text")
    error.status_code = 400
    error.request_id = "req_safe"
    error.code = "something_new"
    error.param = "something_new"
    safe = adapter._provider_error(
        error,
        stage=c.ProviderProcessingStage.TURN_ANALYSIS,
        client_request_id="client-safe",
    )
    dumped = safe.receipt.model_dump_json()
    assert safe.receipt.code is c.ProviderFailureCode.UNKNOWN_SAFE
    assert "full provider body" not in dumped
    assert "something_new" not in dumped
