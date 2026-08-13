from __future__ import annotations

from datetime import datetime, timezone
import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from fastapi.testclient import TestClient
from pydantic import SecretStr

from specops_contracts import workshop_v1 as c
from specops_contracts.canonical import analyzer_request_hash
from specops_workshop.api import create_app
from specops_workshop.config import Settings
from specops_workshop.sources import SourceCatalog
from specops_workshop.v4.openai_adapter import BootstrapResult, PreparedProviderContext
from specops_workshop.v4.orchestrator import V4ProductionOrchestrator


ROOT = next(
    parent for parent in Path(__file__).resolve().parents if parent.name == "Spec_Eng"
)


class DeterministicAdapter:
    def __init__(self, conversation_id="conv_task26", available=True):
        self.operations = []
        self.conversation_id = conversation_id
        self.available = available
        self.released = []

    @staticmethod
    def source_set_hash(sources):
        from specops_workshop.v4.openai_adapter import StoredConversationOpenAIAdapter

        return StoredConversationOpenAIAdapter.source_set_hash(sources)

    async def prepare_context(self, sources):
        return PreparedProviderContext(
            provider_conversation_id=self.conversation_id,
            source_set=c.SourceSetBinding(
                source_set_hash=self.source_set_hash(tuple(item.source for item in sources)),
                ordered_sources=tuple(
                    c.ProviderSourceBinding(
                        source=item.source, provider_file_id=f"file_{index}"
                    )
                    for index, item in enumerate(sources, start=1)
                ),
            ),
        )

    async def bootstrap(self, request, *, prepared, session_id):
        self.operations.append(request.request_type)
        evidence = c.EvidenceCandidate(
            candidate_key="evidence-export",
            source_role=c.SourceRole.PM_SPEC,
            locator=c.SourceLineLocator(
                locator_kind=c.SourceLocatorKind.SOURCE_LINES,
                start_line=1,
                end_line=1,
            ),
            relevance_claim="The source identifies the product workshop.",
            quoted_text_candidate=None,
        )
        problem = c.ProblemCandidate(
            candidate_key="problem-export",
            problem_kind=c.ProblemKind.MISSING_DECISION,
            domain=c.Domain.PRODUCT,
            severity=c.Severity.HIGH,
            statement="One product decision remains open.",
            consequence="The export behavior cannot yet be confirmed.",
            evidence_candidate_keys=("evidence-export",),
        )
        questions = tuple(
            c.QuestionCandidate(
                candidate_key=f"question-export-{index}",
                text=f"Which export behavior should be confirmed for area {index}?",
                rationale="The product decision requires an independently safe human answer.",
                question_shape=c.QuestionShape.OPEN_TEXT,
                capture_policy=c.CapturePolicy.CLARIFICATION_ONLY,
                answer_options=(),
                addresses_problem_keys=("problem-export",),
                prerequisite_problem_keys=(),
                safe_without_current_turn_interpretation=True,
            )
            for index in range(1, c.INITIAL_RUNWAY_DEPTH + 1)
        )
        candidate = c.InterviewBriefCandidate(
            protocol_version="1.0.0",
            output_type="INTERVIEW_BRIEF_CANDIDATE",
            analyzer_run_id=request.analyzer_run_id,
            context_id=request.context_id,
            request_hash=request.request_hash,
            source_set_hash=request.source_set_hash,
            based_on_case_revision=request.based_on_case_revision,
            customer_promise_summary="Produce an evidence-bound export specification.",
            evidence_candidates=(evidence,),
            problems=(problem,),
            problem_clusters=(),
            questions=questions,
            initial_runway=c.QuestionRunwayCandidate(
                recommended_question_key="question-export-1",
                safe_alternate_question_keys=tuple(
                    f"question-export-{index}"
                    for index in range(2, c.INITIAL_RUNWAY_DEPTH + 1)
                ),
                do_not_ask_question_keys=(),
            ),
            confirmation_checkpoints=(),
        )
        context = c.AnalyzerContextBinding(
            protocol_version="1.0.0",
            context_id=request.context_id,
            session_id=session_id,
            provider=c.ProviderName.OPENAI,
            provider_conversation_id=prepared.provider_conversation_id,
            bootstrap_response_id="resp_bootstrap",
            model="gpt-5.6-terra",
            reasoning_effort=c.ReasoningEffort.MEDIUM,
            conversation_state_persisted=True,
            response_store_enabled=True,
            analyzer_contract=request.analyzer_contract,
            source_set=prepared.source_set,
            status=c.ContextStatus.ACTIVE,
            created_at=datetime.now(timezone.utc),
            invalidated_at=None,
            invalidation_reason=None,
        )
        return BootstrapResult(context=context, candidate=candidate)

    async def execute(self, request, *, context):
        self.operations.append(request.request_type)
        return c.TurnAnalysisCandidate(
            protocol_version="1.0.0",
            output_type="TURN_ANALYSIS_CANDIDATE",
            analyzer_run_id=request.analyzer_run_id,
            context_id=request.context_id,
            request_hash=request.request_hash,
            source_set_hash=request.source_set_hash,
            transcript_event_id=request.transcript.transcript_event_id,
            based_on_case_revision=request.based_on_case_revision,
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

    async def context_is_available(self, context):
        return self.available

    async def release_context(self, context):
        self.released.append(context.provider_conversation_id)

    async def release_prepared(self, prepared):
        return None


def configured(tmp_path):
    return Settings(
        specops_database_url=f"sqlite:///{tmp_path / 'foundation.sqlite'}",
        workshop_database_url=f"sqlite:///{tmp_path / 'workshop.sqlite'}",
        gemini_api_key=SecretStr("not-called"),
        openai_api_key=SecretStr("not-called"),
    )


def test_production_factory_uses_stored_conversation_v4_path_and_replays_duplicates(tmp_path):
    adapter = DeterministicAdapter()
    app = create_app(
        settings=configured(tmp_path),
        source_catalog=SourceCatalog(ROOT),
        live_provider=object(),
        analyzer_adapter=adapter,
    )
    with TestClient(app) as client:
        payload = {
            "turn_sequence": 1,
            "text": "Thanks, let us continue.",
            "provider_request_id": "http-final-1",
            "correction_of_version": None,
        }
        first = client.post("/api/session/final-turn", json=payload)
        replay = client.post("/api/session/final-turn", json=payload)
        for _ in range(100):
            if client.get("/api/workshop").json()["analyzer_jobs"][0]["state"] == "COMPLETED":
                break
            time.sleep(0.01)
    assert first.status_code == 200, first.text
    assert replay.status_code == 200, replay.text
    assert first.json()["duplicate"] is False
    assert replay.json()["duplicate"] is True
    assert adapter.operations == [
        c.AnalyzerOperation.BOOTSTRAP,
        c.AnalyzerOperation.TURN_ANALYSIS,
    ]
    recovered = app.state.workshop_protocol_foundation.active_analyzer_context(
        app.state.bootstrap.case_id
    )
    assert recovered is not None
    assert recovered.provider_conversation_id == "conv_task26"


def test_production_source_excludes_the_legacy_orchestration_boundary():
    source = (Path(__file__).resolve().parents[2] / "src/specops_workshop/api.py").read_text()
    for forbidden in ("TerraResponsesProvider", "WorkshopGate", "app.state.gate"):
        assert forbidden not in source


def test_restart_reuses_durable_ready_context_without_duplicate_provider_work(tmp_path):
    first = DeterministicAdapter()
    settings = configured(tmp_path)
    initial = create_app(
        settings=settings, source_catalog=SourceCatalog(ROOT),
        live_provider=object(), analyzer_adapter=first,
    )
    asyncio.run(initial.state.workshop_protocol_orchestrator.ensure_context())
    rebuilt = DeterministicAdapter(conversation_id="conv_task26_rebuilt", available=False)
    restarted = create_app(
        settings=settings, source_catalog=SourceCatalog(ROOT),
        live_provider=object(), analyzer_adapter=rebuilt,
    )
    context = asyncio.run(restarted.state.workshop_protocol_orchestrator.ensure_context())
    assert rebuilt.released == []
    assert rebuilt.operations == []
    assert context.provider_conversation_id == "conv_task26"
    recovered = restarted.state.workshop_protocol_foundation.active_analyzer_context(
        restarted.state.bootstrap.case_id
    )
    assert recovered is not None
    assert recovered.provider_conversation_id == "conv_task26"


def test_post_bootstrap_operations_route_to_their_exact_foundation_admissions():
    async def scenario():
        source_set = c.SourceSetBinding(
            source_set_hash="sha256:" + "1" * 64,
            ordered_sources=tuple(
                c.ProviderSourceBinding(
                    source=c.SourceIdentity(
                        source_id=UUID(f"20000000-0000-4000-8000-{index:012d}"),
                        role=role,
                        version=1,
                        payload_hash="sha256:" + str(index) * 64,
                        canonical_locator=f"/fixture/{index}.md",
                        filename=f"{index}.md",
                        media_type="text/markdown",
                    ),
                    provider_file_id=f"file_{index}",
                )
                for index, role in ((2, c.SourceRole.PM_SPEC), (3, c.SourceRole.TECHNICAL_CONTRACT))
            ),
        )
        contract = c.AnalyzerContractBinding(
            protocol_version="1.0.0",
            instruction_set_id="specops-workshop-analyzer",
            instruction_set_version=1,
            instruction_set_hash="sha256:" + "4" * 64,
            semantic_quality_contract_id="SEMANTIC-QUALITY-CONTRACT",
            semantic_quality_contract_version="2.1.0",
            semantic_quality_contract_hash="sha256:" + "5" * 64,
            provider_schema_version="1.0.0",
            model="gpt-5.6-terra",
            reasoning_effort=c.ReasoningEffort.MEDIUM,
        )
        context = c.AnalyzerContextBinding(
            protocol_version="1.0.0",
            context_id=UUID("20000000-0000-4000-8000-000000000010"),
            session_id=UUID("20000000-0000-4000-8000-000000000011"),
            provider=c.ProviderName.OPENAI,
            provider_conversation_id="conv_matrix",
            bootstrap_response_id="resp_matrix",
            model="gpt-5.6-terra",
            reasoning_effort=c.ReasoningEffort.MEDIUM,
            conversation_state_persisted=True,
            response_store_enabled=True,
            analyzer_contract=contract,
            source_set=source_set,
            status=c.ContextStatus.ACTIVE,
            created_at=datetime.now(timezone.utc),
            invalidated_at=None,
            invalidation_reason=None,
        )

        class Foundation:
            def __init__(self): self.commands = []
            def active_analyzer_context(self, case_id): return context
            def execute(self, command):
                self.commands.append(command)
                return SimpleNamespace(receipt_type=command.command_type)

        class Adapter:
            def __init__(self): self.candidate = None
            @staticmethod
            def source_set_hash(sources): return source_set.source_set_hash
            async def context_is_available(self, value): return True
            async def execute(self, request, *, context): return self.candidate

        foundation, adapter = Foundation(), Adapter()
        orchestrator = V4ProductionOrchestrator(
            foundation=foundation,
            adapter=adapter,
            case_id=UUID("20000000-0000-4000-8000-000000000012"),
            session_id=context.session_id,
            sources=(),  # provider preparation is not reached with the recovered context
            analyzer_contract=contract,
        )
        orchestrator.source_set_hash = source_set.source_set_hash

        target_spec = c.ArtifactDraftTarget(
            artifact_type="SPEC_PACKAGE",
            foundation_artifact_id=UUID("20000000-0000-4000-8000-000000000020"),
            artifact_key="SPEC-MATRIX",
            next_artifact_version=1,
        )
        target_tech = c.ArtifactDraftTarget(
            artifact_type="TECHNICAL_CONTRACT",
            foundation_artifact_id=UUID("20000000-0000-4000-8000-000000000021"),
            artifact_key="CONTRACT-MATRIX",
            next_artifact_version=1,
        )
        plans = {
            "spec": c.ArtifactSynthesisIdentityPlan(
                identity_plan_id=UUID("20000000-0000-4000-8000-000000000022"), identity_plan_version=1,
                target=target_spec, based_on_case_revision=8, semantic_state_hash="sha256:" + "6" * 64,
                planned_identities=(c.PlannedArtifactIdentity(foundation_id=UUID("20000000-0000-4000-8000-000000000023"), foundation_version=1, entity_kind="REQUIREMENT", source_entity_refs=()),),
            ),
            "tech": c.ArtifactSynthesisIdentityPlan(
                identity_plan_id=UUID("20000000-0000-4000-8000-000000000024"), identity_plan_version=1,
                target=target_tech, based_on_case_revision=8, semantic_state_hash="sha256:" + "7" * 64,
                planned_identities=(c.PlannedArtifactIdentity(foundation_id=UUID("20000000-0000-4000-8000-000000000025"), foundation_version=1, entity_kind="COMPONENT", source_entity_refs=()),),
            ),
        }
        common = dict(
            protocol_version="1.0.0", client_request_id="matrix", context_id=context.context_id,
            provider_conversation_id=context.provider_conversation_id, source_set_hash=source_set.source_set_hash,
            analyzer_contract=contract, based_on_case_revision=8,
        )
        cases = []
        for index, (request_cls, operation, extras) in enumerate((
            (c.ReplenishGuidanceRequest, c.AnalyzerOperation.GUIDANCE, {}),
            (c.GenerateReviewNarrationRequest, c.AnalyzerOperation.REVIEW_NARRATION, {}),
            (c.SpecPackageSynthesisRequest, c.AnalyzerOperation.SPEC_PACKAGE_SYNTHESIS, {"target": target_spec, "identity_plan": plans["spec"]}),
            (c.TechnicalContractSynthesisRequest, c.AnalyzerOperation.TECHNICAL_CONTRACT_SYNTHESIS, {"target": target_tech, "identity_plan": plans["tech"], "confirmed_spec": c.ConfirmedSpecSynthesisBinding(foundation_artifact_id=target_spec.foundation_artifact_id, artifact_key=target_spec.artifact_key, artifact_version=1, record_revision=1, confirmed_case_revision=8, payload_hash="sha256:" + "8" * 64, confirmation_id=UUID("20000000-0000-4000-8000-000000000026"), canonical_payload_json="{}")}),
        )):
            run_id = UUID(f"20000000-0000-4000-8000-{30 + index:012d}")
            values = dict(common, request_type=operation, analyzer_run_id=run_id, request_hash="sha256:" + "0" * 64, **extras)
            request = request_cls.model_construct(**values)
            request = request.model_copy(update={"request_hash": analyzer_request_hash(request.model_dump(mode="json"))})
            echo = dict(
                protocol_version="1.0.0", analyzer_run_id=run_id,
                context_id=context.context_id, request_hash=request.request_hash,
                source_set_hash=source_set.source_set_hash, based_on_case_revision=8,
            )
            if operation is c.AnalyzerOperation.GUIDANCE:
                question_ref = c.FoundationEntityRef(
                    ref_kind="FOUNDATION_ID",
                    foundation_id=UUID("20000000-0000-4000-8000-000000000040"),
                    expected_version=1,
                )
                candidate = c.GuidanceCandidate(
                    **echo, output_type="GUIDANCE_CANDIDATE",
                    recommended_question=c.GuidanceQuestion(question_ref=question_ref, exact_text="Which option should be confirmed?", reason="A human decision remains open."),
                    safe_alternates=(), do_not_ask_question_refs=(),
                    dependencies=(c.GuidanceDependency(dependency_kind=c.GuidanceDependencyKind.SOURCE_SET, entity_ref=None),),
                    acknowledgement_suggestion="The prior answer was recorded.",
                )
            elif operation is c.AnalyzerOperation.REVIEW_NARRATION:
                candidate = c.ReviewNarrationCandidate(
                    **echo, output_type="REVIEW_NARRATION_CANDIDATE",
                    decision_batch_view_id=UUID("20000000-0000-4000-8000-000000000041"),
                    decision_batch_view_hash="sha256:" + "9" * 64,
                    spoken_opening="Review the exact pending decision.",
                    items=(c.ReviewNarrationItemCandidate(handle="A", core_concept="One pending decision.", material_considerations=()),),
                    spoken_confirmation_question="Confirm, revise, reject, or defer item A?",
                )
            else:
                candidate_cls = c.TechnicalContractSynthesisCandidate if operation is c.AnalyzerOperation.TECHNICAL_CONTRACT_SYNTHESIS else c.SpecPackageSynthesisCandidate
                synthesis_echo = {key: value for key, value in echo.items() if key != "protocol_version"}
                candidate = candidate_cls(
                    **synthesis_echo,
                    output_type="TECHNICAL_CONTRACT_SYNTHESIS_CANDIDATE" if operation is c.AnalyzerOperation.TECHNICAL_CONTRACT_SYNTHESIS else "SPEC_PACKAGE_SYNTHESIS_CANDIDATE",
                    foundation_artifact_id=extras["target"].foundation_artifact_id,
                    identity_plan_id=extras["identity_plan"].identity_plan_id,
                    identity_plan_version=1,
                    semantic_state_hash=extras["identity_plan"].semantic_state_hash,
                    candidate_payload_json="{}",
                    payload_schema_id="technical-contract-payload" if operation is c.AnalyzerOperation.TECHNICAL_CONTRACT_SYNTHESIS else "spec-package-payload",
                    payload_schema_version="4.0.0",
                )
            cases.append((request, candidate))
        for request, candidate in cases:
            adapter.candidate = candidate
            await orchestrator.execute_operation(request)
        assert [command.command_type for command in foundation.commands] == [
            "ADMIT_GUIDANCE", "ADMIT_REVIEW_NARRATION",
            "ADMIT_SPEC_PACKAGE_SYNTHESIS", "ADMIT_TECHNICAL_CONTRACT_SYNTHESIS",
        ]

    asyncio.run(scenario())
