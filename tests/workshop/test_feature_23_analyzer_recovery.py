from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from specops_workflow import FrozenClock
from specops_workflow.models import QueryOne
from specops_workshop.analyzer import (
    AnalyzerProviderAvailabilityError,
    AnalyzerProviderSchemaError,
    AnalyzerTurnResult,
    ControlIntent,
    GroundedSemanticText,
    SemanticPackageDelta,
    SemanticTurnDraft,
    SupportingExcerpt,
)
from specops_workshop.legacy_test_app import DEMO_SESSION_ID, create_app
from specops_workshop.config import GEMINI_MODEL, TERRA_MODEL, Settings
from specops_workshop.contracts import (
    AnalyzerCheckpointStage,
    AnalyzerCheckpointStatus,
    AnalyzerFailureKind,
    AnalyzerRecoveryAction,
    ProposalStatus,
)
from specops_workshop.evidence import DeterministicEvidenceRetriever, EvidenceIndex
from specops_workshop.gate import WorkshopGate
from specops_workshop.providers.openai_responses import TerraResponsesProvider
from specops_workshop.sessions import WorkshopStore
from specops_workshop.sessions.store import analyzer_checkpoints, package_proposals
from specops_workshop.sources import SourceCatalog


ROOT = Path("/Users/rudinro/Desktop/SpecOps/Spec_Eng")
NOW = datetime(2026, 8, 10, 0, 30, tzinfo=timezone.utc)


def configured(tmp_path: Path) -> Settings:
    return Settings.load({
        "SPECOPS_DATABASE_URL": f"sqlite:///{tmp_path / 'foundation.sqlite'}",
        "WORKSHOP_DATABASE_URL": f"sqlite:///{tmp_path / 'workshop.sqlite'}",
        "GEMINI_API_KEY": "gemini-feature-23-fixture",
        "OPENAI_API_KEY": "openai-feature-23-fixture",
        "GEMINI_LIVE_MODEL": GEMINI_MODEL,
        "OPENAI_ANALYZER_MODEL": TERRA_MODEL,
    })


class FakeBudget:
    def __init__(self) -> None:
        self.value = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.advance(seconds)


class GroundingFixture:
    def __init__(self, supported: bool = True) -> None:
        self.supported = supported

    def supports(self, _claim, _refs) -> bool:
        return self.supported


class SequenceAnalyzer:
    def __init__(self, *outcomes, budget: FakeBudget | None = None) -> None:
        self.outcomes = list(outcomes)
        self.requests = []
        self.budget = budget

    async def analyze(self, request):
        self.requests.append(request)
        outcome = self.outcomes[min(len(self.requests) - 1, len(self.outcomes) - 1)]
        if isinstance(outcome, tuple):
            advance, outcome = outcome
            assert self.budget is not None
            self.budget.advance(advance)
        if callable(outcome):
            outcome = outcome(request)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def valid_draft(request) -> SemanticTurnDraft:
    candidate = request.candidates[0]

    def grounded(text: str) -> GroundedSemanticText:
        return GroundedSemanticText(
            text=text,
            evidence_aliases=(candidate.alias,),
            supporting_excerpts=(SupportingExcerpt(
                alias=candidate.alias,
                excerpt=candidate.text,
            ),),
        )

    return SemanticTurnDraft(
        schema_version=1,
        outcome="PACKAGE_PROPOSAL",
        findings=(),
        package_delta=SemanticPackageDelta(
            item_title="Deadline-safe filtered export",
            business_requirement=grounded(
                "The filtered export uses the selected technical agenda evidence."
            ),
            technical_decision=grounded(
                "Implement the selected technical agenda decision exactly."
            ),
            acceptance_check=grounded(
                "The selected technical agenda behavior is verified exactly."
            ),
        ),
        control_intent=ControlIntent.NONE,
        edit_instruction=None,
        acknowledgement="Evidence analyzed",
        next_question="Should we confirm this package?",
        uncertainty=None,
    )


def invalid_alias_draft(request) -> SemanticTurnDraft:
    candidate = request.candidates[0]
    invalid = GroundedSemanticText(
        text="Unsupported alias content.",
        evidence_aliases=("technical-agenda:d-99",),
        supporting_excerpts=(SupportingExcerpt(
            alias="technical-agenda:d-99",
            excerpt=candidate.text,
        ),),
    )
    return SemanticTurnDraft(
        schema_version=1,
        outcome="PACKAGE_PROPOSAL",
        findings=(),
        package_delta=SemanticPackageDelta(
            item_title="Invalid alias package",
            business_requirement=invalid,
            technical_decision=invalid,
            acceptance_check=invalid,
        ),
        control_intent=ControlIntent.NONE,
        edit_instruction=None,
        acknowledgement="Evidence analyzed",
        next_question="Should we confirm this package?",
        uncertainty=None,
    )


def make_app(tmp_path: Path, analyzer):
    tmp_path.mkdir(parents=True, exist_ok=True)
    return create_app(
        settings=configured(tmp_path),
        clock=FrozenClock(NOW),
        source_catalog=SourceCatalog(ROOT),
        live_provider=object(),
        analyzer_provider=analyzer,
        grounding_checker=GroundingFixture(),
    )


def gate_for(
    app,
    analyzer,
    budget: FakeBudget,
    *,
    store=None,
    grounding=None,
    evidence_index=None,
    evidence_snapshots=None,
) -> WorkshopGate:
    return WorkshopGate(
        store or app.state.workshop_store,
        app.state.workflow,
        analyzer,
        grounding or GroundingFixture(),
        clock=FrozenClock(NOW),
        evidence_index=evidence_index or app.state.evidence_index,
        evidence_snapshots=evidence_snapshots or app.state.evidence_snapshots,
        business_context=app.state.analyzer_business_context,
        dev_lead_actor_id=app.state.bootstrap.dev_lead_actor_id,
        monotonic=budget.monotonic,
        sleep=budget.sleep,
    )


def commit_turn(app, *, sequence: int = 1) -> None:
    app.state.coordinator.commit_final_turn(
        DEMO_SESSION_ID,
        turn_sequence=sequence,
        text="Resolve D-02 timezone configuration for the filtered export.",
        provider_request_id=f"feature-23-final-{sequence}",
    )


def current_revision(app) -> int:
    return app.state.workflow.get_workflow_view(QueryOne(
        case_id=app.state.bootstrap.case_id,
        acting_actor_id=app.state.bootstrap.pm_actor_id,
    )).revision


def test_ar_ev_007_only_validated_retrieval_and_atomic_materialized_result_checkpoint(tmp_path):
    budget = FakeBudget()
    invalid = SequenceAnalyzer(
        AnalyzerProviderSchemaError("raw secret transcript must not persist"),
        AnalyzerProviderSchemaError("raw secret transcript must not persist"),
    )
    app = make_app(tmp_path, invalid)
    gate = gate_for(app, invalid, budget)
    commit_turn(app)
    before = current_revision(app)

    recovered = asyncio.run(gate.analyze_final_turn(DEMO_SESSION_ID, 1))

    assert recovered.complete_package_proposal is None
    checkpoint = app.state.workshop_store.latest_analyzer_checkpoint(DEMO_SESSION_ID)
    assert checkpoint.stage == AnalyzerCheckpointStage.RETRIEVAL_VALIDATED
    assert checkpoint.status == AnalyzerCheckpointStatus.RECOVERY
    assert checkpoint.failure_kind == AnalyzerFailureKind.PROVIDER_SCHEMA
    assert checkpoint.proposal_ref is None
    assert app.state.workshop_store.pending_proposal(DEMO_SESSION_ID) is None
    assert current_revision(app) == before

    successful = SequenceAnalyzer(valid_draft)
    gate.analyzer = successful
    result = asyncio.run(gate.analyze_final_turn(DEMO_SESSION_ID, 1))
    pending = app.state.workshop_store.pending_proposal(DEMO_SESSION_ID)
    checkpoint = app.state.workshop_store.latest_analyzer_checkpoint(DEMO_SESSION_ID)
    assert result.complete_package_proposal is not None
    assert pending is not None
    assert checkpoint.stage == AnalyzerCheckpointStage.PENDING_MATERIALIZED
    assert checkpoint.status == AnalyzerCheckpointStatus.VALIDATED
    assert checkpoint.proposal_ref == pending.proposal_ref
    assert AnalyzerTurnResult.model_validate_json(
        pending.analyzer_result_json
    ) == result


@pytest.mark.parametrize(
    ("case", "outcomes", "grounded", "locked", "failure", "action", "calls"),
    [
        (
            "availability",
            (
                AnalyzerProviderAvailabilityError("unavailable"),
                AnalyzerProviderAvailabilityError("unavailable"),
            ),
            True,
            False,
            AnalyzerFailureKind.PROVIDER_AVAILABILITY,
            AnalyzerRecoveryAction.REQUEUE,
            2,
        ),
        (
            "schema",
            (
                AnalyzerProviderSchemaError("invalid schema"),
                AnalyzerProviderSchemaError("invalid schema"),
            ),
            True,
            False,
            AnalyzerFailureKind.PROVIDER_SCHEMA,
            AnalyzerRecoveryAction.RETRY_TEXT,
            2,
        ),
        (
            "alias",
            (invalid_alias_draft, invalid_alias_draft),
            True,
            False,
            AnalyzerFailureKind.ALIAS_VALIDATION,
            AnalyzerRecoveryAction.CLARIFY,
            2,
        ),
        (
            "grounding",
            (valid_draft,),
            False,
            False,
            AnalyzerFailureKind.GROUNDING,
            AnalyzerRecoveryAction.CLARIFY,
            1,
        ),
        (
            "governance",
            (valid_draft,),
            True,
            True,
            AnalyzerFailureKind.GOVERNANCE,
            AnalyzerRecoveryAction.LOCK,
            0,
        ),
    ],
)
def test_ar_ev_008_failure_specific_recovery_never_expands_selected_evidence(
    tmp_path, case, outcomes, grounded, locked, failure, action, calls
):
    budget = FakeBudget()
    analyzer = SequenceAnalyzer(*outcomes)
    app = make_app(tmp_path, analyzer)
    gate = gate_for(
        app,
        analyzer,
        budget,
        grounding=GroundingFixture(grounded),
    )
    commit_turn(app)
    if locked:
        app.state.workshop_store.lock_session(
            DEMO_SESSION_ID, "TEST_REVISION_LOCK", NOW
        )
    before = current_revision(app)

    result = asyncio.run(gate.analyze_final_turn(DEMO_SESSION_ID, 1))

    assert result.complete_package_proposal is None, case
    assert len(analyzer.requests) == calls
    if calls == 2:
        assert analyzer.requests[0] == analyzer.requests[1]
        assert tuple(value.alias for value in analyzer.requests[0].candidates) == (
            "technical-agenda:d-02",
        )
    recovery = gate.latest_recovery(DEMO_SESSION_ID)
    assert recovery.failure_kind == failure
    assert recovery.recovery_action == action
    assert recovery.guidance == result.next_question
    assert app.state.workshop_store.pending_proposal(DEMO_SESSION_ID) is None
    assert current_revision(app) == before


def test_ar_ev_009_primary_recovery_provider_cutoff_and_hard_commit_guard(tmp_path, monkeypatch):
    primary_budget = FakeBudget()
    primary = SequenceAnalyzer((9.9, valid_draft), budget=primary_budget)
    primary_app = make_app(tmp_path / "primary", primary)
    primary_gate = gate_for(primary_app, primary, primary_budget)
    commit_turn(primary_app)
    primary_result = asyncio.run(
        primary_gate.analyze_final_turn(DEMO_SESSION_ID, 1)
    )
    assert primary_result.complete_package_proposal is not None
    assert len(primary.requests) == 1
    assert primary_budget.value == 9.9

    recovery_budget = FakeBudget()
    recovery = SequenceAnalyzer(
        (10.1, AnalyzerProviderAvailabilityError("transient")),
        valid_draft,
        budget=recovery_budget,
    )
    recovery_app = make_app(tmp_path / "recovery", recovery)
    recovery_gate = gate_for(recovery_app, recovery, recovery_budget)
    commit_turn(recovery_app)
    recovered = asyncio.run(
        recovery_gate.analyze_final_turn(DEMO_SESSION_ID, 1)
    )
    assert recovered.complete_package_proposal is not None
    assert len(recovery.requests) == 2
    assert recovery_budget.sleeps == [0.25]
    assert recovery_budget.value == 10.35

    cutoff_budget = FakeBudget()
    crossing = SequenceAnalyzer(
        (25.01, valid_draft),
        budget=cutoff_budget,
    )
    cutoff_app = make_app(tmp_path / "cutoff", crossing)
    cutoff_gate = gate_for(cutoff_app, crossing, cutoff_budget)
    commit_turn(cutoff_app)
    cutoff_result = asyncio.run(
        cutoff_gate.analyze_final_turn(DEMO_SESSION_ID, 1)
    )
    assert cutoff_result.complete_package_proposal is None
    assert len(crossing.requests) == 1
    assert cutoff_gate.latest_recovery(DEMO_SESSION_ID).failure_kind == (
        AnalyzerFailureKind.DEADLINE
    )
    assert cutoff_app.state.workshop_store.pending_proposal(DEMO_SESSION_ID) is None

    retry_cutoff_budget = FakeBudget()
    late_failure = SequenceAnalyzer(
        (24.9, AnalyzerProviderAvailabilityError("late")),
        valid_draft,
        budget=retry_cutoff_budget,
    )
    retry_app = make_app(tmp_path / "retry-cutoff", late_failure)
    retry_gate = gate_for(retry_app, late_failure, retry_cutoff_budget)
    commit_turn(retry_app)
    retry_result = asyncio.run(
        retry_gate.analyze_final_turn(DEMO_SESSION_ID, 1)
    )
    assert retry_result.complete_package_proposal is None
    assert len(late_failure.requests) == 1

    hard_budget = FakeBudget()
    hard = SequenceAnalyzer(valid_draft)
    hard_app = make_app(tmp_path / "hard", hard)
    hard_gate = gate_for(hard_app, hard, hard_budget)
    commit_turn(hard_app)
    original = hard_app.state.workshop_store.save_validated_proposal_checkpoint

    def cross_hard_deadline(*args, **kwargs):
        hard_budget.advance(30.0)
        return original(*args, **kwargs)

    monkeypatch.setattr(
        hard_app.state.workshop_store,
        "save_validated_proposal_checkpoint",
        cross_hard_deadline,
    )
    hard_result = asyncio.run(
        hard_gate.analyze_final_turn(DEMO_SESSION_ID, 1)
    )
    assert hard_result.complete_package_proposal is None
    assert hard_gate.latest_recovery(DEMO_SESSION_ID).failure_kind == (
        AnalyzerFailureKind.DEADLINE
    )
    assert hard_app.state.workshop_store.pending_proposal(DEMO_SESSION_ID) is None


def test_ar_ev_010_restart_reuses_retrieval_and_materialized_proposal_without_duplicate(
    tmp_path, monkeypatch
):
    budget = FakeBudget()
    first_analyzer = SequenceAnalyzer(valid_draft)
    app = make_app(tmp_path, first_analyzer)
    first_gate = gate_for(app, first_analyzer, budget)
    commit_turn(app)
    first = asyncio.run(first_gate.analyze_final_turn(DEMO_SESSION_ID, 1))
    first_pending = app.state.workshop_store.pending_proposal(DEMO_SESSION_ID)

    reopened = WorkshopStore(configured(tmp_path).workshop_database_url)
    resumed_analyzer = SequenceAnalyzer(valid_draft)
    resumed_gate = gate_for(
        app,
        resumed_analyzer,
        FakeBudget(),
        store=reopened,
    )

    def retrieval_must_not_repeat(*_args, **_kwargs):
        raise AssertionError("validated retrieval repeated after restart")

    monkeypatch.setattr(
        "specops_workshop.gate.DeterministicEvidenceRetriever.retrieve",
        retrieval_must_not_repeat,
    )
    resumed = asyncio.run(
        resumed_gate.analyze_final_turn(DEMO_SESSION_ID, 1)
    )
    assert resumed == first
    assert resumed_analyzer.requests == []
    assert reopened.pending_proposal(DEMO_SESSION_ID) == first_pending
    with reopened.engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(package_proposals)) == 1
    reopened.set_proposal_status(
        first_pending.proposal_ref, ProposalStatus.REJECTED, NOW
    )
    resolved_analyzer = SequenceAnalyzer(valid_draft)
    resolved_gate = gate_for(
        app,
        resolved_analyzer,
        FakeBudget(),
        store=reopened,
    )
    resolved = asyncio.run(
        resolved_gate.analyze_final_turn(DEMO_SESSION_ID, 1)
    )
    assert resolved.complete_package_proposal is None
    assert resolved.acknowledgement == "Proposal already resolved"
    assert resolved_analyzer.requests == []


def test_ar_ev_010_restart_from_retrieval_retries_provider_and_source_drift_recomputes(
    tmp_path, monkeypatch
):
    failing = SequenceAnalyzer(
        AnalyzerProviderAvailabilityError("offline"),
        AnalyzerProviderAvailabilityError("offline"),
    )
    app = make_app(tmp_path, failing)
    first_gate = gate_for(app, failing, FakeBudget())
    commit_turn(app)
    asyncio.run(first_gate.analyze_final_turn(DEMO_SESSION_ID, 1))
    failed_checkpoint = app.state.workshop_store.latest_analyzer_checkpoint(
        DEMO_SESSION_ID
    )
    assert len(failing.requests) == 2

    reopened = WorkshopStore(configured(tmp_path).workshop_database_url)
    resumed_analyzer = SequenceAnalyzer(valid_draft)
    resumed_gate = gate_for(
        app,
        resumed_analyzer,
        FakeBudget(),
        store=reopened,
    )
    retrieval_calls = 0
    original_retrieve = DeterministicEvidenceRetriever.retrieve

    def counted_retrieval(retriever, final_turn):
        nonlocal retrieval_calls
        retrieval_calls += 1
        return original_retrieve(retriever, final_turn)

    monkeypatch.setattr(
        DeterministicEvidenceRetriever, "retrieve", counted_retrieval
    )
    resumed = asyncio.run(
        resumed_gate.analyze_final_turn(DEMO_SESSION_ID, 1)
    )
    assert resumed.complete_package_proposal is not None
    assert len(resumed_analyzer.requests) == 1
    assert retrieval_calls == 0
    resumed_checkpoint = reopened.latest_analyzer_checkpoint(DEMO_SESSION_ID)
    assert resumed_checkpoint.request_id == failed_checkpoint.request_id
    # Three is cumulative across two user-visible sessions: 2 initial + 1 resumed.
    assert resumed_checkpoint.provider_call_count == 3

    drifted_snapshots = tuple(
        value.model_copy(update={"version": 2})
        for value in app.state.evidence_snapshots
    )
    drifted_index = EvidenceIndex(
        case_id=app.state.evidence_index.case_id,
        units=app.state.evidence_index.units,
        bindings=tuple(
            value.model_copy(update={"version": 2})
            for value in app.state.evidence_index.bindings
        ),
        source_seals=tuple(
            value.model_copy(update={"version": 2})
            for value in app.state.evidence_index.source_seals
        ),
    )
    drift_analyzer = SequenceAnalyzer(valid_draft)
    drift_gate = gate_for(
        app,
        drift_analyzer,
        FakeBudget(),
        store=reopened,
        evidence_index=drifted_index,
        evidence_snapshots=drifted_snapshots,
    )
    drifted = asyncio.run(
        drift_gate.analyze_final_turn(DEMO_SESSION_ID, 1)
    )
    assert drifted.complete_package_proposal is not None
    assert len(drift_analyzer.requests) == 1
    assert retrieval_calls == 1
    assert reopened.latest_analyzer_checkpoint(DEMO_SESSION_ID).request_id != (
        resumed_checkpoint.request_id
    )


def test_ar_ev_011_checkpoint_receipts_are_content_free_and_boundary_closed(tmp_path):
    analyzer = SequenceAnalyzer(
        AnalyzerProviderAvailabilityError(
            "secret source transcript semantic claim raw audio jira github"
        ),
        AnalyzerProviderAvailabilityError(
            "secret source transcript semantic claim raw audio jira github"
        ),
    )
    app = make_app(tmp_path, analyzer)
    gate = gate_for(app, analyzer, FakeBudget())
    commit_turn(app)
    asyncio.run(gate.analyze_final_turn(DEMO_SESSION_ID, 1))

    with app.state.workshop_store.engine.connect() as connection:
        row = dict(connection.execute(select(analyzer_checkpoints)).mappings().one())
    assert set(row) == {
        "request_id", "session_id", "turn_sequence", "request_fingerprint",
        "source_fingerprint", "retrieval_fingerprint", "selected_aliases_json",
        "stage", "status", "failure_kind", "recovery_action", "proposal_ref",
        "provider_call_count", "created_at", "updated_at",
    }
    serialized = json.dumps(row, sort_keys=True).casefold()
    selected_text = app.state.evidence_index.unit_for(
        "technical-agenda:d-02"
    ).text.casefold()
    assert selected_text not in serialized
    assert "resolve d-02 timezone" not in serialized
    assert "secret source transcript" not in serialized
    assert "semantic claim" not in serialized
    assert "raw audio" not in serialized
    assert "gemini-feature-23-fixture" not in serialized
    assert "openai-feature-23-fixture" not in serialized

    source_root = Path(__file__).parents[2] / "src/specops_workshop"
    provider_neutral = "\n".join(
        (source_root / name).read_text(encoding="utf-8").casefold()
        for name in ("analyzer.py", "analyzer_runtime.py", "gate.py")
    )
    assert all(
        forbidden not in provider_neutral
        for forbidden in (
            "from openai", "import openai", "api_key", "jira_", "github_",
            "atlassian", "api.github.com",
        )
    )


def test_ar_ev_012_one_call_no_provider_cache_visible_fallback_and_local_250ms(tmp_path):
    class Responses:
        def __init__(self) -> None:
            self.calls = []

        async def create(self, **kwargs):
            self.calls.append(kwargs)
            request_payload = json.loads(
                kwargs["input"][0]["content"][1]["text"].partition("\n")[2]
            )
            request = type("RequestEvidence", (), {
                "candidates": [type("Candidate", (), value)()
                               for value in request_payload["candidates"]]
            })()
            return type("Response", (), {
                "output_text": valid_draft(request).model_dump_json(),
                "_request_id": "feature23-provider-fixture",
            })()

    client = type("Client", (), {"responses": Responses()})()
    provider = TerraResponsesProvider(
        api_key="server-only-feature-23",
        model=TERRA_MODEL,
        client=client,
    )
    app = make_app(tmp_path, provider)
    gate = gate_for(app, provider, FakeBudget())
    app.state.gate = gate
    commit_turn(app)

    started = time.perf_counter()
    result = asyncio.run(gate.analyze_final_turn(DEMO_SESSION_ID, 1))
    local_fixture_ms = (time.perf_counter() - started) * 1000

    assert result.complete_package_proposal is not None
    assert len(client.responses.calls) == 1
    call = client.responses.calls[0]
    assert call["reasoning"] == {"effort": "medium"}
    assert call["store"] is False
    assert "prompt_cache_key" not in call
    assert "prompt_cache_options" not in call
    assert local_fixture_ms <= 250

    failing = SequenceAnalyzer(
        AnalyzerProviderAvailabilityError("offline"),
        AnalyzerProviderAvailabilityError("offline"),
    )
    fallback_app = make_app(tmp_path / "fallback", failing)
    fallback_gate = gate_for(fallback_app, failing, FakeBudget())
    fallback_app.state.gate = fallback_gate
    commit_turn(fallback_app)
    asyncio.run(fallback_gate.analyze_final_turn(DEMO_SESSION_ID, 1))
    with TestClient(fallback_app) as client_view:
        visible = client_view.get("/api/analyzer/recovery")
    assert visible.status_code == 200
    assert visible.json()["failure_kind"] == "PROVIDER_AVAILABILITY"
    assert visible.json()["recovery_action"] == "REQUEUE"
    assert visible.json()["guidance"].endswith("?")
