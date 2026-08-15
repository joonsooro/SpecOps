from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import IntegrityError

from specops_contracts import workshop_v1 as c
from specops_contracts import artifact_quality_v1 as q
from specops_contracts.canonical import domain_hash, payload_hash, transcript_hash
from specops_workflow import WorkflowService, migrate
from specops_workflow.enums import SourceArtifactType
from specops_workflow.models import CreateCaseCommand, RegisterSourceArtifactCommand, SourceArtifactIdentity
from specops_workflow.persistence import (
    ARTIFACT_QUALITY_TABLES,
    EVIDENCE_ASSESSMENT_TABLES,
    WORKSHOP_PROTOCOL_TABLES,
    audit_events,
    cases,
    engine_for,
)
from specops_workflow.workshop_protocol import FoundationProtocolError, WorkshopFoundationService
from specops_workflow.artifact_projection import draft_governance
from specops_workflow.artifact_evidence_support import (
    EvidenceSupportProposal,
    prepare_evidence_support_request,
)
from specops_workshop.v4.api import install_workshop_protocol_api
from specops_workshop.v4.artifact_quality import build_audit_bundle, quality_contract_hash
from specops_workshop.v4.openai_adapter import ProviderSourceUpload
from v4_payload_factory import PayloadFactory, bind_planned_identities
from v4_quality_factory import (
    admit_all_pass_quality,
    all_pass_candidate,
    stable_id as quality_stable_id,
)


NOW = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
CASE_ID = UUID("10000000-0000-4000-8000-000000000001")
SESSION_ID = UUID("10000000-0000-4000-8000-000000000002")
PM_ID = UUID("10000000-0000-4000-8000-000000000003")
DEV_ID = UUID("10000000-0000-4000-8000-000000000004")
CONTEXT_ID = UUID("10000000-0000-4000-8000-000000000005")
RUN_ID = UUID("10000000-0000-4000-8000-000000000006")
PM_SOURCE_BYTES = b"Export filtered orders.\n"
TECHNICAL_SOURCE_BYTES = b"\n" * 530 + b"CSV output uses UTF-8 and one header row.\n"
PM_SOURCE_HASH = "sha256:" + hashlib.sha256(PM_SOURCE_BYTES).hexdigest()
TECHNICAL_SOURCE_HASH = "sha256:" + hashlib.sha256(TECHNICAL_SOURCE_BYTES).hexdigest()
SOURCE_SET_HASH = domain_hash(
    "SPECOPS:SOURCE_SET:v1",
    [
        {
            "source_id": "10000000-0000-4000-8000-000000000005",
            "role": "PM_SPEC",
            "version": 1,
            "payload_hash": PM_SOURCE_HASH,
        },
        {
            "source_id": "10000000-0000-4000-8000-000000000006",
            "role": "TECHNICAL_CONTRACT",
            "version": 1,
            "payload_hash": TECHNICAL_SOURCE_HASH,
        },
    ],
)
REQUEST_HASH = "sha256:" + "2" * 64


def _contract() -> c.AnalyzerContractBinding:
    return c.AnalyzerContractBinding(
        protocol_version="1.0.0",
        instruction_set_id="specops-workshop-analyzer",
        instruction_set_version=1,
        instruction_set_hash="sha256:" + "3" * 64,
        semantic_quality_contract_id="SEMANTIC-QUALITY-CONTRACT",
        semantic_quality_contract_version="2.2.0",
        semantic_quality_contract_hash=quality_contract_hash(),
        provider_schema_version="1.0.0",
        model="gpt-5.6-terra",
        reasoning_effort="medium",
    )


def _source_set(locators: tuple[str, str], hashes: tuple[str, str]) -> c.SourceSetBinding:
    return c.SourceSetBinding(
        source_set_hash=SOURCE_SET_HASH,
        ordered_sources=tuple(
            c.ProviderSourceBinding(
                source=c.SourceIdentity(
                    source_id=UUID(f"10000000-0000-4000-8000-{index:012d}"),
                    role=role,
                    version=1,
                    payload_hash="sha256:" + hashes[index - 5],
                    canonical_locator=locators[index - 5],
                    filename=f"{index}.md",
                    media_type="text/markdown",
                ),
                provider_file_id=f"file_{index}",
            )
            for index, role in (
                (5, c.SourceRole.PM_SPEC),
                (6, c.SourceRole.TECHNICAL_CONTRACT),
            )
        ),
    )


def _base(revision: int, *, actor="SYSTEM", idempotency=None) -> dict:
    return {
        "protocol_version": "1.0.0",
        "command_id": uuid4(),
        "case_id": CASE_ID,
        "session_id": SESSION_ID,
        "correlation_id": uuid4(),
        "causation_id": None,
        "idempotency_key": idempotency or f"idem-{uuid4()}",
        "issued_at": NOW,
        "acting_actor_id": actor,
        "expected_case_revision": revision,
    }


def _transcript_command(revision: int, sequence: int, text: str, actor=PM_ID):
    event_id = uuid4()
    values = _base(revision)
    values.update(
        command_type="RECORD_FINAL_TRANSCRIPT",
        transcript=c.TranscriptFinalizedEvent(
            protocol_version="1.0.0",
            event_type="TRANSCRIPT_FINALIZED",
            event_id=event_id,
            case_id=CASE_ID,
            session_id=SESSION_ID,
            correlation_id=values["correlation_id"],
            causation_id=None,
            event_sequence=sequence,
            observed_case_revision=revision,
            occurred_at=NOW,
            producer="VOICE",
            turn_id=uuid4(),
            transcript_artifact_id=uuid4(),
            transcript_version=1,
            transcript_hash=transcript_hash(text),
            actor=c.TranscriptActor.PM,
            speaker_actor_id=actor,
            speaker_attribution_method=c.SpeakerAttributionMethod.VERBAL_SELF_ASSERTION,
            sequence_number=sequence,
            text=text,
        ),
    )
    return c.RecordFinalTranscriptCommand(**values)


def _runtime(tmp_path):
    url = f"sqlite:///{tmp_path / 'foundation.sqlite'}"
    migrate(url)
    legacy = WorkflowService(database_url=url)
    legacy.create_case(
        CreateCaseCommand(
            command_id=uuid4(),
            case_id=CASE_ID,
            acting_actor_id=PM_ID,
            pm_actor_id=PM_ID,
            dev_lead_actor_id=DEV_ID,
        )
    )
    pm_path = tmp_path / "pm-spec.md"
    technical_path = tmp_path / "technical-contract.md"
    pm_path.write_bytes(PM_SOURCE_BYTES)
    technical_path.write_bytes(TECHNICAL_SOURCE_BYTES)
    locators = (str(pm_path), str(technical_path))
    hashes = tuple(hashlib.sha256(Path(value).read_bytes()).hexdigest() for value in locators)
    revision = 1
    for index, (locator, digest, source_type) in enumerate(
        zip(
            locators,
            hashes,
            (SourceArtifactType.BUSINESS_SPEC, SourceArtifactType.TECHNICAL_CONTRACT),
        ),
        start=5,
    ):
        result = legacy.register_source_artifact(
            RegisterSourceArtifactCommand(
                command_id=uuid4(),
                case_id=CASE_ID,
                acting_actor_id="SYSTEM",
                expected_case_revision=revision,
                identity=SourceArtifactIdentity(
                    artifact_id=UUID(f"10000000-0000-4000-8000-{index:012d}"),
                    case_id=CASE_ID,
                    type=source_type,
                    version=1,
                    media_type="text/markdown",
                    canonical_locator=locator,
                    content_hash=digest,
                ),
            )
        )
        revision = result.receipt.revision
    foundation = WorkshopFoundationService(url, now=lambda: NOW)
    foundation.test_source_set = _source_set(locators, hashes)
    foundation.register_case(
        case_id=CASE_ID,
        session_id=SESSION_ID,
        source_set_hash=SOURCE_SET_HASH,
    )
    return url, foundation


def _activate(foundation):
    values = _base(3)
    values.update(
        command_type="ACTIVATE_ANALYZER_CONTEXT",
        context=c.AnalyzerContextBinding(
            protocol_version="1.0.0",
            context_id=CONTEXT_ID,
            session_id=SESSION_ID,
            provider=c.ProviderName.OPENAI,
            provider_conversation_id="conv_test",
            bootstrap_response_id="resp_test",
            model="gpt-5.6-terra",
            reasoning_effort=c.ReasoningEffort.MEDIUM,
            conversation_state_persisted=True,
            response_store_enabled=True,
            analyzer_contract=_contract(),
            source_set=foundation.test_source_set,
            status=c.ContextStatus.ACTIVE,
            created_at=NOW,
            invalidated_at=None,
            invalidation_reason=None,
        ),
    )
    return foundation.execute(c.ActivateAnalyzerContextCommand(**values))


def _quality_bundle(foundation, artifact_type: str):
    record = foundation.latest_artifact_record(CASE_ID, artifact_type)
    sources = tuple(
        ProviderSourceUpload(
            source=item.source,
            content=Path(item.source.canonical_locator).read_bytes(),
        )
        for item in foundation.test_source_set.ordered_sources
    )
    bundle = build_audit_bundle(
        audit_id=uuid4(),
        evaluator_run_id=uuid4(),
        case_id=CASE_ID,
        session_id=SESSION_ID,
        based_on_case_revision=foundation.case_revision(CASE_ID),
        artifact_record=record,
        sources=sources,
        transcripts=foundation.final_transcripts(CASE_ID),
        semantic_snapshot=foundation.semantic_snapshot(CASE_ID),
        semantic_quality_contract_hash=_contract().semantic_quality_contract_hash,
        confirmed_spec=(
            foundation.confirmed_spec_binding(CASE_ID)
            if artifact_type == "TECHNICAL_CONTRACT"
            else None
        ),
    )
    return bundle


def _admit_quality(foundation, artifact_type: str):
    return admit_all_pass_quality(
        foundation, _quality_bundle(foundation, artifact_type)
    )


def _insert_minimal_spec_artifact(foundation):
    artifact_id = uuid4()
    payload = PayloadFactory().payload("spec-package-payload.schema.json")
    _bind_fixture_refs(payload)
    digest = payload_hash(payload)
    governance = draft_governance(
        artifact_type="SPEC_PACKAGE",
        payload=payload,
        target=SimpleNamespace(next_artifact_version=1),
        quality_hash=quality_contract_hash(),
        now=NOW,
        new_id=uuid4,
    )
    with foundation.engine.begin() as connection:
        connection.execute(
            insert(WORKSHOP_PROTOCOL_TABLES["workshop_artifact_records"]).values(
                artifact_id=str(artifact_id),
                artifact_version=1,
                case_id=str(CASE_ID),
                artifact_type="SPEC_PACKAGE",
                artifact_key="SPEC-QUALITY-NEGATIVE",
                record_revision=1,
                payload_hash=digest,
                payload_json=json.dumps(payload, separators=(",", ":"), sort_keys=True),
                governance_json=json.dumps(
                    governance, separators=(",", ":"), sort_keys=True
                ),
                status="DRAFT",
                confirmed_from_json=None,
                created_at=NOW.isoformat(),
            )
        )
    return artifact_id


def _prepare_quality_candidate(foundation, bundle):
    foundation.prepare_artifact_quality_audit(
        q.PrepareArtifactQualityAuditCommand(
            protocol_version=q.PROTOCOL_VERSION,
            command_id=quality_stable_id(bundle.audit_id, "prepare-negative"),
            idempotency_key=f"quality-negative-prepare-{bundle.audit_id}",
            expected_case_revision=bundle.based_on_case_revision,
            bundle=bundle,
        )
    )
    foundation.bind_artifact_quality_evaluator(
        q.BindArtifactQualityEvaluatorCommand(
            protocol_version=q.PROTOCOL_VERSION,
            command_id=quality_stable_id(bundle.audit_id, "bind-negative"),
            audit_id=bundle.audit_id,
            request_hash=bundle.request_hash,
            provider="OPENAI",
            model="gpt-5.6-terra",
            reasoning_effort="medium",
            provider_conversation_id=f"conv_negative_{str(bundle.audit_id)[:12]}",
            client_request_id=f"aqa-negative-{bundle.request_hash[7:23]}",
            started_at=NOW,
        )
    )


def _admit_quality_candidate(foundation, bundle, candidate):
    return foundation.admit_artifact_quality_audit(
        q.AdmitArtifactQualityAuditCommand(
            protocol_version=q.PROTOCOL_VERSION,
            command_id=quality_stable_id(bundle.audit_id, "admit-negative"),
            idempotency_key=f"quality-negative-admit-{bundle.audit_id}",
            expected_case_revision=bundle.based_on_case_revision,
            bundle=bundle,
            execution=q.EvaluatorExecutionBinding(
                provider="OPENAI",
                model="gpt-5.6-terra",
                reasoning_effort="medium",
                provider_conversation_id=f"conv_negative_{str(bundle.audit_id)[:12]}",
                provider_response_id=f"resp_negative_{str(bundle.audit_id)[:12]}",
                client_request_id=f"aqa-negative-response-{bundle.request_hash[7:23]}",
                store_enabled=True,
                started_at=NOW,
                completed_at=NOW,
            ),
            candidate=candidate,
        )
    )


def _bind_fixture_refs(payload: dict, confirmed_spec_payload: dict | None = None) -> None:
    by_group = {
        "requirement_refs": payload.get("requirements", []),
        "decision_refs": payload.get("decisions", []),
        "acceptance_check_refs": payload.get("acceptance_checks", []),
        "dependency_refs": payload.get("dependencies", []),
        "constraint_refs": payload.get("constraints", []),
        "risk_refs": payload.get("risks", []),
        "open_item_refs": payload.get("open_items", []),
        "scenario_refs": payload.get("scenarios", []),
        "outcome_refs": payload.get("outcomes", []),
        "actor_refs": payload.get("actors", []),
    }
    actor_id = next((item["id"] for item in payload.get("actors", [])), None)
    owned_ids = list(WorkshopFoundationService._artifact_identity_kinds(payload))
    spec_ids = (
        list(WorkshopFoundationService._artifact_identity_kinds(confirmed_spec_payload))
        if confirmed_spec_payload is not None
        else []
    )

    def bind(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in by_group:
                    group = by_group[key]
                    value[key] = (
                        [entry["id"] for entry in group][: max(1, len(item))]
                        if item and group
                        else []
                    )
                elif key in {
                    "primary_customer", "beneficiary_actor_ref", "primary_actor_ref", "actor_ref"
                } and actor_id is not None:
                    value[key] = actor_id
                elif key == "implements_spec_refs" and spec_ids:
                    value[key] = spec_ids[: max(1, len(item))] if item else []
                elif key in {"from_ref", "trigger_interface_ref", "failure_ref", "component_ref"} and owned_ids:
                    value[key] = owned_ids[0]
                elif key == "to_ref" and owned_ids:
                    value[key] = owned_ids[min(1, len(owned_ids) - 1)]
                else:
                    bind(item)
        elif isinstance(value, list):
            for item in value:
                bind(item)

    bind(payload)
    for requirement in payload.get("requirements", []):
        if not requirement["source_evidence_refs"] and not requirement["decision_refs"]:
            requirement["priority"] = "should"
    if confirmed_spec_payload is not None:
        promise = confirmed_spec_payload["product_thesis"]["customer_promise"]
        for value in payload["contract_intent"].values():
            if isinstance(value, dict) and "spec_json_pointer" in value:
                value["spec_json_pointer"] = "/product_thesis/customer_promise"
                value["value"] = promise


def _turn_candidate(transcript_id: UUID, revision: int) -> c.TurnAnalysisCandidate:
    evidence = c.CandidateEntityRef(ref_kind="CANDIDATE_KEY", candidate_key="evidence-encoding")
    problem = c.CandidateEntityRef(ref_kind="CANDIDATE_KEY", candidate_key="problem-encoding")
    decisions = tuple(
        c.DecisionCandidate(
            candidate_key=f"decision-{suffix}",
            existing_decision_ref=None,
            classification=c.Domain.PRODUCT,
            statement=statement,
            rationale="This closes one explicit export decision.",
            alternatives_considered=(),
            problem_links=(
                c.ProblemResolutionLink(problem_ref=problem, resolution_kind=c.ProblemResolutionKind.FULL),
            ),
            evidence_refs=(evidence,),
            requires_human_confirmation=True,
        )
        for suffix, statement in (
            ("encoding", "Use UTF-8 for every CSV export."),
            ("header", "Emit exactly one header row."),
        )
    )
    return c.TurnAnalysisCandidate(
        protocol_version="1.0.0",
        output_type="TURN_ANALYSIS_CANDIDATE",
        analyzer_run_id=RUN_ID,
        context_id=CONTEXT_ID,
        request_hash=REQUEST_HASH,
        source_set_hash=SOURCE_SET_HASH,
        transcript_event_id=transcript_id,
        based_on_case_revision=revision,
        disposition=c.TurnDisposition.SUBSTANTIVE,
        no_change_reason_code=None,
        evidence_candidates=(
            c.EvidenceCandidate(
                candidate_key="evidence-encoding",
                source_role=c.SourceRole.TECHNICAL_CONTRACT,
                locator=c.SourceLineLocator(locator_kind=c.SourceLocatorKind.SOURCE_LINES, start_line=531, end_line=531),
                relevance_claim="The source fixes export encoding and headers.",
                quoted_text_candidate="CSV output uses UTF-8 and one header row.",
            ),
        ),
        new_problems=(
            c.TurnProblemCandidate(
                candidate_key="problem-encoding",
                problem_kind=c.ProblemKind.MISSING_DECISION,
                domain=c.Domain.PRODUCT,
                severity=c.Severity.HIGH,
                statement="The export encoding and header policy need confirmation.",
                consequence="Consumers could parse files inconsistently.",
                evidence_refs=(evidence,),
            ),
        ),
        new_problem_clusters=(),
        revised_problem_clusters=(),
        new_questions=(),
        revised_questions=(),
        low_risk_facts=(),
        decisions=decisions,
        problem_assessments=(),
        evidence_findings=(
            c.SemanticEvidenceFindingCandidate(
                candidate_key="finding-encoding",
                claim_ref=problem,
                evidence_ref=evidence,
                assessment=c.EvidenceAssessment.SUPPORTS,
                confidence=0.99,
                explanation="The exact source line supports the export decision context.",
            ),
        ),
    )


def test_foundation_mixed_batch_is_atomic_audited_replayable_and_restart_safe(tmp_path):
    url, foundation = _runtime(tmp_path)
    _activate(foundation)

    turn = _transcript_command(4, 1, "Confirm UTF-8 and one header row.")
    foundation.execute(turn)
    candidate = _turn_candidate(turn.transcript.event_id, 5)
    admit_values = _base(5)
    admit_values.update(
        command_type="ADMIT_TURN_ANALYSIS",
        analyzer_run_id=RUN_ID,
        context_id=CONTEXT_ID,
        provider_request_hash=REQUEST_HASH,
        candidate=candidate,
    )
    admitted = foundation.execute(c.AdmitTurnAnalysisCommand(**admit_values))
    decision_ids = tuple(
        item.foundation_id for item in admitted.identity_mappings if item.entity_kind == "DECISION"
    )
    review_values = _base(6)
    review_values.update(
        command_type="MATERIALIZE_DECISION_BATCH_REVIEW",
        pending_decision_ids=decision_ids,
        derived_from_cluster_ids=(),
    )
    review_receipt = foundation.execute(c.MaterializeDecisionBatchReviewCommand(**review_values))
    review = foundation.current_decision_view(CASE_ID)
    assert review is not None
    assert review.view_hash == review_receipt.view_hash
    assert [item.handle for item in review.items] == ["A", "B"]

    confirmation = _transcript_command(7, 2, "Confirm A and revise B.")
    foundation.execute(confirmation)
    selections = (
        c.VoiceConfirmationSelectionItemCandidate(handle="A", action=c.ConfirmationAction.CONFIRM, revision_span=None),
        c.VoiceConfirmationSelectionItemCandidate(
            handle="B",
            action=c.ConfirmationAction.REVISE,
            revision_span=c.TranscriptSpan(
                transcript_event_id=confirmation.transcript.event_id,
                start_character=14,
                end_character_exclusive=22,
            ),
        ),
    )
    selection = c.VoiceConfirmationSelectionCandidate(
        protocol_version="1.0.0",
        output_type="VOICE_CONFIRMATION_SELECTION_CANDIDATE",
        producer="VOICE",
        selection_event_id=uuid4(),
        mapping_status=c.ConfirmationMappingStatus.MAPPED,
        decision_batch_view_id=review.view_id,
        decision_batch_view_hash=review.view_hash,
        observed_case_revision=8,
        transcript_event_id=confirmation.transcript.event_id,
        speaker_actor_id=PM_ID,
        selections=selections,
        unmentioned_item_policy="REMAIN_PENDING",
        clarification_question=None,
    )
    response_values = _base(0, actor=PM_ID)
    response_values.pop("expected_case_revision")
    response_values.update(
        command_type="APPLY_DECISION_BATCH_RESPONSE",
        observed_case_revision=8,
        actor_authentication=c.VerbalSelfAssertion(
            authentication_method=c.ActorAuthenticationMethod.VERBAL_SELF_ASSERTION,
            assurance_level=c.AssuranceLevel.SELF_ASSERTED,
            actor_id=PM_ID,
            asserted_display_name="PM",
            claimed_role="Product Manager",
            assertion_transcript_event_id=confirmation.transcript.event_id,
        ),
        selection=selection,
        decision_batch_view_id=review.view_id,
        decision_batch_view_hash=review.view_hash,
        response_transcript_event_id=confirmation.transcript.event_id,
        item_actions=tuple(
            c.DecisionBatchItemActionCommand(
                review_item_id=item.review_item_id,
                handle=item.handle,
                pending_decision_id=item.pending_decision_id,
                expected_pending_decision_version=item.pending_decision_version,
                action=selections[index].action,
                revision_span=selections[index].revision_span,
            )
            for index, item in enumerate(review.items)
        ),
        unmentioned_item_policy="REMAIN_PENDING",
    )
    command = c.ApplyDecisionBatchResponseCommand(**response_values)
    receipt = foundation.execute(command)
    assert [item.outcome for item in receipt.item_results] == ["COMMITTED", "REVISION_REQUESTED"]
    assert foundation.execute(command) == receipt
    problem = next(item for item in foundation.semantic_snapshot(CASE_ID).problems)
    assert problem.status is c.SemanticRecordStatus.RESOLVED

    engine = engine_for(url)
    with engine.connect() as connection:
        assert connection.execute(select(cases.c.revision).where(cases.c.id == str(CASE_ID))).scalar_one() == 9
        assert connection.execute(select(func.count()).select_from(audit_events)).scalar_one() == 9
        assert connection.execute(
            select(func.count()).select_from(WORKSHOP_PROTOCOL_TABLES["workshop_protocol_events"])
        ).scalar_one() == 6
        stored = connection.execute(
            select(WORKSHOP_PROTOCOL_TABLES["workshop_semantic_records"].c.payload_json).where(
                WORKSHOP_PROTOCOL_TABLES["workshop_semantic_records"].c.entity_kind == "DECISION"
            )
        ).scalars().all()
        assert stored and all("CANDIDATE_KEY" not in value for value in stored)
        finding_json = connection.execute(
            select(WORKSHOP_PROTOCOL_TABLES["workshop_semantic_records"].c.payload_json).where(
                WORKSHOP_PROTOCOL_TABLES["workshop_semantic_records"].c.entity_kind == "FINDING"
            )
        ).scalar_one()
        finding = c.AdmittedSemanticEvidenceFinding.model_validate_json(finding_json)
        assert finding.assessment is c.EvidenceAssessment.SUPPORTS
        assert finding.source_hash == foundation.test_source_set.ordered_sources[1].source.payload_hash

    snapshot = foundation.semantic_snapshot(CASE_ID)
    assert snapshot.evidence_findings == (finding,)

    bindings = foundation.confirmed_decision_synthesis_bindings(CASE_ID)
    assert len(bindings) == 1
    omitted_payload = PayloadFactory().payload("spec-package-payload.schema.json")
    identity_kinds = WorkshopFoundationService._artifact_identity_kinds(
        omitted_payload
    )
    plan = foundation.issue_artifact_identity_plan(
        CASE_ID, "SPEC_PACKAGE", (*identity_kinds.values(), "DECISION")
    )
    omitted_payload = bind_planned_identities(
        omitted_payload,
        {
            original: planned.foundation_id
            for original, planned in zip(
                identity_kinds,
                plan.planned_identities[: len(identity_kinds)],
                strict=True,
            )
        },
    )
    omitted_payload["decisions"] = []
    candidate = c.SpecPackageSynthesisCandidate(
        output_type="SPEC_PACKAGE_SYNTHESIS_CANDIDATE",
        analyzer_run_id=uuid4(),
        context_id=CONTEXT_ID,
        request_hash=REQUEST_HASH,
        source_set_hash=SOURCE_SET_HASH,
        based_on_case_revision=9,
        foundation_artifact_id=plan.target.foundation_artifact_id,
        identity_plan_id=plan.identity_plan_id,
        identity_plan_version=plan.identity_plan_version,
        semantic_state_hash=plan.semantic_state_hash,
        candidate_payload_json=json.dumps(
            omitted_payload, separators=(",", ":"), sort_keys=True
        ),
        payload_schema_id="spec-package-payload",
        payload_schema_version="4.0.2",
    )
    admission_values = _base(9)
    admission_values.update(
        command_type="ADMIT_SPEC_PACKAGE_SYNTHESIS",
        target=plan.target,
        identity_plan=plan,
        confirmed_decision_bindings=bindings,
        provider_request_hash=REQUEST_HASH,
        candidate=candidate,
    )
    with pytest.raises(FoundationProtocolError) as omitted_error:
        foundation.execute(c.AdmitSpecPackageSynthesisCommand(**admission_values))
    assert omitted_error.value.code is c.FoundationRejectionCode.CONFIRMATION_BINDING_FAILED
    assert foundation.case_revision(CASE_ID) == 9

    restarted = WorkflowService(database_url=url)
    assert restarted._cases[CASE_ID].revision == 9

    duplicate = command.model_copy(
        update={"command_id": uuid4(), "idempotency_key": f"idem-{uuid4()}"}
    )
    with pytest.raises(FoundationProtocolError) as error:
        foundation.execute(duplicate)
    assert error.value.code is c.FoundationRejectionCode.STALE_VIEW
    with engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(audit_events)).scalar_one() == 9


def test_transcript_hash_and_idempotency_conflicts_fail_without_mutation(tmp_path):
    url, foundation = _runtime(tmp_path)
    _activate(foundation)
    command = _transcript_command(4, 1, "Exact final transcript.")
    bad_event = command.transcript.model_copy(update={"transcript_hash": "sha256:" + "9" * 64})
    bad = command.model_copy(update={"transcript": bad_event})
    with pytest.raises(FoundationProtocolError) as error:
        foundation.execute(bad)
    assert error.value.code is c.FoundationRejectionCode.TRANSCRIPT_BINDING_FAILED

    applied = foundation.execute(command)
    conflicting_event = command.transcript.model_copy(
        update={"text": "Different content.", "transcript_hash": transcript_hash("Different content.")}
    )
    conflict = command.model_copy(update={"transcript": conflicting_event})
    with pytest.raises(FoundationProtocolError) as error:
        foundation.execute(conflict)
    assert error.value.code is c.FoundationRejectionCode.DUPLICATE_CONFLICT

    with engine_for(url).connect() as connection:
        assert connection.execute(select(cases.c.revision).where(cases.c.id == str(CASE_ID))).scalar_one() == 5
        assert connection.execute(select(func.count()).select_from(audit_events)).scalar_one() == 5
    assert applied.command.resulting_case_revision == 5


def test_voice_and_generic_http_endpoints_share_the_foundation_handler(tmp_path):
    _, foundation = _runtime(tmp_path)
    _activate(foundation)
    app = FastAPI()
    install_workshop_protocol_api(app, foundation)
    command = _transcript_command(4, 1, "The same final transcript enters Foundation.")
    with TestClient(app) as client:
        voice = client.post(
            "/api/v4/voice/final-transcripts",
            json=command.model_dump(mode="json", exclude_none=False),
        )
        assert voice.status_code == 200, voice.json()
        generic = client.post(
            "/api/v4/foundation/commands",
            json=command.model_dump(mode="json", exclude_none=False),
        )
        assert generic.status_code == 200
        assert generic.json() == voice.json()

        conflict = command.model_copy(
            update={
                "transcript": command.transcript.model_copy(
                    update={
                        "text": "Conflicting transcript.",
                        "transcript_hash": transcript_hash("Conflicting transcript."),
                    }
                )
            }
        )
        rejected = client.post(
            "/api/v4/foundation/commands",
            json=conflict.model_dump(mode="json", exclude_none=False),
        )
        assert rejected.status_code == 409
        assert rejected.json() == {"detail": {"code": "DUPLICATE_CONFLICT"}}


def test_artifact_confirmation_binds_exact_view_commits_once_and_survives_refresh(tmp_path):
    url, foundation = _runtime(tmp_path)
    _activate(foundation)
    artifact_id = uuid4()
    payload = PayloadFactory().payload("spec-package-payload.schema.json")
    _bind_fixture_refs(payload)
    digest = payload_hash(payload)
    governance = draft_governance(
        artifact_type="SPEC_PACKAGE",
        payload=payload,
        target=SimpleNamespace(next_artifact_version=1),
        quality_hash=quality_contract_hash(),
        now=NOW,
        new_id=uuid4,
    )
    tables = WORKSHOP_PROTOCOL_TABLES
    with engine_for(url).begin() as connection:
        connection.execute(
            insert(tables["workshop_artifact_records"]).values(
                artifact_id=str(artifact_id),
                artifact_version=1,
                case_id=str(CASE_ID),
                artifact_type="SPEC_PACKAGE",
                artifact_key="SPEC-TEST",
                record_revision=1,
                payload_hash=digest,
                payload_json=json.dumps(payload, separators=(",", ":"), sort_keys=True),
                governance_json=json.dumps(governance, separators=(",", ":"), sort_keys=True),
                status="DRAFT",
                confirmed_from_json=None,
                created_at=NOW.isoformat(),
            )
        )
    audit = _admit_quality(foundation, "SPEC_PACKAGE")
    assert audit.outcome is q.AuditOutcome.PASS
    initial_results = {
        item.rule_id: item.result for item in audit.combined_rule_results
    }
    assert initial_results["SPEC-Q-024"] is q.ComponentResult.PENDING
    assert initial_results["SPEC-Q-025"] is q.ComponentResult.PENDING
    assert sum(
        item.result is q.ComponentResult.PASS
        for item in audit.combined_rule_results
    ) == 24
    # Review projection needs only the immutable source-set binding. Historical
    # provider contexts remain readable after the semantic-quality contract advances.
    contexts = WORKSHOP_PROTOCOL_TABLES["workshop_analyzer_contexts"]
    with engine_for(url).begin() as connection:
        stored = connection.execute(
            select(contexts.c.binding_json).where(
                contexts.c.case_id == str(CASE_ID),
                contexts.c.status == c.ContextStatus.ACTIVE.value,
            )
        ).scalar_one()
        historical = json.loads(stored)
        historical["analyzer_contract"]["semantic_quality_contract_version"] = "2.1.0"
        connection.execute(
            update(contexts)
            .where(contexts.c.case_id == str(CASE_ID))
            .values(
                binding_json=json.dumps(
                    historical, separators=(",", ":"), sort_keys=True
                )
            )
        )
    review_values = _base(5)
    review_values.update(
        command_type="MATERIALIZE_ARTIFACT_REVIEW",
        subject=c.ArtifactReviewSubjectBinding(
            artifact_type="SPEC_PACKAGE",
            artifact_id=artifact_id,
            artifact_key="SPEC-TEST",
            artifact_version=1,
            record_revision=audit.resulting_record_revision,
            payload_hash=digest,
        ),
        view_mode="FINAL",
    )
    review = foundation.execute(c.MaterializeArtifactReviewCommand(**review_values))
    projection = foundation.current_artifact_review(CASE_ID)
    projected_record = foundation.latest_artifact_record(CASE_ID, "SPEC_PACKAGE")
    projected_rules = {
        item["rule_id"]: item["result"]
        for item in json.loads(projected_record["governance_json"])["readiness_audit"]["rule_results"]
    }
    assert projected_rules["SPEC-Q-025"] == "pass"
    assert "SPEC-Q-024" not in projected_rules
    view_id = review.view_id
    confirmation_id = review.confirmation_id
    transcript = _transcript_command(6, 1, "I confirm the displayed Spec Package.")
    foundation.execute(transcript)
    values = _base(7, actor=PM_ID)
    values.update(
        command_type="CONFIRM_ARTIFACT",
        actor_authentication=c.VerbalSelfAssertion(
            authentication_method=c.ActorAuthenticationMethod.VERBAL_SELF_ASSERTION,
            assurance_level=c.AssuranceLevel.SELF_ASSERTED,
            actor_id=PM_ID,
            asserted_display_name="PM",
            claimed_role="Product Manager",
            assertion_transcript_event_id=transcript.transcript.event_id,
        ),
        binding=c.ArtifactConfirmationBinding(
            artifact_type="SPEC_PACKAGE",
            artifact_id=artifact_id,
            artifact_key="SPEC-TEST",
            artifact_version=1,
            record_revision=projection["view"]["source"]["record_revision"],
            payload_hash=digest,
            confirmation_id=confirmation_id,
            view_id=view_id,
            view_hash=review.view_hash,
        ),
        confirmation_transcript_event_id=transcript.transcript.event_id,
        approved_exception_ids=(),
    )
    command = c.ConfirmArtifactCommand(**values)
    first = foundation.execute(command)
    assert foundation.execute(command) == first
    confirmed_record = foundation.latest_artifact_record(CASE_ID, "SPEC_PACKAGE")
    confirmed_rules = {
        item["rule_id"]: item["result"]
        for item in json.loads(confirmed_record["governance_json"])["readiness_audit"]["rule_results"]
    }
    assert confirmed_rules == {
        f"SPEC-Q-{index:03d}": "pass" for index in range(1, 27)
    }
    refreshed = WorkshopFoundationService(url, now=lambda: NOW).confirmed_artifact(
        CASE_ID, "SPEC_PACKAGE"
    )
    assert refreshed is not None
    assert refreshed["artifact_id"] == artifact_id
    assert refreshed["payload_hash"] == digest

    duplicate = command.model_copy(
        update={"command_id": uuid4(), "idempotency_key": f"idem-{uuid4()}"}
    )
    duplicate = duplicate.model_copy(update={"expected_case_revision": 8})
    with pytest.raises(FoundationProtocolError) as error:
        foundation.execute(duplicate)
    assert error.value.code is c.FoundationRejectionCode.CONFIRMATION_BINDING_FAILED
    with engine_for(url).connect() as connection:
        assert connection.execute(
            select(func.count()).select_from(tables["workshop_artifact_confirmations"])
        ).scalar_one() == 1
        assert connection.execute(select(func.count()).select_from(audit_events)).scalar_one() == 7


def test_v0_residual_spec_risk_acceptance_is_exact_durable_and_audit_immutable(tmp_path):
    url, foundation = _runtime(tmp_path)
    _activate(foundation)
    artifact_id = _insert_minimal_spec_artifact(foundation)
    bundle = _quality_bundle(foundation, "SPEC_PACKAGE")
    _prepare_quality_candidate(foundation, bundle)
    candidate = all_pass_candidate(bundle)
    q004 = next(item for item in candidate.assessments if item.rule_id == "SPEC-Q-004")
    q004 = q004.model_copy(
        update={
            "result": q.SemanticAssessmentResult.FAIL,
            "artifact_pointers": ("/actors",),
            "explanation": "One actor has an unresolved authority definition.",
            "findings": (
                q.QualityFindingCandidate(
                    candidate_key="actor-authority-unresolved",
                    severity="HIGH",
                    category="ACTOR_AUTHORITY",
                    message="Define the actor authority before a production release.",
                    artifact_pointers=("/actors",),
                    evidence_ids=(),
                    transcript_event_ids=(),
                ),
            ),
        }
    )
    candidate = candidate.model_copy(
        update={
            "assessments": tuple(
                q004 if item.rule_id == q004.rule_id else item
                for item in candidate.assessments
            )
        }
    )
    audit = _admit_quality_candidate(foundation, bundle, candidate)
    assert audit.outcome is q.AuditOutcome.BLOCKED
    assert tuple(
        item.rule_id
        for item in audit.combined_rule_results
        if item.result is q.ComponentResult.FAIL
    ) == ("SPEC-Q-004",)
    assert {
        item.rule_id
        for item in audit.combined_rule_results
        if item.result is q.ComponentResult.PENDING
    } == {"SPEC-Q-024", "SPEC-Q-025"}

    record = foundation.latest_artifact_record(CASE_ID, "SPEC_PACKAGE")
    review_values = _base(foundation.case_revision(CASE_ID))
    review_values.update(
        command_type="MATERIALIZE_ARTIFACT_REVIEW",
        subject=c.ArtifactReviewSubjectBinding(
            artifact_type="SPEC_PACKAGE",
            artifact_id=artifact_id,
            artifact_key=record["artifact_key"],
            artifact_version=record["artifact_version"],
            record_revision=record["record_revision"],
            payload_hash=record["payload_hash"],
        ),
        view_mode="REVIEW",
    )
    review = foundation.execute(c.MaterializeArtifactReviewCommand(**review_values))
    statement = (
        "I accept the listed residual Spec quality failures for this exact V0 review "
        "and authorize Technical Contract continuation."
    )
    transcript = _transcript_command(
        foundation.case_revision(CASE_ID), 1, statement
    )
    foundation.execute(transcript)
    projection = foundation.current_artifact_review(CASE_ID)
    source = projection["view"]["source"]
    binding = c.ArtifactConfirmationBinding(
        artifact_type="SPEC_PACKAGE",
        artifact_id=UUID(source["artifact_id"]),
        artifact_key=source["artifact_key"],
        artifact_version=source["artifact_version"],
        record_revision=source["record_revision"],
        payload_hash=source["payload_hash"],
        confirmation_id=review.confirmation_id,
        view_id=review.view_id,
        view_hash=review.view_hash,
    )
    authentication = c.VerbalSelfAssertion(
        authentication_method=c.ActorAuthenticationMethod.VERBAL_SELF_ASSERTION,
        assurance_level=c.AssuranceLevel.SELF_ASSERTED,
        actor_id=PM_ID,
        asserted_display_name="PM",
        claimed_role="Product Manager",
        assertion_transcript_event_id=transcript.transcript.event_id,
    )

    def confirm_command(*, failed_rule_ids, acceptance_statement=statement):
        values = _base(foundation.case_revision(CASE_ID), actor=PM_ID)
        values.update(
            command_type="CONFIRM_ARTIFACT",
            actor_authentication=authentication,
            binding=binding,
            confirmation_transcript_event_id=transcript.transcript.event_id,
            approved_exception_ids=(),
            residual_quality_risk_acceptance=c.ResidualQualityRiskAcceptance(
                policy_id="V0_EXPLICIT_RESIDUAL_SPEC_RISK",
                audit_id=audit.audit_id,
                accepted_failed_rule_ids=failed_rule_ids,
                acceptance_statement=acceptance_statement,
            ),
        )
        return c.ConfirmArtifactCommand(**values)

    audits = ARTIFACT_QUALITY_TABLES["workshop_artifact_quality_audits"]
    with foundation.engine.connect() as connection:
        immutable_before = dict(
            connection.execute(
                select(audits).where(audits.c.audit_id == str(audit.audit_id))
            ).mappings().one()
        )
    with pytest.raises(FoundationProtocolError) as mismatch:
        foundation.execute(confirm_command(failed_rule_ids=("SPEC-Q-005",)))
    assert mismatch.value.code is c.FoundationRejectionCode.CONFIRMATION_BINDING_FAILED
    with pytest.raises(FoundationProtocolError) as transcript_mismatch:
        foundation.execute(
            confirm_command(
                failed_rule_ids=("SPEC-Q-004",),
                acceptance_statement="I accept a different statement.",
            )
        )
    assert (
        transcript_mismatch.value.code
        is c.FoundationRejectionCode.TRANSCRIPT_BINDING_FAILED
    )

    command = confirm_command(failed_rule_ids=("SPEC-Q-004",))
    confirmed = foundation.execute(command)
    assert foundation.execute(command) == confirmed
    exact = foundation.confirmed_spec_binding(CASE_ID)
    assert exact is not None
    assert exact.foundation_artifact_id == artifact_id
    assert exact.record_revision == source["record_revision"]
    assert exact.payload_hash == source["payload_hash"]

    phases = ARTIFACT_QUALITY_TABLES["workshop_artifact_quality_gate_phases"]
    confirmations = WORKSHOP_PROTOCOL_TABLES["workshop_artifact_confirmations"]
    with foundation.engine.connect() as connection:
        immutable_after = dict(
            connection.execute(
                select(audits).where(audits.c.audit_id == str(audit.audit_id))
            ).mappings().one()
        )
        risk_phase = connection.execute(
            select(phases).where(
                phases.c.audit_id == str(audit.audit_id),
                phases.c.phase == "RESIDUAL_RISK_ACCEPTANCE",
            )
        ).mappings().one()
        confirmation_count = connection.execute(
            select(func.count()).select_from(confirmations)
        ).scalar_one()
    assert immutable_after == immutable_before
    evidence = json.loads(risk_phase["evidence_json"])
    assert evidence["audit_outcome"] == "BLOCKED"
    assert evidence["accepted_failed_rule_ids"] == ["SPEC-Q-004"]
    assert evidence["approved_finding_ids"] == [str(audit.findings[0].finding_id)]
    assert evidence["acceptance_statement"] == statement
    governance = json.loads(
        foundation.latest_artifact_record(CASE_ID, "SPEC_PACKAGE")["governance_json"]
    )
    results = {
        item["rule_id"]: item["result"]
        for item in governance["readiness_audit"]["rule_results"]
    }
    assert results["SPEC-Q-004"] == "fail"
    assert results["SPEC-Q-024"] == "pass"
    assert results["SPEC-Q-025"] == "pass"
    assert governance["confirmation"]["exceptions"] == [
        str(audit.findings[0].finding_id)
    ]

    stale = command.model_copy(
        update={
            "command_id": uuid4(),
            "idempotency_key": f"idem-{uuid4()}",
            "expected_case_revision": foundation.case_revision(CASE_ID),
        }
    )
    with pytest.raises(FoundationProtocolError) as duplicate:
        foundation.execute(stale)
    assert duplicate.value.code is c.FoundationRejectionCode.CONFIRMATION_BINDING_FAILED
    with foundation.engine.connect() as connection:
        assert connection.execute(
            select(func.count()).select_from(confirmations)
        ).scalar_one() == confirmation_count == 1


def test_guidance_is_admitted_only_from_exact_foundation_question_and_dependencies(tmp_path):
    url, foundation = _runtime(tmp_path)
    _activate(foundation)
    brief = c.InterviewBriefCandidate(
        protocol_version="1.0.0",
        output_type="INTERVIEW_BRIEF_CANDIDATE",
        analyzer_run_id=RUN_ID,
        context_id=CONTEXT_ID,
        request_hash=REQUEST_HASH,
        source_set_hash=SOURCE_SET_HASH,
        based_on_case_revision=4,
        customer_promise_summary="Export filtered orders.",
        evidence_candidates=(
            c.EvidenceCandidate(
                candidate_key="evidence-brief",
                source_role=c.SourceRole.TECHNICAL_CONTRACT,
                locator=c.SourceLineLocator(
                    locator_kind=c.SourceLocatorKind.SOURCE_LINES,
                    start_line=531,
                    end_line=531,
                ),
                relevance_claim="The source fixes encoding.",
                quoted_text_candidate="CSV output uses UTF-8 and one header row.",
            ),
        ),
        problems=(
            c.ProblemCandidate(
                candidate_key="problem-brief",
                problem_kind=c.ProblemKind.MISSING_DECISION,
                domain=c.Domain.PRODUCT,
                severity=c.Severity.HIGH,
                statement="The PM must confirm the encoding policy.",
                consequence="Consumers need a stable encoding.",
                evidence_candidate_keys=("evidence-brief",),
            ),
        ),
        problem_clusters=(),
        questions=tuple(
            c.QuestionCandidate(
                candidate_key=(
                    "question-brief" if index == 1 else f"question-brief-{index}"
                ),
                text=(
                    "Should every export use UTF-8?"
                    if index == 1
                    else f"Should export policy area {index} use UTF-8?"
                ),
                rationale="This closes the encoding decision.",
                question_shape=c.QuestionShape.CLOSED_BOOLEAN,
                capture_policy=c.CapturePolicy.BINDING_DECISION,
                answer_options=(),
                addresses_problem_keys=("problem-brief",),
                prerequisite_problem_keys=(),
                safe_without_current_turn_interpretation=True,
            )
            for index in range(1, c.INITIAL_RUNWAY_DEPTH + 1)
        ),
        initial_runway=c.QuestionRunwayCandidate(
            recommended_question_key="question-brief",
            safe_alternate_question_keys=tuple(
                f"question-brief-{index}"
                for index in range(2, c.INITIAL_RUNWAY_DEPTH + 1)
            ),
            do_not_ask_question_keys=(),
        ),
        confirmation_checkpoints=(),
    )
    values = _base(4)
    values.update(
        command_type="ADMIT_INTERVIEW_BRIEF",
        analyzer_run_id=RUN_ID,
        context_id=CONTEXT_ID,
        provider_request_hash=REQUEST_HASH,
        candidate=brief,
    )
    admitted = foundation.execute(c.AdmitInterviewBriefCommand(**values))
    question = next(item for item in admitted.identity_mappings if item.entity_kind == "QUESTION")
    problem = next(item for item in admitted.identity_mappings if item.entity_kind == "PROBLEM")
    question_ref = c.FoundationEntityRef(
        ref_kind="FOUNDATION_ID",
        foundation_id=question.foundation_id,
        expected_version=question.record_version,
    )
    guidance_candidate = c.GuidanceCandidate(
        protocol_version="1.0.0",
        output_type="GUIDANCE_CANDIDATE",
        analyzer_run_id=UUID("10000000-0000-4000-8000-000000000099"),
        context_id=CONTEXT_ID,
        request_hash="sha256:" + "9" * 64,
        source_set_hash=SOURCE_SET_HASH,
        based_on_case_revision=5,
        recommended_question=c.GuidanceQuestion(
            question_ref=question_ref,
            exact_text="Should every export use UTF-8?",
            reason="This is the next safe question.",
        ),
        safe_alternates=(),
        do_not_ask_question_refs=(),
        dependencies=(
            c.GuidanceDependency(
                dependency_kind=c.GuidanceDependencyKind.SOURCE_SET,
                entity_ref=None,
            ),
            c.GuidanceDependency(
                dependency_kind=c.GuidanceDependencyKind.PROBLEM,
                entity_ref=c.FoundationEntityRef(
                    ref_kind="FOUNDATION_ID",
                    foundation_id=problem.foundation_id,
                    expected_version=problem.record_version,
                ),
            ),
        ),
        acknowledgement_suggestion="The encoding question is ready.",
    )
    guidance_values = _base(5)
    guidance_values.update(
        command_type="ADMIT_GUIDANCE",
        analyzer_run_id=guidance_candidate.analyzer_run_id,
        context_id=CONTEXT_ID,
        provider_request_hash=guidance_candidate.request_hash,
        candidate=guidance_candidate,
    )
    receipt = foundation.execute(c.AdmitGuidanceCommand(**guidance_values))
    assert receipt.admitted_guidance_id is not None
    with engine_for(url).connect() as connection:
        payload = connection.execute(
            select(WORKSHOP_PROTOCOL_TABLES["workshop_guidance"].c.payload_json).where(
                WORKSHOP_PROTOCOL_TABLES["workshop_guidance"].c.valid == 1
            )
        ).scalar_one()
    guidance = c.AdmittedGuidance.model_validate_json(payload)
    assert guidance.recommended_question.question_id == question.foundation_id
    assert c.GuidanceInvalidationTrigger.SOURCE_SET_CHANGED in guidance.invalidation_triggers


def test_full_spec_then_technical_contract_validation_projection_and_exact_lineage(tmp_path):
    url, foundation = _runtime(tmp_path)
    _activate(foundation)
    factory = PayloadFactory()

    def synthesize(artifact_type: str, revision: int, confirmed_spec=None):
        filename = (
            "spec-package-payload.schema.json"
            if artifact_type == "SPEC_PACKAGE"
            else "technical-contract-payload.schema.json"
        )
        payload = factory.payload(filename)
        # The fixture has no evidence entries. Give Foundation a complete plan
        # for every payload-owned UUID while preserving non-identity references.
        identity_kinds = WorkshopFoundationService._artifact_identity_kinds(payload)
        plan = foundation.issue_artifact_identity_plan(
            CASE_ID, artifact_type, tuple(identity_kinds.values())
        )
        payload = bind_planned_identities(
            payload,
            {
                original: planned.foundation_id
                for original, planned in zip(
                    identity_kinds, plan.planned_identities, strict=True
                )
            },
        )
        _bind_fixture_refs(
            payload,
            None
            if confirmed_spec is None
            else json.loads(confirmed_spec.canonical_payload_json),
        )
        if artifact_type == "SPEC_PACKAGE":
            payload.update(foundation._server_owned_spec_records((), plan))
        # The current schemas call semantic identifiers `id`; the Foundation
        # guard also requires every planned identity to occur in provider JSON.
        run_id = uuid4()
        values = dict(
            analyzer_run_id=run_id,
            context_id=CONTEXT_ID,
            request_hash=REQUEST_HASH,
            source_set_hash=SOURCE_SET_HASH,
            based_on_case_revision=revision,
            foundation_artifact_id=plan.target.foundation_artifact_id,
            identity_plan_id=plan.identity_plan_id,
            identity_plan_version=plan.identity_plan_version,
            semantic_state_hash=plan.semantic_state_hash,
            candidate_payload_json=json.dumps(payload, separators=(",", ":"), sort_keys=True),
            payload_schema_version=(
                "4.0.2" if artifact_type == "SPEC_PACKAGE" else "4.0.0"
            ),
        )
        if artifact_type == "SPEC_PACKAGE":
            candidate = c.SpecPackageSynthesisCandidate(
                **values,
                output_type="SPEC_PACKAGE_SYNTHESIS_CANDIDATE",
                payload_schema_id="spec-package-payload",
            )
            command_values = _base(revision)
            command_values.update(
                command_type="ADMIT_SPEC_PACKAGE_SYNTHESIS",
                target=plan.target,
                identity_plan=plan,
                provider_request_hash=REQUEST_HASH,
                candidate=candidate,
            )
            receipt = foundation.execute(c.AdmitSpecPackageSynthesisCommand(**command_values))
        else:
            candidate = c.TechnicalContractSynthesisCandidate(
                **values,
                output_type="TECHNICAL_CONTRACT_SYNTHESIS_CANDIDATE",
                payload_schema_id="technical-contract-payload",
            )
            command_values = _base(revision)
            command_values.update(
                command_type="ADMIT_TECHNICAL_CONTRACT_SYNTHESIS",
                target=plan.target,
                identity_plan=plan,
                confirmed_spec=confirmed_spec,
                provider_request_hash=REQUEST_HASH,
                candidate=candidate,
            )
            receipt = foundation.execute(c.AdmitTechnicalContractSynthesisCommand(**command_values))
        return receipt, payload

    spec_admitted, spec_payload = synthesize("SPEC_PACKAGE", 4)
    spec_audit = _admit_quality(foundation, "SPEC_PACKAGE")
    assert spec_audit.outcome is q.AuditOutcome.PASS
    review_values = _base(6)
    review_values.update(
        command_type="MATERIALIZE_ARTIFACT_REVIEW",
        subject=c.ArtifactReviewSubjectBinding(
            artifact_type="SPEC_PACKAGE",
            artifact_id=spec_admitted.artifact_id,
            artifact_key=spec_admitted.artifact_key,
            artifact_version=spec_admitted.artifact_version,
            record_revision=spec_audit.resulting_record_revision,
            payload_hash=spec_admitted.payload_hash,
        ),
        view_mode="REVIEW",
    )
    spec_review = foundation.execute(c.MaterializeArtifactReviewCommand(**review_values))
    projection = foundation.current_artifact_review(CASE_ID)
    assert projection["view"]["view_type"] == "spec_package_view"
    assert projection["view"]["projection_integrity"]["all_material_items_included"] is True
    transcript = _transcript_command(7, 1, "I confirm the exact Spec Package review.")
    foundation.execute(transcript)
    confirm_values = _base(8, actor=PM_ID)
    confirm_values.update(
        command_type="CONFIRM_ARTIFACT",
        actor_authentication=c.VerbalSelfAssertion(
            authentication_method=c.ActorAuthenticationMethod.VERBAL_SELF_ASSERTION,
            assurance_level=c.AssuranceLevel.SELF_ASSERTED,
            actor_id=PM_ID,
            asserted_display_name="PM",
            claimed_role="Product Manager",
            assertion_transcript_event_id=transcript.transcript.event_id,
        ),
        binding=c.ArtifactConfirmationBinding(
            artifact_type="SPEC_PACKAGE",
            artifact_id=spec_admitted.artifact_id,
            artifact_key=spec_admitted.artifact_key,
            artifact_version=1,
            record_revision=projection["view"]["source"]["record_revision"],
            payload_hash=spec_admitted.payload_hash,
            confirmation_id=spec_review.confirmation_id,
            view_id=spec_review.view_id,
            view_hash=spec_review.view_hash,
        ),
        confirmation_transcript_event_id=transcript.transcript.event_id,
        approved_exception_ids=(),
    )
    foundation.execute(c.ConfirmArtifactCommand(**confirm_values))
    exact = foundation.confirmed_spec_binding(CASE_ID)
    assert exact is not None
    assert json.loads(exact.canonical_payload_json) == spec_payload

    technical_admitted, _ = synthesize("TECHNICAL_CONTRACT", 9, exact)
    technical_audit = _admit_quality(foundation, "TECHNICAL_CONTRACT")
    assert technical_audit.outcome is q.AuditOutcome.PASS
    review_values = _base(11)
    review_values.update(
        command_type="MATERIALIZE_ARTIFACT_REVIEW",
        subject=c.ArtifactReviewSubjectBinding(
            artifact_type="TECHNICAL_CONTRACT",
            artifact_id=technical_admitted.artifact_id,
            artifact_key=technical_admitted.artifact_key,
            artifact_version=1,
            record_revision=technical_audit.resulting_record_revision,
            payload_hash=technical_admitted.payload_hash,
        ),
        view_mode="REVIEW",
    )
    technical_review = foundation.execute(c.MaterializeArtifactReviewCommand(**review_values))
    projection = foundation.current_artifact_review(CASE_ID)
    assert projection["view"]["view_type"] == "technical_contract_view"
    with engine_for(url).connect() as connection:
        assert connection.execute(
            select(func.count())
            .select_from(WORKSHOP_PROTOCOL_TABLES["workshop_artifact_reviews"])
            .where(
                WORKSHOP_PROTOCOL_TABLES["workshop_artifact_reviews"].c.case_id
                == str(CASE_ID),
                WORKSHOP_PROTOCOL_TABLES["workshop_artifact_reviews"].c.current == 1,
            )
        ).scalar_one() == 1
    source = projection["view"]["source"]
    assert source["confirmed_spec_artifact_id"] == str(exact.foundation_artifact_id)
    assert source["confirmed_spec_record_revision"] == exact.record_revision
    assert source["confirmed_spec_confirmation_id"] == str(exact.confirmation_id)

    transcript2 = _transcript_command(12, 2, "I approve the exact Technical Contract review.", actor=DEV_ID)
    foundation.execute(transcript2)
    tech_confirm_values = _base(13, actor=DEV_ID)
    tech_confirm_values.update(
        command_type="CONFIRM_ARTIFACT",
        actor_authentication=c.VerbalSelfAssertion(
            authentication_method=c.ActorAuthenticationMethod.VERBAL_SELF_ASSERTION,
            assurance_level=c.AssuranceLevel.SELF_ASSERTED,
            actor_id=DEV_ID,
            asserted_display_name="Dev Lead",
            claimed_role="Development Lead",
            assertion_transcript_event_id=transcript2.transcript.event_id,
        ),
        binding=c.ArtifactConfirmationBinding(
            artifact_type="TECHNICAL_CONTRACT",
            artifact_id=technical_admitted.artifact_id,
            artifact_key=technical_admitted.artifact_key,
            artifact_version=1,
            record_revision=projection["view"]["source"]["record_revision"],
            payload_hash=technical_admitted.payload_hash,
            confirmation_id=technical_review.confirmation_id,
            view_id=technical_review.view_id,
            view_hash=technical_review.view_hash,
        ),
        confirmation_transcript_event_id=transcript2.transcript.event_id,
        approved_exception_ids=(),
    )
    confirmed = foundation.execute(c.ConfirmArtifactCommand(**tech_confirm_values))
    assert foundation.execute(c.ConfirmArtifactCommand(**tech_confirm_values)) == confirmed
    assert WorkshopFoundationService(url, now=lambda: NOW).confirmed_artifact(
        CASE_ID, "TECHNICAL_CONTRACT"
    )["payload_hash"] == technical_admitted.payload_hash


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("stale_request", c.FoundationRejectionCode.PROVIDER_REQUEST_BINDING_FAILED),
        ("unknown_pointer", c.FoundationRejectionCode.UNKNOWN_REFERENCE),
        ("unknown_evidence", c.FoundationRejectionCode.UNKNOWN_REFERENCE),
    ],
)
def test_quality_attestation_bindings_and_references_fail_closed(
    tmp_path, mutation, expected_code
):
    _, foundation = _runtime(tmp_path)
    _activate(foundation)
    _insert_minimal_spec_artifact(foundation)
    bundle = _quality_bundle(foundation, "SPEC_PACKAGE")
    _prepare_quality_candidate(foundation, bundle)
    candidate = all_pass_candidate(bundle)
    if mutation == "stale_request":
        candidate = candidate.model_copy(update={"request_hash": "sha256:" + "f" * 64})
    else:
        first = candidate.assessments[0]
        if mutation == "unknown_pointer":
            first = first.model_copy(update={"artifact_pointers": ("/missing",)})
        else:
            first = first.model_copy(update={"evidence_ids": (uuid4(),)})
        candidate = candidate.model_copy(
            update={"assessments": (first, *candidate.assessments[1:])}
        )
    with pytest.raises(FoundationProtocolError) as caught:
        _admit_quality_candidate(foundation, bundle, candidate)
    assert caught.value.code is expected_code
    assert foundation.case_revision(CASE_ID) == bundle.based_on_case_revision


def test_quality_abstention_is_reviewable_but_cannot_enter_confirmation_gate(tmp_path):
    _, foundation = _runtime(tmp_path)
    _activate(foundation)
    _insert_minimal_spec_artifact(foundation)
    bundle = _quality_bundle(foundation, "SPEC_PACKAGE")
    _prepare_quality_candidate(foundation, bundle)
    candidate = all_pass_candidate(bundle)
    abstained = candidate.assessments[0].model_copy(
        update={
            "result": q.SemanticAssessmentResult.ABSTAIN,
            "artifact_pointers": (),
            "explanation": "The evaluator cannot establish this semantic rule.",
        }
    )
    candidate = candidate.model_copy(
        update={"assessments": (abstained, *candidate.assessments[1:])}
    )
    receipt = _admit_quality_candidate(foundation, bundle, candidate)
    assert receipt.outcome is not q.AuditOutcome.PASS
    abstained_result = next(
        item
        for item in receipt.combined_rule_results
        if item.rule_id == abstained.rule_id
    )
    assert abstained_result.result is q.ComponentResult.FAIL

    record = foundation.latest_artifact_record(CASE_ID, "SPEC_PACKAGE")
    review_values = _base(foundation.case_revision(CASE_ID))
    review_values.update(
        command_type="MATERIALIZE_ARTIFACT_REVIEW",
        subject=c.ArtifactReviewSubjectBinding(
            artifact_type="SPEC_PACKAGE",
            artifact_id=UUID(record["artifact_id"]),
            artifact_key=record["artifact_key"],
            artifact_version=record["artifact_version"],
            record_revision=record["record_revision"],
            payload_hash=record["payload_hash"],
        ),
        view_mode="REVIEW",
    )
    review = foundation.execute(c.MaterializeArtifactReviewCommand(**review_values))
    assert review.receipt_type == "ARTIFACT_REVIEW"
    current = foundation.latest_artifact_record(CASE_ID, "SPEC_PACKAGE")
    with foundation.engine.begin() as connection:
        with pytest.raises(FoundationProtocolError) as caught:
            foundation.quality_audit_for_record(
                connection, current, require_pass=True
            )
    assert caught.value.code is c.FoundationRejectionCode.INVALID_TRANSITION


def test_failed_audit_can_apply_exactly_one_durable_bounded_revision(tmp_path):
    _, foundation = _runtime(tmp_path)
    _activate(foundation)
    _insert_minimal_spec_artifact(foundation)
    bundle = _quality_bundle(foundation, "SPEC_PACKAGE")
    _prepare_quality_candidate(foundation, bundle)
    candidate = all_pass_candidate(bundle)
    failed = candidate.assessments[0].model_copy(
        update={
            "result": q.SemanticAssessmentResult.FAIL,
            "artifact_pointers": ("/requirements",),
            "explanation": "The requirement combines two independently testable duties.",
            "findings": (
                q.QualityFindingCandidate(
                    candidate_key="split-atomic-requirement",
                    severity="HIGH",
                    category="ATOMICITY",
                    message="Split the two duties into independent requirements.",
                    artifact_pointers=("/requirements",),
                    evidence_ids=(),
                    transcript_event_ids=(),
                ),
            ),
        }
    )
    candidate = candidate.model_copy(
        update={"assessments": (failed, *candidate.assessments[1:])}
    )
    rejected = _admit_quality_candidate(foundation, bundle, candidate)
    assert rejected.outcome is not q.AuditOutcome.PASS

    request = foundation.prepare_artifact_quality_revision(
        CASE_ID,
        "SPEC_PACKAGE",
        allocated_identity_kinds=("REQUIREMENT",),
    )
    payload = json.loads(request.canonical_payload_json)
    replacement = [dict(item) for item in payload["requirements"]]
    added = dict(replacement[0])
    added["id"] = str(request.allocated_identities[0].foundation_id)
    added["title"] = "One independently testable duty"
    replacement.append(added)
    revision_candidate = q.ArtifactQualityRevisionCandidate(
        protocol_version=q.PROTOCOL_VERSION,
        output_type="ARTIFACT_QUALITY_REVISION_CANDIDATE",
        revision_request_id=request.revision_request_id,
        revision_request_version=1,
        request_hash=request.request_hash,
        artifact_id=request.artifact_id,
        artifact_version=request.artifact_version,
        record_revision=request.record_revision,
        payload_hash=request.payload_hash,
        attempt=1,
        patches=(
            q.ArtifactRevisionPatch(
                pointer="/requirements",
                replacement_value_json=json.dumps(replacement),
            ),
        ),
    )

    applied = foundation.apply_artifact_quality_revision(
        CASE_ID, request, revision_candidate
    )
    replay = foundation.apply_artifact_quality_revision(
        CASE_ID, request, revision_candidate
    )

    assert applied.replayed is False
    assert replay.model_copy(update={"replayed": False}) == applied
    assert applied.prior_record_revision == rejected.resulting_record_revision
    assert applied.resulting_record_revision == rejected.resulting_record_revision + 1
    record = foundation.latest_artifact_record(CASE_ID, "SPEC_PACKAGE")
    assert record is not None
    assert record["payload_hash"] == applied.resulting_payload_hash
    assert len(json.loads(record["payload_json"])["requirements"]) == 2
    assert foundation.case_revision(CASE_ID) == request.based_on_case_revision + 1


def test_quality_provider_mapping_and_admitted_receipt_survive_restart(tmp_path):
    url, foundation = _runtime(tmp_path)
    _activate(foundation)
    _insert_minimal_spec_artifact(foundation)
    bundle = _quality_bundle(foundation, "SPEC_PACKAGE")
    _prepare_quality_candidate(foundation, bundle)

    restarted = WorkshopFoundationService(url, now=lambda: NOW)
    provider = restarted.artifact_quality_provider_context(bundle.audit_id)
    assert provider == {
        "provider": "OPENAI",
        "model": "gpt-5.6-terra",
        "reasoning_effort": "medium",
        "provider_conversation_id": f"conv_negative_{str(bundle.audit_id)[:12]}",
        "client_request_id": f"aqa-negative-{bundle.request_hash[7:23]}",
        "started_at": NOW.isoformat().replace("+00:00", "Z"),
    }
    admitted = _admit_quality_candidate(
        restarted, bundle, all_pass_candidate(bundle)
    )
    assert admitted.replayed is False

    after_restart = WorkshopFoundationService(url, now=lambda: NOW)
    replay = after_restart.prepare_artifact_quality_audit(
        q.PrepareArtifactQualityAuditCommand(
            protocol_version=q.PROTOCOL_VERSION,
            command_id=quality_stable_id(bundle.audit_id, "prepare-negative"),
            idempotency_key=f"quality-negative-prepare-{bundle.audit_id}",
            expected_case_revision=bundle.based_on_case_revision,
            bundle=bundle,
        )
    )
    assert replay.existing_receipt is not None
    assert replay.existing_receipt.replayed is True
    assert replay.existing_receipt.model_copy(update={"replayed": False}) == admitted


def test_quality_response_checkpoint_survives_restart_without_recreate(tmp_path):
    url, foundation = _runtime(tmp_path)
    _activate(foundation)
    _insert_minimal_spec_artifact(foundation)
    bundle = _quality_bundle(foundation, "SPEC_PACKAGE")
    _prepare_quality_candidate(foundation, bundle)
    command = q.CheckpointArtifactQualityResponseCommand(
        protocol_version=q.PROTOCOL_VERSION,
        command_id=quality_stable_id(bundle.audit_id, "checkpoint-response"),
        audit_id=bundle.audit_id,
        request_hash=bundle.request_hash,
        client_request_id=f"aqa-response-{bundle.request_hash[7:39]}",
        provider_response_id=f"resp_checkpoint_{str(bundle.audit_id)[:12]}",
    )

    foundation.checkpoint_artifact_quality_response(command)
    foundation.checkpoint_artifact_quality_response(command)

    restarted = WorkshopFoundationService(url, now=lambda: NOW)
    provider = restarted.artifact_quality_provider_context(bundle.audit_id)
    assert provider["provider_response_id"] == command.provider_response_id
    assert provider["response_client_request_id"] == command.client_request_id

    with pytest.raises(FoundationProtocolError) as caught:
        restarted.checkpoint_artifact_quality_response(
            command.model_copy(update={"provider_response_id": "resp_conflict"})
        )
    assert caught.value.code is c.FoundationRejectionCode.DUPLICATE_CONFLICT


@pytest.mark.parametrize("shape", ["missing", "duplicate"])
def test_semantic_attestation_requires_exactly_one_result_per_semantic_rule(shape):
    # Use a complete runtime-shaped bundle from the strict contract fixture.
    rules = tuple(
        q.SemanticRuleAssessment(
            rule_id=f"SPEC-Q-{index:03d}",
            result=q.SemanticAssessmentResult.PASS,
            explanation="The complete payload supports this rule.",
            applicability_reason=None,
            artifact_pointers=("",),
            evidence_ids=(),
            transcript_event_ids=(),
            findings=(),
        )
        for index in range(1, 22)
    )
    if shape == "missing":
        invalid = rules[:-1]
    else:
        invalid = (*rules[:-1], rules[0])
    with pytest.raises(ValueError):
        q.ArtifactSemanticAttestationCandidate(
            protocol_version=q.PROTOCOL_VERSION,
            output_type="ARTIFACT_SEMANTIC_ATTESTATION_CANDIDATE",
            audit_id=uuid4(),
            evaluator_run_id=uuid4(),
            request_hash="sha256:" + "1" * 64,
            artifact_id=uuid4(),
            artifact_version=1,
            record_revision=1,
            payload_hash="sha256:" + "2" * 64,
            audit_scope_manifest_hash="sha256:" + "3" * 64,
            semantic_quality_contract_hash="sha256:" + "4" * 64,
            assessments=invalid,
        )


@pytest.mark.parametrize("plan_error", ["wrong_kind", "missing_identity"])
def test_artifact_admission_rejects_noncanonical_identity_plan(tmp_path, plan_error):
    url, foundation = _runtime(tmp_path)
    _activate(foundation)
    payload = PayloadFactory().payload("spec-package-payload.schema.json")
    _bind_fixture_refs(payload)
    identity_kinds = WorkshopFoundationService._artifact_identity_kinds(payload)
    plan = foundation.issue_artifact_identity_plan(
        CASE_ID, "SPEC_PACKAGE", tuple(identity_kinds.values())
    )
    payload = bind_planned_identities(
        payload,
        {
            original: planned.foundation_id
            for original, planned in zip(
                identity_kinds, plan.planned_identities, strict=True
            )
        },
    )
    payload.update(foundation._server_owned_spec_records((), plan))
    identities = list(plan.planned_identities)
    slots = list(plan.slots)
    if plan_error == "wrong_kind":
        identities[0] = identities[0].model_copy(update={"entity_kind": "COMPONENT"})
        slots[0] = slots[0].model_copy(update={"entity_kind": "COMPONENT"})
    else:
        identities.pop()
        slots.pop()
    invalid_plan = plan.model_copy(
        update={"planned_identities": tuple(identities), "slots": tuple(slots)}
    )
    candidate = c.SpecPackageSynthesisCandidate(
        output_type="SPEC_PACKAGE_SYNTHESIS_CANDIDATE",
        analyzer_run_id=uuid4(),
        context_id=CONTEXT_ID,
        request_hash=REQUEST_HASH,
        source_set_hash=SOURCE_SET_HASH,
        based_on_case_revision=4,
        foundation_artifact_id=plan.target.foundation_artifact_id,
        identity_plan_id=plan.identity_plan_id,
        identity_plan_version=plan.identity_plan_version,
        semantic_state_hash=plan.semantic_state_hash,
        candidate_payload_json=json.dumps(payload, separators=(",", ":"), sort_keys=True),
        payload_schema_id="spec-package-payload",
        payload_schema_version="4.0.2",
    )
    values = _base(4)
    values.update(
        command_type="ADMIT_SPEC_PACKAGE_SYNTHESIS",
        target=plan.target,
        identity_plan=invalid_plan,
        provider_request_hash=REQUEST_HASH,
        candidate=candidate,
    )
    with pytest.raises(FoundationProtocolError) as exc:
        foundation.execute(c.AdmitSpecPackageSynthesisCommand(**values))
    assert exc.value.code is c.FoundationRejectionCode.IDENTITY_PLAN_FAILED
    assert foundation.case_revision(CASE_ID) == 4
    with engine_for(url).connect() as connection:
        assert connection.execute(
            select(func.count()).select_from(
                WORKSHOP_PROTOCOL_TABLES["workshop_artifact_records"]
            )
        ).scalar_one() == 0


def test_artifact_admission_accepts_unused_bounded_identity_capacity(tmp_path):
    _, foundation = _runtime(tmp_path)
    _activate(foundation)
    payload = PayloadFactory().payload("spec-package-payload.schema.json")
    _bind_fixture_refs(payload)
    identity_kinds = WorkshopFoundationService._artifact_identity_kinds(payload)
    plan = foundation.issue_artifact_identity_plan(
        CASE_ID,
        "SPEC_PACKAGE",
        (*identity_kinds.values(), "REQUIREMENT"),
    )
    payload = bind_planned_identities(
        payload,
        {
            original: planned.foundation_id
            for original, planned in zip(
                identity_kinds,
                plan.planned_identities[: len(identity_kinds)],
                strict=True,
            )
        },
    )
    payload.update(foundation._server_owned_spec_records((), plan))
    candidate = c.SpecPackageSynthesisCandidate(
        output_type="SPEC_PACKAGE_SYNTHESIS_CANDIDATE",
        analyzer_run_id=uuid4(),
        context_id=CONTEXT_ID,
        request_hash=REQUEST_HASH,
        source_set_hash=SOURCE_SET_HASH,
        based_on_case_revision=4,
        foundation_artifact_id=plan.target.foundation_artifact_id,
        identity_plan_id=plan.identity_plan_id,
        identity_plan_version=plan.identity_plan_version,
        semantic_state_hash=plan.semantic_state_hash,
        candidate_payload_json=json.dumps(payload, separators=(",", ":"), sort_keys=True),
        payload_schema_id="spec-package-payload",
        payload_schema_version="4.0.2",
    )
    values = _base(4)
    values.update(
        command_type="ADMIT_SPEC_PACKAGE_SYNTHESIS",
        target=plan.target,
        identity_plan=plan,
        provider_request_hash=REQUEST_HASH,
        candidate=candidate,
    )

    receipt = foundation.execute(c.AdmitSpecPackageSynthesisCommand(**values))

    assert receipt.artifact_type == "SPEC_PACKAGE"
    assert foundation.case_revision(CASE_ID) == 5


def test_foundation_materializes_and_retains_exact_evidence_revision_once(tmp_path):
    _, foundation = _runtime(tmp_path)
    _activate(foundation)
    payload = PayloadFactory().payload("spec-package-payload.schema.json")
    _bind_fixture_refs(payload)
    identity_kinds = WorkshopFoundationService._artifact_identity_kinds(payload)
    plan = foundation.issue_artifact_identity_plan(
        CASE_ID,
        "SPEC_PACKAGE",
        (*identity_kinds.values(), "EVIDENCE", "SEMANTIC_EVIDENCE_FINDING"),
    )
    payload = bind_planned_identities(
        payload,
        {
            original: planned.foundation_id
            for original, planned in zip(
                identity_kinds,
                plan.planned_identities[: len(identity_kinds)],
                strict=True,
            )
        },
    )
    payload.update(foundation._server_owned_spec_records((), plan))
    evidence_id = plan.planned_identities[-2].foundation_id
    finding_id = plan.planned_identities[-1].foundation_id
    payload["evidence_catalog"] = [
        {
            "id": str(evidence_id),
            "source_id": "10000000-0000-4000-8000-000000000005",
            "source_hash": PM_SOURCE_HASH,
            "locator": "PM source line 1",
            "excerpt_hash": "sha256:86dae90de48c8838e01cc421488dc5b5001a4533638b25e6bc1a945c7f3aeab9",
            "claim_refs": [str(payload["requirements"][0]["id"])],
        }
    ]
    candidate = c.SpecPackageSynthesisCandidate(
        output_type="SPEC_PACKAGE_SYNTHESIS_CANDIDATE",
        analyzer_run_id=uuid4(),
        context_id=CONTEXT_ID,
        request_hash=REQUEST_HASH,
        source_set_hash=SOURCE_SET_HASH,
        based_on_case_revision=4,
        foundation_artifact_id=plan.target.foundation_artifact_id,
        identity_plan_id=plan.identity_plan_id,
        identity_plan_version=plan.identity_plan_version,
        semantic_state_hash=plan.semantic_state_hash,
        candidate_payload_json=json.dumps(payload, separators=(",", ":"), sort_keys=True),
        payload_schema_id="spec-package-payload",
        payload_schema_version="4.0.2",
    )
    values = _base(4)
    values.update(
        command_type="ADMIT_SPEC_PACKAGE_SYNTHESIS",
        target=plan.target,
        identity_plan=plan,
        provider_request_hash=REQUEST_HASH,
        candidate=candidate,
    )
    foundation.execute(c.AdmitSpecPackageSynthesisCommand(**values))
    record = foundation.latest_artifact_record(CASE_ID, "SPEC_PACKAGE")
    assert record is not None
    source = q.AuditSourceDocument(
        source_id=UUID("10000000-0000-4000-8000-000000000005"),
        role=q.SourceRole.PM_SPEC,
        version=1,
        payload_hash=PM_SOURCE_HASH,
        canonical_locator="/sources/pm.md",
        filename="pm.md",
        media_type="text/markdown",
        complete_text=PM_SOURCE_BYTES.decode(),
    )
    ids = iter((uuid4(), uuid4()))
    request = prepare_evidence_support_request(
        evaluator_run_id=uuid4(),
        artifact_id=UUID(record["artifact_id"]),
        artifact_version=record["artifact_version"],
        record_revision=record["record_revision"],
        payload=json.loads(record["payload_json"]),
        sources=(source,),
        proposals=(
            EvidenceSupportProposal(
                claim_ref=UUID(payload["requirements"][0]["id"]),
                claim_pointer="/requirements/0/behaviour",
                source_id=source.source_id,
                locator="PM source line 1",
                exact_excerpt="Export filtered orders.",
                evidence_ref=evidence_id,
                finding_ref=finding_id,
            ),
        ),
        new_id=lambda: next(ids),
    )
    support = q.ArtifactEvidenceSupportCandidate(
        protocol_version=q.PROTOCOL_VERSION,
        output_type="ARTIFACT_EVIDENCE_SUPPORT_CANDIDATE",
        request_id=request.request_id,
        evaluator_run_id=request.evaluator_run_id,
        request_hash=request.request_hash,
        artifact_id=request.artifact_id,
        artifact_version=request.artifact_version,
        record_revision=request.record_revision,
        payload_hash=request.payload_hash,
        assessments=(
            q.EvidenceSupportAssessment(
                pair_id=request.pairs[0].pair_id,
                assessment=q.EvidenceSupportResult.SUPPORTS,
                confidence=0.98,
            ),
        ),
    )

    execution = q.StandaloneEvaluatorExecutionBinding(
        provider="OPENAI",
        model="gpt-5.6-terra",
        reasoning_effort="medium",
        provider_response_id="resp_evidence_support_fixture",
        client_request_id=f"aqa-support-{request.request_hash[7:39]}",
        store_enabled=True,
        started_at=NOW,
        completed_at=NOW,
    )
    revised = foundation.materialize_artifact_evidence_support(
        CASE_ID, request, support, execution
    )

    revised_payload = json.loads(revised["payload_json"])
    assert revised["record_revision"] == 2
    assert revised["payload_hash"] != record["payload_hash"]
    assert revised_payload["evidence_catalog"][0]["id"] == str(evidence_id)
    assert revised_payload["semantic_evidence_findings"][0]["finding_id"] == str(
        finding_id
    )
    assert revised_payload["semantic_evidence_findings"][0]["assessment"] == "SUPPORTS"
    assert foundation.case_revision(CASE_ID) == 6
    with foundation.engine.connect() as connection:
        run = connection.execute(
            select(
                EVIDENCE_ASSESSMENT_TABLES[
                    "workshop_artifact_evidence_assessment_runs"
                ]
            )
        ).mappings().one()
        assessment = connection.execute(
            select(
                EVIDENCE_ASSESSMENT_TABLES[
                    "workshop_artifact_evidence_assessments"
                ]
            )
        ).mappings().one()
    assert run["canonical_count"] == 1
    assert run["quarantined_count"] == 0
    assert run["provider_response_id"] == execution.provider_response_id
    assert assessment["assessment"] == "SUPPORTS"
    assert assessment["disposition"] == "CANONICAL"
    with pytest.raises(IntegrityError, match="EVIDENCE_ASSESSMENT_APPEND_ONLY"):
        with foundation.engine.begin() as connection:
            connection.execute(
                update(
                    EVIDENCE_ASSESSMENT_TABLES[
                        "workshop_artifact_evidence_assessment_runs"
                    ]
                ).values(model="changed")
            )

    replayed = foundation.materialize_artifact_evidence_support(
        CASE_ID, request, support, execution
    )
    assert replayed["payload_hash"] == revised["payload_hash"]
    assert replayed["record_revision"] == revised["record_revision"]
    assert foundation.case_revision(CASE_ID) == 6


def test_artifact_admission_rejects_reference_to_unused_planned_identity(tmp_path):
    url, foundation = _runtime(tmp_path)
    _activate(foundation)
    payload = PayloadFactory().payload("spec-package-payload.schema.json")
    _bind_fixture_refs(payload)
    identity_kinds = WorkshopFoundationService._artifact_identity_kinds(payload)
    plan = foundation.issue_artifact_identity_plan(
        CASE_ID,
        "SPEC_PACKAGE",
        (*identity_kinds.values(), "REQUIREMENT"),
    )
    payload = bind_planned_identities(
        payload,
        {
            original: planned.foundation_id
            for original, planned in zip(
                identity_kinds,
                plan.planned_identities[: len(identity_kinds)],
                strict=True,
            )
        },
    )
    payload.update(foundation._server_owned_spec_records((), plan))
    unused_requirement_id = plan.planned_identities[-1].foundation_id
    payload["acceptance_checks"][0]["requirement_refs"] = [
        str(unused_requirement_id)
    ]
    candidate = c.SpecPackageSynthesisCandidate(
        output_type="SPEC_PACKAGE_SYNTHESIS_CANDIDATE",
        analyzer_run_id=uuid4(),
        context_id=CONTEXT_ID,
        request_hash=REQUEST_HASH,
        source_set_hash=SOURCE_SET_HASH,
        based_on_case_revision=4,
        foundation_artifact_id=plan.target.foundation_artifact_id,
        identity_plan_id=plan.identity_plan_id,
        identity_plan_version=plan.identity_plan_version,
        semantic_state_hash=plan.semantic_state_hash,
        candidate_payload_json=json.dumps(payload, separators=(",", ":"), sort_keys=True),
        payload_schema_id="spec-package-payload",
        payload_schema_version="4.0.2",
    )
    values = _base(4)
    values.update(
        command_type="ADMIT_SPEC_PACKAGE_SYNTHESIS",
        target=plan.target,
        identity_plan=plan,
        confirmed_decision_bindings=(),
        provider_request_hash=REQUEST_HASH,
        candidate=candidate,
    )

    with pytest.raises(FoundationProtocolError) as exc:
        foundation.execute(c.AdmitSpecPackageSynthesisCommand(**values))

    assert exc.value.code is c.FoundationRejectionCode.UNKNOWN_REFERENCE
    assert exc.value.safe_diagnostic_pointers == (
        "/acceptance_checks/0/requirement_refs/0",
    )
    assert str(unused_requirement_id) not in repr(exc.value.safe_diagnostic_pointers)
    assert foundation.case_revision(CASE_ID) == 4
    with engine_for(url).connect() as connection:
        assert connection.execute(
            select(func.count()).select_from(
                WORKSHOP_PROTOCOL_TABLES["workshop_artifact_records"]
            )
        ).scalar_one() == 0
        assert connection.execute(
            select(func.count()).select_from(
                WORKSHOP_PROTOCOL_TABLES["workshop_identity_plans"]
            )
        ).scalar_one() == 0
        assert connection.execute(
            select(func.count()).select_from(
                ARTIFACT_QUALITY_TABLES["workshop_artifact_quality_audits"]
            )
        ).scalar_one() == 0


def test_rule_derived_blueprint_projects_cross_domain_authority_deterministically():
    actor_id = uuid4()
    decision_id = uuid4()
    target = c.ArtifactDraftTarget(
        artifact_type="SPEC_PACKAGE",
        foundation_artifact_id=uuid4(),
        artifact_key="SPEC-BLUEPRINT",
        next_artifact_version=1,
    )
    plan = c.ArtifactSynthesisIdentityPlan(
        identity_plan_id=uuid4(),
        identity_plan_version=1,
        target=target,
        based_on_case_revision=9,
        semantic_state_hash="sha256:" + "7" * 64,
        source_entity_refs=(),
        planned_identities=(
            c.PlannedArtifactIdentity(
                foundation_id=actor_id,
                foundation_version=1,
                entity_kind="ACTOR",
            ),
            c.PlannedArtifactIdentity(
                foundation_id=decision_id,
                foundation_version=1,
                entity_kind="DECISION",
            ),
        ),
        slots=(
            c.ArtifactIdentitySlot(
                slot_key="ACTOR:0001",
                entity_kind="ACTOR",
                ordinal=1,
                owner="FOUNDATION",
                foundation_id=actor_id,
                allocation_mode="BOUND_EXISTING",
            ),
            c.ArtifactIdentitySlot(
                slot_key="DECISION:0001",
                entity_kind="DECISION",
                ordinal=1,
                owner="FOUNDATION",
                foundation_id=decision_id,
                allocation_mode="BOUND_EXISTING",
            ),
        ),
    )
    binding = c.ConfirmedDecisionSynthesisBinding(
        decision_id=decision_id,
        decision_version=1,
        classification=c.Domain.CROSS_DOMAIN,
        statement="Commit the accepted export snapshot immutably.",
        rationale="Acceptance fixes membership and values.",
        alternatives_considered=("Re-evaluate the export during download.",),
        problem_ids=(uuid4(),),
        evidence_ids=(uuid4(),),
        confirmation_id=uuid4(),
        decision_batch_view_id=uuid4(),
        decision_batch_view_hash="sha256:" + "8" * 64,
        review_item_id=uuid4(),
        confirmed_case_revision=9,
        actor_ref=actor_id,
        authority_validation_id=uuid4(),
        transcript_event_id=uuid4(),
        confirmed_at=NOW,
    )

    projection = WorkshopFoundationService._server_owned_spec_records(
        (binding,), plan
    )

    assert projection["actors"][0]["id"] == str(actor_id)
    assert projection["actors"][0]["authority_domains"] == ["cross_domain"]
    assert projection["decisions"][0]["id"] == str(decision_id)
    assert projection["decisions"][0]["decision"] == binding.statement
    assert projection["decisions"][0]["confirmation_binding"][
        "confirmation_id"
    ] == str(binding.confirmation_id)

    policy = WorkshopFoundationService.artifact_construction_policy(
        "SPEC_PACKAGE",
        confirmed_decision_count=4,
        quality_rule_ids=tuple(f"SPEC-Q-{index:03d}" for index in range(1, 27)),
    )
    counts = {
        kind: sum(item[0] == kind for item in policy)
        for kind, _, _ in policy
    }
    assert counts["DECISION"] == 4
    assert counts["REQUIREMENT"] == 14
    assert counts["SCOPE_ITEM"] == 16
    assert counts["BEHAVIOUR_RULE"] == 16
    assert counts["SCENARIO"] == 12
    assert counts["EXPERIENCE_STATE"] == 9
    assert counts["ACCEPTANCE_CHECK"] == 14


def test_foundation_initializes_absent_provider_evidence_fields_before_projection():
    provider_payload = {
        "requirements": [
            {}
        ],
        "behaviour_contract": {
            "always": [{}],
            "ask_first": [],
            "never": [],
        },
        "glossary": [],
        "outcomes": [],
        "scope": {"in_scope": [], "non_goals": [], "boundaries": []},
        "data_rules": [],
        "quality_attributes": [],
        "constraints": [],
        "dependencies": [],
        "risks": [],
    }

    WorkshopFoundationService._initialize_foundation_owned_evidence_refs(provider_payload)

    assert provider_payload["requirements"][0]["source_evidence_refs"] == []
    assert provider_payload["behaviour_contract"]["always"][0]["evidence_refs"] == []

    provider_payload["requirements"][0]["source_evidence_refs"] = [
        "10000000-0000-4000-8000-000000000010"
    ]
    with pytest.raises(FoundationProtocolError) as exc:
        WorkshopFoundationService._initialize_foundation_owned_evidence_refs(
            provider_payload
        )
    assert exc.value.code is c.FoundationRejectionCode.CONFIRMATION_BINDING_FAILED
