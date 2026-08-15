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
SPEC_ENG = next(
    parent for parent in Path(__file__).resolve().parents if parent.name == "Spec_Eng"
)
_temporary = tempfile.TemporaryDirectory(prefix="specops-task26-browser-")


def _bind_matching_planned_identities(payload: dict, identity_plan) -> dict:
    """Use the first available server-owned identity of each required kind."""

    available: dict[str, list] = {}
    for planned in identity_plan.planned_identities:
        available.setdefault(planned.entity_kind, []).append(planned.foundation_id)
    mapping = {}
    for original, kind in WorkshopFoundationService._artifact_identity_kinds(payload).items():
        try:
            mapping[original] = available[kind].pop(0)
        except (KeyError, IndexError) as exc:
            raise AssertionError(f"identity plan lacks required {kind} capacity") from exc
    return bind_planned_identities(payload, mapping)


def _provider_owned_spec_payload(payload: dict, identity_plan) -> dict:
    """Mirror the production schema: Foundation owns exact server records."""

    # Keep the generated actor just long enough to bind every actor_ref to the
    # Foundation-allocated ACTOR slot. No unconfirmed decision may be emitted.
    payload["decisions"] = []
    payload["evidence_catalog"] = []
    payload["semantic_evidence_findings"] = []
    payload = _bind_matching_planned_identities(payload, identity_plan)
    bind_fixture_references(payload)
    payload.pop("actors")
    payload.pop("decisions")
    payload.pop("evidence_catalog")
    payload.pop("semantic_evidence_findings")

    def clear_server_owned_refs(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"decision_refs", "source_evidence_refs", "evidence_refs"}:
                    value[key] = []
                else:
                    clear_server_owned_refs(item)
        elif isinstance(value, list):
            for item in value:
                clear_server_owned_refs(item)

    clear_server_owned_refs(payload)
    return payload


class BrowserAnalyzerAdapter(DeterministicAdapter):
    async def execute(self, request, *, context):
        self.operations.append(request.request_type)
        if isinstance(request, c.SpecPackageSynthesisRequest):
            payload = PayloadFactory(full_identity_plan=True).payload(
                "spec-package-payload.schema.json"
            )
            payload = _provider_owned_spec_payload(payload, request.identity_plan)
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
            payload = _bind_matching_planned_identities(payload, request.identity_plan)
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
