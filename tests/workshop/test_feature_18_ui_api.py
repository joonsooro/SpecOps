from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from specops_workflow import FrozenClock
from specops_workshop.analyzer import (
    ControlIntent,
    GroundedSemanticText,
    SemanticPackageDelta,
    SemanticTurnDraft,
    SupportingExcerpt,
)
from specops_workshop.api import create_app
from specops_workshop.config import GEMINI_MODEL, TERRA_MODEL, Settings
from specops_workshop.sources import SourceCatalog


ROOT = Path("/Users/rudinro/Desktop/SpecOps/Spec_Eng")
NOW = datetime(2026, 8, 9, 14, tzinfo=timezone.utc)


def configured(tmp_path):
    return Settings.load({
        "SPECOPS_DATABASE_URL": f"sqlite:///{tmp_path / 'foundation.sqlite'}",
        "WORKSHOP_DATABASE_URL": f"sqlite:///{tmp_path / 'workshop.sqlite'}",
        "GEMINI_API_KEY": "gemini-browser-secret",
        "OPENAI_API_KEY": "openai-browser-secret",
        "GEMINI_LIVE_MODEL": GEMINI_MODEL,
        "OPENAI_ANALYZER_MODEL": TERRA_MODEL,
    })


class Grounded:
    def supports(self, _claim, _refs): return True


class ProposalAnalyzer:
    async def analyze(self, request):
        candidate = request.candidates[0]
        def grounded(text):
            return GroundedSemanticText(
                text=text,
                evidence_aliases=(candidate.alias,),
                supporting_excerpts=(SupportingExcerpt(
                    alias=candidate.alias, excerpt=candidate.text
                ),),
            )
        return SemanticTurnDraft(
            schema_version=1,
            outcome="PACKAGE_PROPOSAL",
            findings=(),
            package_delta=SemanticPackageDelta(
                item_title="Complete filtered export",
                business_requirement=grounded("Use the organization timezone."),
                technical_decision=grounded("Use an IANA timezone with UTC fallback."),
                acceptance_check=grounded("Timezone boundaries and rendering stay exact."),
            ),
            control_intent=ControlIntent.NONE,
            edit_instruction=None,
            acknowledgement="Drafted grounded package",
            next_question="Should this package be committed?",
            uncertainty=None,
        )


def test_workshop_projection_and_accessible_control_use_server_state_only(tmp_path):
    app = create_app(
        settings=configured(tmp_path), clock=FrozenClock(NOW),
        source_catalog=SourceCatalog(ROOT), live_provider=object(),
        analyzer_provider=ProposalAnalyzer(), grounding_checker=Grounded(),
    )
    with TestClient(app) as client:
        turn = client.post("/api/session/final-turn", json={
            "turn_sequence": 1,
            "text": "Resolve D-02 timezone configuration for the export.",
            "provider_request_id": "browser-final-1",
            "correction_of_version": None,
        })
        assert turn.status_code == 200
        projection = client.get("/api/workshop")
        assert projection.status_code == 200
        value = projection.json()
        pending = value["pending_proposal"]
        assert pending["record"]["status"] == "PENDING"
        assert pending["result"]["complete_package_proposal"]["items"][0]["title"] == "Complete filtered export"
        serialized = projection.text
        assert "gemini-browser-secret" not in serialized
        assert "openai-browser-secret" not in serialized

        confirmed = client.post("/api/proposals/control", json={
            "intent": "CONFIRM",
            "proposal_ref": pending["record"]["proposal_ref"],
            "edit_instruction": None,
            "acknowledgement": "Confirmed",
        })
        assert confirmed.status_code == 200
        assert confirmed.json() == {"status": "COMMITTED"}
        committed = client.get("/api/workshop").json()
        assert committed["pending_proposal"] is None
        assert committed["governance"]["items"][0]["readiness"] == "READY"


def test_proposal_control_rejects_missing_and_unknown_browser_fields(tmp_path):
    app = create_app(
        settings=configured(tmp_path), clock=FrozenClock(NOW),
        source_catalog=SourceCatalog(ROOT), live_provider=object(),
    )
    with TestClient(app) as client:
        assert client.post("/api/proposals/control", json={"intent": "CONFIRM"}).status_code == 422
        assert client.post("/api/proposals/control", json={
            "intent": "REJECT", "proposal_ref": "patch-1", "edit_instruction": None,
            "acknowledgement": None, "platform": "forbidden",
        }).status_code == 422


def test_real_browser_latency_json_reaches_the_strict_server_contract(tmp_path):
    app = create_app(
        settings=configured(tmp_path), clock=FrozenClock(NOW),
        source_catalog=SourceCatalog(ROOT), live_provider=object(),
    )
    with TestClient(app) as client:
        response = client.post("/api/telemetry/spans", json={
            "span_id": str(uuid4()),
            "stage": "BROWSER",
            "duration_ms": 42,
            "outcome": "OK",
        })
        assert response.status_code == 200, response.json()
        assert response.json()["stage"] == "BROWSER"

        rejected = client.post("/api/telemetry/spans", json={
            "span_id": str(uuid4()),
            "stage": "ANALYZER",
            "duration_ms": 42,
            "outcome": "OK",
        })
        assert rejected.status_code == 422


def test_v4_runtime_binds_the_exact_two_approved_full_source_documents(tmp_path):
    app = create_app(
        settings=configured(tmp_path), clock=FrozenClock(NOW),
        source_catalog=SourceCatalog(ROOT), live_provider=object(),
    )
    sources = app.state.v4_source_uploads
    assert [item.source.role for item in sources] == ["PM_SPEC", "TECHNICAL_CONTRACT"]
    assert [item.source.filename for item in sources] == [
        "PM_Specs.md",
        "filtered-orders-csv-export-technical-contract.md",
    ]
    assert sources[0].content == (ROOT / "docs/PM_Specs.md").read_bytes()
    assert sources[1].content == (
        ROOT / "docs/technical-specs/filtered-orders-csv-export-technical-contract.md"
    ).read_bytes()
