from __future__ import annotations

import asyncio
import json
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from specops_workflow.models import QueryOne
from specops_workshop.analyzer import AnalyzerTurnResult, ControlIntent, ControlTarget
from specops_workshop.api import DEMO_SESSION_ID, create_app
from specops_workshop.config import Settings
from specops_workshop.contracts import ProviderResumeContext
from specops_workshop.demo import LIVE_PM_TURN
from specops_workshop.ports import VoiceContext, VoiceEventType
from specops_workshop.providers.gemini_live import GeminiLiveProvider
from specops_workshop.providers.openai_responses import TerraResponsesProvider
from specops_workshop.sources import SourceCatalog


ROOT = Path("/Users/rudinro/Desktop/SpecOps/Spec_Eng")
BACKEND = Path("/Users/rudinro/Desktop/SpecOps/backend")
RECEIPT = BACKEND / "tests/workshop/live-evidence.json"


class RecordingAnalyzer:
    def __init__(self, provider: TerraResponsesProvider) -> None:
        self.provider = provider
        self.request_ids: list[str] = []

    async def analyze(self, request):
        result = await self.provider.analyze(request)
        diagnostic = self.provider.request_diagnostics[-1]
        self.request_ids.append(
            diagnostic.provider_request_id or diagnostic.client_request_id
        )
        return result


class LiveTerraAnalysisError(RuntimeError):
    def __init__(self, cause: Exception, provider: TerraResponsesProvider) -> None:
        super().__init__("TERRA_ANALYSIS_FAILURE")
        self.request_diagnostics = provider.request_diagnostics
        message = str(cause)
        if message.startswith("analyzer proposal is not grounded in its cited evidence"):
            _, _, detail = message.partition(": ")
            self.validation_error_code = (
                "GROUNDING_" + detail if detail else "GROUNDING_REJECTED"
            )
        elif message == "analyzer turn SourceRef mismatch":
            self.validation_error_code = "TURN_SOURCE_REF_MISMATCH"
        else:
            self.validation_error_code = "LOCAL_VALIDATION_REJECTED"


async def run() -> dict[str, object]:
    loaded = Settings.load({
        "SPECOPS_DATABASE_URL": "sqlite:///:memory:",
        "WORKSHOP_DATABASE_URL": "sqlite:///:memory:",
    }, ROOT / ".env")
    with tempfile.TemporaryDirectory(prefix="specops-live-") as directory:
        runtime = Path(directory)
        settings = loaded.model_copy(update={
            "specops_database_url": f"sqlite:///{runtime / 'foundation.sqlite'}",
            "workshop_database_url": f"sqlite:///{runtime / 'workshop.sqlite'}",
        })
        try:
            app = create_app(settings=settings, source_catalog=SourceCatalog(ROOT))
        except Exception as exc:
            raise RuntimeError("LIVE_BOOTSTRAP_FAILURE") from exc
        assert isinstance(app.state.live_provider, GeminiLiveProvider)
        assert isinstance(app.state.gate.analyzer, TerraResponsesProvider)
        terra = RecordingAnalyzer(app.state.gate.analyzer)
        app.state.gate.analyzer = terra

        committed = app.state.coordinator.commit_final_turn(
            DEMO_SESSION_ID,
            turn_sequence=1,
            text=LIVE_PM_TURN,
            provider_request_id="live-pm-final-1",
        )
        snapshot = app.state.workshop_store.latest_snapshots(DEMO_SESSION_ID)[0]
        recovered = app.state.coordinator.recover(DEMO_SESSION_ID)
        try:
            voice = await app.state.live_provider.connect(VoiceContext(
                system_instruction=(
                    "You are the SpecOps Workshop facilitator. Acknowledge the PM's governed CSV Export "
                    "decision in a brief spoken response. Do not discuss Jira or GitHub actions."
                ),
                resume=ProviderResumeContext(
                    conversation_phase=recovered.session.conversation_phase,
                    committed_package=None,
                    downstream_handoff=None,
                    final_transcript_snapshots=(snapshot,),
                ),
            ))
        except Exception as exc:
            raise RuntimeError("GEMINI_CONNECT_FAILURE") from exc
        audio_bytes = 0
        gemini_request_ids: set[str] = set()
        try:
            try:
                await voice.send_text(
                    "Acknowledge that the PM decision was received, using one brief spoken sentence."
                )
            except Exception as exc:
                raise RuntimeError("GEMINI_SEND_FAILURE") from exc

            async def receive_audio() -> None:
                nonlocal audio_bytes
                async for event in voice.events():
                    if event.provider_request_id:
                        gemini_request_ids.add(event.provider_request_id)
                    if event.type == VoiceEventType.AUDIO and event.audio:
                        audio_bytes += len(event.audio)
                        if audio_bytes > 0:
                            return

            try:
                await asyncio.wait_for(receive_audio(), timeout=45)
            except Exception as exc:
                raise RuntimeError("GEMINI_AUDIO_FAILURE") from exc
        finally:
            await voice.close()
        if audio_bytes <= 0:
            raise RuntimeError("GEMINI_AUDIO_MISSING")

        try:
            analyzed = await app.state.gate.analyze_final_turn(DEMO_SESSION_ID, 1)
        except Exception as exc:
            current: BaseException | None = exc
            while current is not None:
                if isinstance(current, TimeoutError):
                    raise RuntimeError("TERRA_TIMEOUT") from exc
                current = current.__cause__
            raise LiveTerraAnalysisError(exc, terra.provider) from exc
        if analyzed.complete_package_proposal is None:
            raise RuntimeError("TERRA_PACKAGE_PROPOSAL_MISSING")
        technical_id = app.state.bootstrap.technical_source_id
        technical_findings = [
            finding for finding in analyzed.finding_proposals
            if any(ref.artifact_id == technical_id for ref in finding.evidence_refs)
        ]
        if not technical_findings:
            raise RuntimeError("TERRA_OPEN_DECISION_POINTER_MISSING")
        pending = app.state.workshop_store.pending_proposal(DEMO_SESSION_ID)
        if pending is None:
            raise RuntimeError("TERRA_PENDING_PROPOSAL_MISSING")
        confirmed = app.state.gate.apply_control(
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
        workflow = app.state.workflow.get_workflow_view(QueryOne(
            case_id=app.state.bootstrap.case_id,
            acting_actor_id=app.state.bootstrap.pm_actor_id,
        ))
        if workflow.current_package is None or confirmed.status.value != "COMMITTED":
            raise RuntimeError("EXPLICIT_CONFIRMATION_NOT_COMMITTED")
        pointer = technical_findings[0].evidence_refs[0]
        return {
            "schema_version": 1,
            "sw_ev": "SW-EV-024",
            "status": "PASS",
            "completed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "gemini_model": settings.gemini_model,
            "terra_model": settings.terra_model,
            "gemini_provider": type(app.state.live_provider).__name__,
            "terra_provider": type(terra.provider).__name__,
            "gemini_request_ids": sorted(gemini_request_ids),
            "terra_request_ids": terra.request_ids,
            "terra_provider_diagnostics": [
                value.as_receipt() for value in terra.provider.request_diagnostics
            ],
            "gemini_audio_bytes": audio_bytes,
            "decision_source_pointer": pointer.model_dump(mode="json"),
            "foundation_revision": workflow.revision,
            "package_binding": workflow.current_package.model_dump(mode="json"),
            "initial_transcript_revision": committed.receipt.revision,
        }


def main() -> int:
    try:
        receipt = asyncio.run(run())
    except Exception as exc:
        causes = []
        status_codes = []
        provider_codes = []
        provider_params = []
        schema_details = []
        terra_failure_stages = []
        terra_request_diagnostics = []
        openai_request_ids = []
        openai_client_request_ids = []
        validation_error_codes = []
        current: BaseException | None = exc
        while current is not None and len(causes) < 6:
            causes.append(type(current).__name__)
            status = getattr(current, "status_code", None)
            if isinstance(status, int) and status not in status_codes:
                status_codes.append(status)
            stage = getattr(current, "stage", None)
            if stage in {"CACHE_WARM", "ANALYSIS"} and stage not in terra_failure_stages:
                terra_failure_stages.append(stage)
            diagnostics = getattr(current, "request_diagnostics", ())
            for diagnostic in diagnostics:
                as_receipt = getattr(diagnostic, "as_receipt", None)
                if callable(as_receipt):
                    value = as_receipt()
                    if value not in terra_request_diagnostics:
                        terra_request_diagnostics.append(value)
                    diagnostic_status = value.get("status_code")
                    if isinstance(diagnostic_status, int) and diagnostic_status not in status_codes:
                        status_codes.append(diagnostic_status)
                    diagnostic_provider_id = value.get("provider_request_id")
                    if (
                        isinstance(diagnostic_provider_id, str)
                        and diagnostic_provider_id not in openai_request_ids
                    ):
                        openai_request_ids.append(diagnostic_provider_id)
                    diagnostic_client_id = value.get("client_request_id")
                    if (
                        isinstance(diagnostic_client_id, str)
                        and diagnostic_client_id not in openai_client_request_ids
                    ):
                        openai_client_request_ids.append(diagnostic_client_id)
            provider_request_id = getattr(current, "provider_request_id", None)
            sdk_request_id = getattr(current, "request_id", None)
            client_request_id = getattr(current, "client_request_id", None)
            validation_error_code = getattr(current, "validation_error_code", None)
            if (
                isinstance(validation_error_code, str)
                and validation_error_code.isascii()
                and validation_error_code.replace("_", "").isalnum()
                and validation_error_code not in validation_error_codes
            ):
                validation_error_codes.append(validation_error_code)
            for candidate in (provider_request_id, sdk_request_id):
                if (
                    isinstance(candidate, str)
                    and candidate.isascii()
                    and 0 < len(candidate) <= 512
                    and all(character.isalnum() or character in "._-" for character in candidate)
                    and candidate not in openai_request_ids
                ):
                    openai_request_ids.append(candidate)
            if (
                isinstance(client_request_id, str)
                and client_request_id.isascii()
                and 0 < len(client_request_id) <= 512
                and all(character.isalnum() or character in "._-" for character in client_request_id)
                and client_request_id not in openai_client_request_ids
            ):
                openai_client_request_ids.append(client_request_id)
            code = getattr(current, "code", None)
            if isinstance(code, str) and code.replace("_", "").replace("-", "").isalnum():
                provider_codes.append(code[:64])
            param = getattr(current, "param", None)
            if isinstance(param, str) and all(character.isalnum() or character in "._-" for character in param):
                provider_params.append(param[:64])
            if code == "invalid_json_schema":
                detail = str(getattr(current, "message", ""))
                detail = re.sub(r"[0-9a-f]{64}", "[HASH]", detail, flags=re.IGNORECASE)
                detail = re.sub(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", "[UUID]", detail, flags=re.IGNORECASE)
                detail = re.sub(r"/(?:Users|private)/[^\s'\"]+", "[PATH]", detail)
                if detail and len(detail) <= 600:
                    schema_details.append(detail)
            current = current.__cause__
        receipt = {
            "schema_version": 1,
            "sw_ev": "SW-EV-024",
            "status": "FAIL",
            "completed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "error_type": type(exc).__name__,
            "error_code": str(exc) if str(exc).isupper() and len(str(exc)) <= 64 else "PROVIDER_OR_CONTRACT_FAILURE",
            "cause_types": causes,
            "provider_status_codes": status_codes,
            "provider_error_codes": provider_codes,
            "provider_error_params": provider_params,
            "schema_error_details": schema_details,
            "terra_failure_stage": terra_failure_stages[0] if terra_failure_stages else None,
            "terra_request_diagnostics": terra_request_diagnostics,
            "openai_request_ids": openai_request_ids,
            "openai_client_request_ids": openai_client_request_ids,
            "validation_error_codes": validation_error_codes,
        }
    RECEIPT.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        key: value for key, value in receipt.items()
        if key not in {"decision_source_pointer", "package_binding"}
    }, sort_keys=True))
    return 0 if receipt["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
