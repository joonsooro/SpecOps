"""Offline real-FastAPI browser fixture for the Task 26 deterministic gate."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import uvicorn
from pydantic import SecretStr

from specops_contracts import workshop_v1 as c
from specops_workshop.api import create_app
from specops_workshop.config import Settings
from specops_workshop.sources import SourceCatalog
from specops_workflow.workshop_protocol import WorkshopFoundationService

from test_feature_26_v4_production_seam import DeterministicAdapter
from v4_payload_factory import (
    PayloadFactory,
    bind_fixture_references,
    bind_planned_identities,
)
from v4_quality_factory import DeterministicQualityEvaluator


BACKEND = Path(__file__).resolve().parents[2]
SPEC_ENG = BACKEND.parent / "Spec_Eng"
_temporary = tempfile.TemporaryDirectory(prefix="specops-task26-browser-")


class BrowserAnalyzerAdapter(DeterministicAdapter):
    async def execute(self, request, *, context):
        self.operations.append(request.request_type)
        if isinstance(request, c.SpecPackageSynthesisRequest):
            payload = PayloadFactory(full_identity_plan=True).payload(
                "spec-package-payload.schema.json"
            )
            identities = WorkshopFoundationService._artifact_identity_kinds(payload)
            payload = bind_planned_identities(
                payload,
                {
                    original: planned.foundation_id
                    for original, planned in zip(
                        identities, request.identity_plan.planned_identities, strict=True
                    )
                },
            )
            bind_fixture_references(payload)
            source_evidence = request.foundation_snapshot.evidence[0]
            evidence = payload["evidence_catalog"][0]
            evidence.update(
                source_id=str(context.source_set.ordered_sources[0].source.source_id),
                source_hash=source_evidence.source_hash,
                excerpt_hash=source_evidence.excerpt_hash,
                claim_refs=[payload["requirements"][0]["id"]],
            )
            finding = payload["semantic_evidence_findings"][0]
            finding.update(
                claim_ref=payload["requirements"][0]["id"],
                evidence_ref=evidence["id"],
                source_hash=source_evidence.source_hash,
                excerpt_hash=source_evidence.excerpt_hash,
                analyzer_run_id=str(request.analyzer_run_id),
            )
            for decision in payload["decisions"]:
                decision["status"] = "deferred"
                decision["confirmation_binding"] = None
            return c.SpecPackageSynthesisCandidate(
                output_type="SPEC_PACKAGE_SYNTHESIS_CANDIDATE",
                analyzer_run_id=request.analyzer_run_id,
                context_id=request.context_id,
                request_hash=request.request_hash,
                source_set_hash=request.source_set_hash,
                based_on_case_revision=request.based_on_case_revision,
                foundation_artifact_id=request.target.foundation_artifact_id,
                identity_plan_id=request.identity_plan.identity_plan_id,
                identity_plan_version=request.identity_plan.identity_plan_version,
                semantic_state_hash=request.identity_plan.semantic_state_hash,
                candidate_payload_json=json.dumps(
                    payload, separators=(",", ":"), sort_keys=True
                ),
                payload_schema_id=request.payload_schema_id,
                payload_schema_version=request.payload_schema_version,
            )
        if isinstance(request, c.TechnicalContractSynthesisRequest):
            payload = PayloadFactory(full_identity_plan=True).payload(
                "technical-contract-payload.schema.json"
            )
            identities = WorkshopFoundationService._artifact_identity_kinds(payload)
            payload = bind_planned_identities(
                payload,
                {
                    original: planned.foundation_id
                    for original, planned in zip(
                        identities, request.identity_plan.planned_identities, strict=True
                    )
                },
            )
            confirmed_spec = json.loads(request.confirmed_spec.canonical_payload_json)
            bind_fixture_references(payload, confirmed_spec)
            source_evidence = request.foundation_snapshot.evidence[0]
            evidence = payload["evidence_catalog"][0]
            evidence.update(
                source_id=str(context.source_set.ordered_sources[0].source.source_id),
                source_hash=source_evidence.source_hash,
                excerpt_hash=source_evidence.excerpt_hash,
                claim_refs=[payload["components"][0]["id"]],
            )
            finding = payload["semantic_evidence_findings"][0]
            finding.update(
                claim_ref=payload["components"][0]["id"],
                evidence_ref=evidence["id"],
                source_hash=source_evidence.source_hash,
                excerpt_hash=source_evidence.excerpt_hash,
                analyzer_run_id=str(request.analyzer_run_id),
            )
            return c.TechnicalContractSynthesisCandidate(
                output_type="TECHNICAL_CONTRACT_SYNTHESIS_CANDIDATE",
                analyzer_run_id=request.analyzer_run_id,
                context_id=request.context_id,
                request_hash=request.request_hash,
                source_set_hash=request.source_set_hash,
                based_on_case_revision=request.based_on_case_revision,
                foundation_artifact_id=request.target.foundation_artifact_id,
                identity_plan_id=request.identity_plan.identity_plan_id,
                identity_plan_version=request.identity_plan.identity_plan_version,
                semantic_state_hash=request.identity_plan.semantic_state_hash,
                candidate_payload_json=json.dumps(
                    payload, separators=(",", ":"), sort_keys=True
                ),
                payload_schema_id=request.payload_schema_id,
                payload_schema_version=request.payload_schema_version,
            )
        return await super().execute(request, context=context)


app = create_app(
    settings=Settings(
        specops_database_url=f"sqlite:///{_temporary.name}/foundation.sqlite",
        workshop_database_url=f"sqlite:///{_temporary.name}/workshop.sqlite",
        gemini_api_key=SecretStr("offline-not-called"),
        openai_api_key=SecretStr("offline-not-called"),
    ),
    source_catalog=SourceCatalog(SPEC_ENG),
    analyzer_adapter=BrowserAnalyzerAdapter(),
    quality_evaluator=DeterministicQualityEvaluator(),
    live_provider=object(),
)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
