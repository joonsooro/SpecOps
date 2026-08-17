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
from specops_workflow.spec_identity_materialization import (
    SPEC_ANALYZER_COLLECTIONS,
    spec_collection_slot_assignments,
)

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
    """Mirror production: local handles carry semantics; Foundation owns IDs."""

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

    def project_provider_owned_refs(value):
        if isinstance(value, dict):
            for key, item in tuple(value.items()):
                if key in {"source_evidence_refs", "evidence_refs"}:
                    value.pop(key)
                elif key == "decision_refs":
                    value[key] = []
                else:
                    project_provider_owned_refs(item)
        elif isinstance(value, list):
            for item in value:
                project_provider_owned_refs(item)

    project_provider_owned_refs(payload)

    slot_assignments = spec_collection_slot_assignments(identity_plan.slots)
    kind_offsets: dict[str, int] = {}
    selected_by_path = {}
    identity_to_handle = {}
    for path, kind in SPEC_ANALYZER_COLLECTIONS:
        collection = payload
        for part in path:
            collection = collection[part]
        start = kind_offsets.get(kind, 0)
        selected = slot_assignments[path][start : start + len(collection)]
        kind_offsets[kind] = start + len(collection)
        if len(selected) != len(collection):
            raise AssertionError(f"identity plan lacks local {kind} capacity")
        selected_by_path[path] = selected
        identity_to_handle.update(
            (item["id"], slot.slot_key)
            for item, slot in zip(collection, selected, strict=True)
        )

    def rewrite_dict_scalars(value):
        if isinstance(value, dict):
            for child_key, child in tuple(value.items()):
                if child_key != "id" and isinstance(child, str):
                    value[child_key] = identity_to_handle.get(child, child)
                else:
                    rewrite_dict_scalars(child)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                if isinstance(child, str):
                    value[index] = identity_to_handle.get(child, child)
                else:
                    rewrite_dict_scalars(child)

    rewrite_dict_scalars(payload)
    for path, _kind in SPEC_ANALYZER_COLLECTIONS:
        parent = payload
        for part in path[:-1]:
            parent = parent[part]
        collection = parent[path[-1]]
        slots = slot_assignments[path]
        keyed = {slot.slot_key: None for slot in slots}
        for item, slot in zip(collection, selected_by_path[path], strict=True):
            body = dict(item)
            body.pop("id")
            keyed[slot.slot_key] = body
        parent[path[-1]] = keyed
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
