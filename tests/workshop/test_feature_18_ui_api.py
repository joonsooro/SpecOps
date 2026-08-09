from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from specops_workflow import FrozenClock
from specops_workflow.enums import Domain
from specops_workshop.analyzer import (
    AcceptanceCheckProposal,
    AnalyzerTurnResult,
    CompletePackageProposal,
    ControlIntent,
    RequirementProposal,
    SpecPackageItemProposal,
    TechnicalDecisionProposal,
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
        ref = request.final_turn.final_source_ref
        package = CompletePackageProposal(
            proposal_key="csv-package",
            existing_package_id=None,
            requirements=[RequirementProposal(
                proposal_key="all-rows", existing_unit_id=None,
                statement="Export every filtered order", domain=Domain.BUSINESS,
                delivery_required=True, source_refs=[ref],
            )],
            technical_decisions=[TechnicalDecisionProposal(
                proposal_key="async-mode", existing_unit_id=None,
                statement="Generate large exports asynchronously", domain=Domain.TECHNICAL,
                delivery_required=True, source_refs=[ref],
            )],
            acceptance_checks=[AcceptanceCheckProposal(
                proposal_key="complete-check", existing_check_id=None,
                statement="No filtered order is truncated", domain=Domain.CROSS_DOMAIN,
                related_unit_proposal_keys=["all-rows", "async-mode"], source_refs=[ref],
            )],
            items=[SpecPackageItemProposal(
                proposal_key="complete-export", existing_item_id=None,
                title="Complete filtered export", requirement_proposal_keys=["all-rows"],
                technical_decision_proposal_keys=["async-mode"],
                acceptance_check_proposal_keys=["complete-check"], dependency_item_proposal_keys=[],
            )],
        )
        return AnalyzerTurnResult(
            schema_version=1, turn_source_ref=ref, finding_proposals=[],
            complete_package_proposal=package, control_intent=ControlIntent.NONE,
            control_target=None, target_proposal_ref=None, edit_instruction=None,
            acknowledgement="Drafted grounded package",
            next_question="Should this package be committed?",
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
            "text": "Keep every filtered row and generate large exports asynchronously.",
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
