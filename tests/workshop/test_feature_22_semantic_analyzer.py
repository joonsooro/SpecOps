from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from specops_workflow import FrozenClock
from specops_workflow.models import LineRange, QueryOne, SourceRef
from specops_workshop.analyzer import (
    AnalyzerTurnResult,
    CommittedSemanticContext,
    ControlIntent,
    ControlTarget,
    GroundedSemanticText,
    SemanticAnalyzerRequest,
    SemanticPackageDelta,
    SemanticTurnDraft,
    SupportingExcerpt,
)
from specops_workshop.api import DEMO_SESSION_ID, create_app
from specops_workshop.config import GEMINI_MODEL, TERRA_MODEL, Settings
from specops_workshop.contracts import ProposalStatus
from specops_workshop.contracts import AnalyzerFailureKind
from specops_workshop.gate import WorkshopGate
from specops_workshop.sources import SourceCatalog, SourceName


ROOT = Path("/Users/rudinro/Desktop/SpecOps/Spec_Eng")
NOW = datetime(2026, 8, 9, 22, tzinfo=timezone.utc)


def configured(tmp_path: Path) -> Settings:
    return Settings.load({
        "SPECOPS_DATABASE_URL": f"sqlite:///{tmp_path / 'foundation.sqlite'}",
        "WORKSHOP_DATABASE_URL": f"sqlite:///{tmp_path / 'workshop.sqlite'}",
        "GEMINI_API_KEY": "gemini-feature-22-fixture",
        "OPENAI_API_KEY": "openai-feature-22-fixture",
        "GEMINI_LIVE_MODEL": GEMINI_MODEL,
        "OPENAI_ANALYZER_MODEL": TERRA_MODEL,
    })


class AlwaysGrounded:
    def supports(self, _claim, _refs):
        return True


class SemanticFixture:
    def __init__(self, factory=None):
        self.factory = factory or valid_draft
        self.requests = []

    async def analyze(self, request):
        self.requests.append(request)
        return self.factory(request)


def grounded(candidate, text: str, *, alias: str | None = None, excerpt: str | None = None):
    selected_alias = candidate.alias if alias is None else alias
    return GroundedSemanticText(
        text=text,
        evidence_aliases=(selected_alias,),
        supporting_excerpts=(SupportingExcerpt(
            alias=selected_alias,
            excerpt=candidate.text if excerpt is None else excerpt,
        ),),
    )


def valid_draft(request: SemanticAnalyzerRequest) -> SemanticTurnDraft:
    candidate = request.candidates[0]
    return SemanticTurnDraft(
        schema_version=1,
        outcome="PACKAGE_PROPOSAL",
        findings=(),
        package_delta=SemanticPackageDelta(
            item_title="Exactly grounded filtered export",
            business_requirement=grounded(
                candidate, "The export follows the selected agenda evidence."
            ),
            technical_decision=grounded(
                candidate, "Implement the selected technical agenda decision."
            ),
            acceptance_check=grounded(
                candidate, "The selected agenda behavior is verified exactly."
            ),
        ),
        control_intent=ControlIntent.NONE,
        edit_instruction=None,
        acknowledgement="Evidence analyzed",
        next_question="Should we confirm this package?",
        uncertainty=None,
    )


def make_app(tmp_path: Path, analyzer: SemanticFixture):
    return create_app(
        settings=configured(tmp_path),
        clock=FrozenClock(NOW),
        source_catalog=SourceCatalog(ROOT),
        live_provider=object(),
        analyzer_provider=analyzer,
        grounding_checker=AlwaysGrounded(),
    )


def workflow_view(app):
    return app.state.workflow.get_workflow_view(QueryOne(
        case_id=app.state.bootstrap.case_id,
        acting_actor_id=app.state.bootstrap.pm_actor_id,
    ))


def direct_gate(app, analyzer, *, snapshots=None):
    return WorkshopGate(
        app.state.workshop_store,
        app.state.workflow,
        analyzer,
        AlwaysGrounded(),
        clock=FrozenClock(NOW),
        evidence_index=app.state.evidence_index,
        evidence_snapshots=(
            app.state.evidence_snapshots if snapshots is None else snapshots
        ),
        business_context=app.state.analyzer_business_context,
        dev_lead_actor_id=app.state.bootstrap.dev_lead_actor_id,
    )


def all_source_refs(result: AnalyzerTurnResult) -> tuple[SourceRef, ...]:
    package = result.complete_package_proposal
    assert package is not None
    return tuple(
        ref
        for value in (
            *package.requirements,
            *package.technical_decisions,
            *package.acceptance_checks,
        )
        for ref in value.source_refs
    )


@pytest.mark.parametrize(
    ("turn", "alias", "line"),
    [
        ("Resolve D-01 before sign-off.", "technical-agenda:d-01", 527),
        ("Resolve D-02 timezone configuration.", "technical-agenda:d-02", 528),
        (
            "Resolve D-05 currency exponent rounding.",
            "technical-agenda:d-05",
            531,
        ),
    ],
)
def test_ar_ev_004_one_semantic_call_materializes_exact_local_source_ref(
    tmp_path, turn, alias, line
):
    analyzer = SemanticFixture()
    app = make_app(tmp_path, analyzer)
    app.state.coordinator.commit_final_turn(
        DEMO_SESSION_ID,
        turn_sequence=1,
        text=turn,
        provider_request_id=f"feature-22-{line}",
    )
    before = workflow_view(app)

    result = asyncio.run(app.state.gate.analyze_final_turn(DEMO_SESSION_ID, 1))

    assert len(analyzer.requests) == 1
    request = analyzer.requests[0]
    assert tuple(value.alias for value in request.candidates) == (alias,)
    assert request.effort == "medium"
    binding = next(
        value for value in app.state.evidence_index.bindings if value.alias == alias
    )
    expected = SourceRef(
        artifact_id=binding.artifact_id,
        version=binding.version,
        content_hash=binding.content_hash,
        location=LineRange(start=line, end=line),
    )
    assert all_source_refs(result) == (expected, expected, expected)
    assert workflow_view(app).revision == before.revision
    assert workflow_view(app).current_package is None
    pending = app.state.workshop_store.pending_proposal(DEMO_SESSION_ID)
    assert pending is not None and pending.status == ProposalStatus.PENDING


def test_ar_ev_004_request_and_response_schemas_are_alias_only_and_identity_free(tmp_path):
    analyzer = SemanticFixture()
    app = make_app(tmp_path, analyzer)
    app.state.coordinator.commit_final_turn(
        DEMO_SESSION_ID,
        turn_sequence=1,
        text="Resolve D-02 timezone configuration.",
        provider_request_id="feature-22-schema",
    )
    asyncio.run(app.state.gate.analyze_final_turn(DEMO_SESSION_ID, 1))
    request = analyzer.requests[0]
    request_payload = request.model_dump(mode="json")
    request_schema = SemanticAnalyzerRequest.model_json_schema()
    response_schema = SemanticTurnDraft.model_json_schema()
    forbidden = {
        "actor_id", "artifact_id", "binding", "case_id", "command_id",
        "content_hash", "item_id", "json_pointer", "line", "line_range",
        "location", "package_id", "proposal_id", "raw_location", "range",
        "session_id", "source_ref", "source_version", "unit_id", "uuid",
        "version", "workbook_location",
    }

    def keys(value):
        if isinstance(value, dict):
            yield from value
            for child in value.values():
                yield from keys(child)
        elif isinstance(value, list):
            for child in value:
                yield from keys(child)

    assert forbidden.isdisjoint(keys(request_payload))
    assert forbidden.isdisjoint(keys(request_schema))
    assert forbidden.isdisjoint(keys(response_schema))
    serialized = json.dumps(request_payload, sort_keys=True)
    for authoritative in (
        str(app.state.bootstrap.case_id),
        str(app.state.bootstrap.technical_source_id),
        app.state.evidence_snapshots[0].content_hash,
    ):
        assert authoritative not in serialized
    pm_document = SourceCatalog(ROOT).read_text(SourceName.PM_SPEC)
    assert request.business_context != pm_document
    assert "Business objective:" in request.business_context
    assert pm_document not in serialized
    selected = app.state.evidence_index.unit_for("technical-agenda:d-02")
    unselected = app.state.evidence_index.unit_for("technical-agenda:d-01")
    assert request.candidates[0].text == selected.text
    assert unselected.text not in serialized
    assert "filtered-orders-csv-export-technical-contract.md" not in serialized


def test_ar_ev_004_ambiguous_retrieval_returns_safe_question_without_provider_call(tmp_path):
    analyzer = SemanticFixture()
    app = make_app(tmp_path, analyzer)
    app.state.coordinator.commit_final_turn(
        DEMO_SESSION_ID,
        turn_sequence=1,
        text="Should we settle the date or timezone decision?",
        provider_request_id="feature-22-ambiguous",
    )
    before = workflow_view(app)

    result = asyncio.run(app.state.gate.analyze_final_turn(DEMO_SESSION_ID, 1))

    assert analyzer.requests == []
    assert result.complete_package_proposal is None
    assert result.finding_proposals == []
    assert result.control_intent == ControlIntent.NONE
    assert result.next_question is not None
    assert len(result.next_question.split()) <= 25
    assert app.state.workshop_store.pending_proposal(DEMO_SESSION_ID) is None
    assert workflow_view(app).revision == before.revision
    assert workflow_view(app).current_package is None


@pytest.mark.parametrize(
    "failure",
    ["unknown", "duplicate", "unselected", "unsupported-excerpt", "malformed"],
)
def test_ar_ev_005_invalid_provider_semantics_fail_whole_result_before_proposal(
    tmp_path, failure
):
    app_holder = {}

    def invalid(request):
        candidate = request.candidates[0]
        if failure == "malformed":
            return object()
        if failure == "unselected":
            alternate = app_holder["app"].state.evidence_index.unit_for(
                "technical-agenda:d-01"
            )
            value = grounded(
                candidate,
                "Unsupported unselected decision.",
                alias=alternate.alias,
                excerpt=alternate.text,
            )
        elif failure == "unknown":
            value = grounded(
                candidate,
                "Unsupported unknown decision.",
                alias="technical-agenda:d-99",
                excerpt="unknown evidence",
            )
        elif failure == "unsupported-excerpt":
            value = grounded(
                candidate,
                "Unsupported excerpt.",
                excerpt="text that is not present in the selected evidence",
            )
        else:
            value = GroundedSemanticText.model_construct(
                text="Duplicated evidence alias.",
                evidence_aliases=(candidate.alias, candidate.alias),
                supporting_excerpts=(
                    SupportingExcerpt(alias=candidate.alias, excerpt=candidate.text),
                    SupportingExcerpt(alias=candidate.alias, excerpt=candidate.text),
                ),
            )
        delta = SemanticPackageDelta.model_construct(
            item_title="Invalid package",
            business_requirement=value,
            technical_decision=value,
            acceptance_check=value,
        )
        return SemanticTurnDraft.model_construct(
            schema_version=1,
            findings=(),
            package_delta=delta,
            control_intent=ControlIntent.NONE,
            edit_instruction=None,
            acknowledgement="Evidence analyzed",
            next_question="Should we confirm this package?",
            uncertainty=None,
        )

    analyzer = SemanticFixture(invalid)
    app = make_app(tmp_path, analyzer)
    app_holder["app"] = app
    app.state.coordinator.commit_final_turn(
        DEMO_SESSION_ID,
        turn_sequence=1,
        text="Resolve D-02 timezone configuration.",
        provider_request_id=f"feature-22-invalid-{failure}",
    )
    before = workflow_view(app)

    result = asyncio.run(app.state.gate.analyze_final_turn(DEMO_SESSION_ID, 1))

    assert len(analyzer.requests) == 2
    assert result.complete_package_proposal is None
    assert result.finding_proposals == []
    assert app.state.gate.latest_recovery(DEMO_SESSION_ID).failure_kind in {
        AnalyzerFailureKind.ALIAS_VALIDATION,
        AnalyzerFailureKind.PROVIDER_SCHEMA,
    }
    assert app.state.workshop_store.pending_proposal(DEMO_SESSION_ID) is None
    assert workflow_view(app).revision == before.revision
    assert workflow_view(app).current_package is None


@pytest.mark.parametrize("failure", ["stale", "cross-case"])
def test_ar_ev_005_stale_or_cross_case_snapshot_fails_before_proposal(
    tmp_path, failure
):
    analyzer = SemanticFixture()
    app = make_app(tmp_path, analyzer)
    current = app.state.evidence_snapshots[0]
    changed = current.model_copy(update={
        "version": current.version + 1,
    }) if failure == "stale" else current.model_copy(update={"case_id": uuid4()})
    gate = direct_gate(app, analyzer, snapshots=(changed,))
    app.state.coordinator.commit_final_turn(
        DEMO_SESSION_ID,
        turn_sequence=1,
        text="Resolve D-02 timezone configuration.",
        provider_request_id=f"feature-22-{failure}",
    )
    before = workflow_view(app)

    result = asyncio.run(gate.analyze_final_turn(DEMO_SESSION_ID, 1))

    assert len(analyzer.requests) == 0
    assert result.complete_package_proposal is None
    assert app.state.workshop_store.pending_proposal(DEMO_SESSION_ID) is None
    assert workflow_view(app).revision == before.revision


def test_ar_ev_006_local_assembler_retains_existing_identities_without_egress(tmp_path):
    analyzer = SemanticFixture()
    app = make_app(tmp_path, analyzer)
    gate = app.state.gate
    app.state.coordinator.commit_final_turn(
        DEMO_SESSION_ID,
        turn_sequence=1,
        text="Resolve D-02 timezone configuration.",
        provider_request_id="feature-22-initial",
    )
    first = asyncio.run(gate.analyze_final_turn(DEMO_SESSION_ID, 1))
    pending = app.state.workshop_store.pending_proposal(DEMO_SESSION_ID)
    snapshot = app.state.workshop_store.latest_snapshots(DEMO_SESSION_ID)[0]
    gate.apply_control(
        DEMO_SESSION_ID,
        AnalyzerTurnResult(
            schema_version=1,
            turn_source_ref=snapshot.final_source_ref,
            finding_proposals=[],
            complete_package_proposal=None,
            control_intent=ControlIntent.CONFIRM,
            control_target=ControlTarget.WORKSHOP_PATCH,
            target_proposal_ref=pending.proposal_ref,
            edit_instruction=None,
            acknowledgement="Confirmed",
            next_question=None,
        ),
        confirmation_context=True,
    )
    content = app.state.workflow.get_spec_package_content(QueryOne(
        case_id=app.state.bootstrap.case_id,
        acting_actor_id=app.state.bootstrap.pm_actor_id,
    ))
    app.state.coordinator.commit_final_turn(
        DEMO_SESSION_ID,
        turn_sequence=2,
        text="Revisit D-02 timezone configuration.",
        provider_request_id="feature-22-revision",
    )
    committed_revision = workflow_view(app).revision

    second = asyncio.run(gate.analyze_final_turn(DEMO_SESSION_ID, 2))
    package = second.complete_package_proposal
    assert package is not None and first.complete_package_proposal is not None
    assert package.existing_package_id == content.package_binding.artifact_id
    assert package.requirements[0].existing_unit_id == content.payload.requirements[0].unit_id
    assert package.technical_decisions[0].existing_unit_id == (
        content.payload.technical_decisions[0].unit_id
    )
    assert package.acceptance_checks[0].existing_check_id == (
        content.payload.acceptance_checks[0].check_id
    )
    assert package.items[0].existing_item_id == content.payload.items[0].item_id
    provider_payload = json.dumps(analyzer.requests[1].model_dump(mode="json"))
    for identity in (
        content.package_binding.artifact_id,
        content.payload.requirements[0].unit_id,
        content.payload.technical_decisions[0].unit_id,
        content.payload.acceptance_checks[0].check_id,
        content.payload.items[0].item_id,
    ):
        assert str(identity) not in provider_payload
    assert workflow_view(app).revision == committed_revision


def test_feature_22_provider_neutral_core_has_no_transport_or_credentials():
    source_root = Path(__file__).parents[2] / "src/specops_workshop"
    core = "\n".join(
        (source_root / name).read_text(encoding="utf-8").casefold()
        for name in ("analyzer.py", "gate.py")
    )
    assert all(
        forbidden not in core
        for forbidden in (
            "from openai", "import openai", "api_key", "credential",
            "http://", "https://", "jira", "github",
        )
    )
