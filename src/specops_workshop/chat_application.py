"""Task 28 text-authoritative application services.

The ingress owns the only response-finalization transaction.  The projector is
strictly read-only and is shared by chat plus future channel adapters.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid5

from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import IntegrityError
from specops_contracts import workshop_v1 as c
from specops_contracts.canonical import transcript_hash
from specops_workflow.canonical import canonical_json, sha256
from specops_workflow.models import JsonPointer, SourceRef
from specops_workflow.persistence import (
    TASK28_RUNTIME_TABLES,
    V0_RUNTIME_TABLES,
    WORKSHOP_PROTOCOL_TABLES,
    audit_events,
    case_participants,
    source_artifacts,
)
from specops_workflow.workshop_protocol import FoundationProtocolError, WorkshopFoundationService

from .chat_contracts import (
    CommittedParticipantTurn,
    CommittedQuestionResponseTurn,
    FoundationAdmittedQuestion,
    InputChannel,
    ParticipantTurnReceipt,
    ProposalBinding,
    ProposalInteractionStatus,
    ProposalStatusProjection,
    QuestionRunway,
    SubmitTypedResponseIntent,
    TypedResponseSnapshot,
    WorkshopConversationContext,
    VisualProposalActionIntent,
    VisualProposalActionReceipt,
)


RESPONSE_NAMESPACE = UUID("e52d201a-e1d0-4df7-90e5-39ac4091998d")
ANALYZER_JOB_NAMESPACE = UUID("915e1809-c32a-52c6-9a2a-c498cf1d47d4")


class ParticipantTurnError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _instant(value) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _parse_instant(value: str | datetime) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(
        value.replace("Z", "+00:00")
    )


def _canonical_fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _turn_submission_status(connection, *, jobs, case_id: UUID) -> str:
    rows = connection.execute(
        select(jobs.c.operation, jobs.c.subject_id, jobs.c.state).where(
            jobs.c.case_id == str(case_id)
        )
    ).mappings().all()
    blocking_job_failed = any(
        not (
            row["operation"] == c.AnalyzerOperation.TURN_ANALYSIS.value
            and row["subject_id"].startswith("turn-correction:")
        )
        and row["state"] == "FAILED"
        for row in rows
    )
    if blocking_job_failed:
        return "ANALYSIS_FAILED"
    if any(row["state"] not in {"COMPLETED", "FAILED"} for row in rows):
        return "ANALYSIS_PENDING"
    return "READY"


def _job_identity(
    *, session_id: UUID, response_id: UUID, trigger_case_revision: int
) -> UUID:
    material = {
        "session_id": str(session_id).lower(),
        "operation": "TURN_ANALYSIS",
        "response_id": str(response_id).lower(),
        "trigger_case_revision": trigger_case_revision,
        "parent_job_id": None,
        "correction_index": 0,
    }
    return uuid5(ANALYZER_JOB_NAMESPACE, canonical_json(material).decode("utf-8"))


def _question_snapshot(
    guidance: c.AdmittedGuidance, row: dict[str, Any]
) -> FoundationAdmittedQuestion:
    return FoundationAdmittedQuestion(
        question_id=UUID(row["question_id"]),
        question_version=row["question_version"],
        exact_text=row["exact_text"],
        reason=row["reason"],
        dependencies=guidance.dependencies,
    )


class ParticipantTurnIngress:
    """Commit normalized response evidence and its Analyzer job atomically."""

    def __init__(
        self,
        foundation: WorkshopFoundationService,
        *,
        case_id: UUID,
        session_id: UUID,
        actor_id: UUID,
    ) -> None:
        self.foundation = foundation
        self.case_id = case_id
        self.session_id = session_id
        self.actor_id = actor_id

    def submit_typed(self, value: SubmitTypedResponseIntent) -> ParticipantTurnReceipt:
        return self.commit(
            CommittedParticipantTurn(
                client_submission_id=value.client_submission_id,
                question_id=value.question_id,
                expected_question_version=value.expected_question_version,
                normalized_text=value.text,
                correction_of_response_id=value.correction_of_response_id,
                edit_target=value.edit_target,
                input_channel=InputChannel.CHAT,
                channel_confirmation_receipt_id=None,
            )
        )

    def commit(self, value: CommittedParticipantTurn) -> ParticipantTurnReceipt:
        # Current V0 has exactly one admitted adapter.  Reject future-channel
        # values before opening the Foundation transaction.
        if (
            value.input_channel is not InputChannel.CHAT
            or value.channel_confirmation_receipt_id is not None
        ):
            raise ParticipantTurnError("INPUT_CHANNEL_NOT_ENABLED")
        fingerprint = _canonical_fingerprint(value)
        responses = TASK28_RUNTIME_TABLES["workshop_typed_responses"]
        runway = V0_RUNTIME_TABLES["workshop_runway_items"]
        transcripts = WORKSHOP_PROTOCOL_TABLES["workshop_final_transcripts"]
        jobs = V0_RUNTIME_TABLES["workshop_analyzer_jobs"]
        preparations = V0_RUNTIME_TABLES["workshop_preparations"]
        now = self.foundation.now()
        now_text = _instant(now)

        try:
            with self.foundation.engine.begin() as connection:
                replay = connection.execute(
                    select(responses).where(
                        responses.c.client_submission_id
                        == str(value.client_submission_id)
                    )
                ).mappings().one_or_none()
                if replay is not None:
                    if replay["request_fingerprint"] != fingerprint:
                        raise ParticipantTurnError("IDEMPOTENCY_CONFLICT")
                    return self._receipt(replay, replayed=True, connection=connection)

                preparation = connection.execute(
                    select(preparations).where(
                        preparations.c.case_id == str(self.case_id)
                    )
                ).mappings().one()
                if preparation["phase"] != "READY" or preparation["workshop_complete_at"]:
                    raise ParticipantTurnError("WORKSHOP_LOCKED")
                participant = connection.execute(
                    select(case_participants.c.actor_id).where(
                        case_participants.c.case_id == str(self.case_id),
                        case_participants.c.actor_id == str(self.actor_id),
                    )
                ).scalar_one_or_none()
                if participant is None:
                    raise ParticipantTurnError("AUTHORITY_FAILED")
                submission_status = _turn_submission_status(
                    connection, jobs=jobs, case_id=self.case_id
                )
                if submission_status == "ANALYSIS_FAILED":
                    raise ParticipantTurnError("TURN_ANALYSIS_FAILED")
                if submission_status == "ANALYSIS_PENDING":
                    raise ParticipantTurnError("TURN_ANALYSIS_IN_PROGRESS")

                prior_revision = self.foundation._case_revision(connection, self.case_id)
                correction = None
                if value.correction_of_response_id is not None:
                    correction = connection.execute(
                        select(responses).where(
                            responses.c.response_id
                            == str(value.correction_of_response_id),
                            responses.c.case_id == str(self.case_id),
                            responses.c.session_id == str(self.session_id),
                        )
                    ).mappings().one_or_none()
                    if correction is None:
                        raise ParticipantTurnError("CORRECTION_BINDING_FAILED")
                    latest = connection.execute(
                        select(responses)
                        .where(
                            responses.c.case_id == str(self.case_id),
                            responses.c.session_id == str(self.session_id),
                            responses.c.turn_sequence == correction["turn_sequence"],
                        )
                        .order_by(responses.c.response_version.desc())
                        .limit(1)
                    ).mappings().one()
                    if latest["response_id"] != correction["response_id"]:
                        raise ParticipantTurnError("CORRECTION_BINDING_FAILED")
                    if (
                        correction["question_id"] != str(value.question_id)
                        or correction["question_version"]
                        != value.expected_question_version
                    ):
                        raise ParticipantTurnError("QUESTION_BINDING_FAILED")
                    question = FoundationAdmittedQuestion.model_validate_json(
                        correction["question_snapshot_json"]
                    )
                    turn_sequence = correction["turn_sequence"]
                    response_version = correction["response_version"] + 1
                else:
                    guidance = self.foundation.current_admitted_guidance(self.case_id)
                    if guidance is None:
                        raise ParticipantTurnError("NO_QUESTION_AVAILABLE")
                    row = connection.execute(
                        select(runway).where(
                            runway.c.case_id == str(self.case_id),
                            runway.c.guidance_id == str(guidance.guidance_id),
                            runway.c.question_id == str(value.question_id),
                            runway.c.question_version
                            == value.expected_question_version,
                            runway.c.status == "AVAILABLE",
                        )
                    ).mappings().one_or_none()
                    if row is None:
                        raise ParticipantTurnError("QUESTION_BINDING_FAILED")
                    # Only the first ordered ASKABLE question is current.
                    first_id = connection.execute(
                        select(runway.c.question_id)
                        .where(
                            runway.c.case_id == str(self.case_id),
                            runway.c.guidance_id == str(guidance.guidance_id),
                            runway.c.status == "AVAILABLE",
                        )
                        .order_by(runway.c.position)
                        .limit(1)
                    ).scalar_one_or_none()
                    if first_id != str(value.question_id):
                        raise ParticipantTurnError("QUESTION_BINDING_FAILED")
                    question = _question_snapshot(guidance, dict(row))
                    max_turn = connection.execute(
                        select(func.max(responses.c.turn_sequence)).where(
                            responses.c.case_id == str(self.case_id),
                            responses.c.session_id == str(self.session_id),
                        )
                    ).scalar_one()
                    turn_sequence = 1 if max_turn is None else max_turn + 1
                    response_version = 1

                response_id = uuid5(
                    RESPONSE_NAMESPACE,
                    f"{self.case_id}:transcript:{value.client_submission_id}",
                )
                content_hash = hashlib.sha256(
                    value.normalized_text.encode("utf-8")
                ).hexdigest()
                source_artifact_id = uuid5(
                    RESPONSE_NAMESPACE,
                    f"{self.case_id}:transcript-turn:{turn_sequence}",
                )
                source_ref = SourceRef(
                    artifact_id=source_artifact_id,
                    version=response_version,
                    content_hash=content_hash,
                    location=JsonPointer(pointer="/normalized_text"),
                )
                physical_sequence = connection.execute(
                    select(func.max(transcripts.c.sequence_number)).where(
                        transcripts.c.case_id == str(self.case_id),
                        transcripts.c.session_id == str(self.session_id),
                    )
                ).scalar_one()
                physical_sequence = 1 if physical_sequence is None else physical_sequence + 1
                trigger_revision = prior_revision + 1
                job_id = _job_identity(
                    session_id=self.session_id,
                    response_id=response_id,
                    trigger_case_revision=trigger_revision,
                )
                correlation_id = uuid5(RESPONSE_NAMESPACE, f"{response_id}:correlation")
                transcript_event = c.TranscriptFinalizedEvent(
                    protocol_version=c.PROTOCOL_VERSION,
                    event_type="TRANSCRIPT_FINALIZED",
                    event_id=response_id,
                    case_id=self.case_id,
                    session_id=self.session_id,
                    correlation_id=correlation_id,
                    causation_id=None,
                    event_sequence=trigger_revision,
                    observed_case_revision=prior_revision,
                    occurred_at=now,
                    producer="VOICE",
                    turn_id=uuid5(RESPONSE_NAMESPACE, f"{self.case_id}:turn:{turn_sequence}"),
                    transcript_artifact_id=source_artifact_id,
                    transcript_version=response_version,
                    transcript_hash=transcript_hash(value.normalized_text),
                    actor=c.TranscriptActor.PM,
                    speaker_actor_id=self.actor_id,
                    speaker_attribution_method=c.SpeakerAttributionMethod.VERBAL_SELF_ASSERTION,
                    sequence_number=physical_sequence,
                    text=value.normalized_text,
                )
                connection.execute(
                    insert(source_artifacts).values(
                        artifact_id=str(source_artifact_id),
                        version=response_version,
                        case_id=str(self.case_id),
                        type="WORKSHOP_TRANSCRIPT",
                        media_type="application/json",
                        canonical_locator=f"/workshop/transcripts/{turn_sequence}/{response_version}",
                        content_hash=content_hash,
                        registered_at=now,
                    )
                )
                connection.execute(
                    insert(transcripts).values(
                        event_id=str(response_id),
                        case_id=str(self.case_id),
                        session_id=str(self.session_id),
                        sequence_number=physical_sequence,
                        transcript_hash=transcript_event.transcript_hash,
                        event_json=transcript_event.model_dump_json(),
                        recorded_at=now_text,
                    )
                )
                row_values = dict(
                    response_id=str(response_id),
                    case_id=str(self.case_id),
                    session_id=str(self.session_id),
                    question_id=str(value.question_id),
                    question_version=value.expected_question_version,
                    turn_sequence=turn_sequence,
                    response_version=response_version,
                    normalized_text=value.normalized_text,
                    content_hash=content_hash,
                    final_source_ref_json=source_ref.model_dump_json(),
                    client_submission_id=str(value.client_submission_id),
                    request_fingerprint=fingerprint,
                    correction_of_response_id=(
                        None
                        if value.correction_of_response_id is None
                        else str(value.correction_of_response_id)
                    ),
                    edit_target_json=(
                        None if value.edit_target is None else value.edit_target.model_dump_json()
                    ),
                    input_channel=value.input_channel.value,
                    channel_confirmation_receipt_id=None,
                    question_snapshot_json=question.model_dump_json(),
                    transcript_event_id=str(response_id),
                    created_at=now_text,
                )
                connection.execute(insert(responses).values(**row_values))
                connection.execute(
                    insert(jobs).values(
                        job_id=str(job_id),
                        case_id=str(self.case_id),
                        session_id=str(self.session_id),
                        operation=c.AnalyzerOperation.TURN_ANALYSIS.value,
                        subject_id=str(response_id),
                        dedupe_key=f"turn-analysis:{self.case_id}:{response_id}",
                        priority=10,
                        state="ANALYSIS_PENDING",
                        provider_request_id=f"analyzer-job:{str(job_id).lower()}",
                        request_json=None,
                        candidate_json=None,
                        admission_receipt_json=None,
                        attempt_count=0,
                        lease_owner=None,
                        lease_expires_at=None,
                        available_at=now_text,
                        last_error_code=None,
                        created_at=now_text,
                        updated_at=now_text,
                    )
                )
                if correction is None:
                    consumed = connection.execute(
                        update(runway)
                        .where(
                            runway.c.case_id == str(self.case_id),
                            runway.c.guidance_id == str(guidance.guidance_id),
                            runway.c.question_id == str(value.question_id),
                            runway.c.question_version == value.expected_question_version,
                            runway.c.status == "AVAILABLE",
                        )
                        .values(status="ASKED", consumed_at=now_text)
                    )
                    if consumed.rowcount != 1:
                        raise ParticipantTurnError("QUESTION_BINDING_FAILED")
                    self.foundation._consume_runway_and_schedule_guidance(
                        connection,
                        self.case_id,
                        self.session_id,
                        now_text,
                        consume_question=False,
                    )
                self.foundation._advance_revision(connection, self.case_id, prior_revision)
                command_id = uuid5(RESPONSE_NAMESPACE, f"{response_id}:participant-turn")
                connection.execute(
                    insert(audit_events).values(
                        event_id=str(uuid5(RESPONSE_NAMESPACE, f"{command_id}:audit")),
                        case_id=str(self.case_id),
                        case_sequence=trigger_revision,
                        command_id=str(command_id),
                        command_name="commit_participant_turn",
                        command_fingerprint=fingerprint,
                        actor=str(self.actor_id),
                        occurred_at=now,
                        target_ids=json.dumps([str(response_id), str(source_artifact_id), str(job_id)]),
                        before_case_revision=prior_revision,
                        after_case_revision=trigger_revision,
                        metadata=json.dumps(
                            {
                                "input_channel": "CHAT",
                                "question_id": str(value.question_id),
                                "question_version": value.expected_question_version,
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        result=json.dumps(
                            {"response_id": str(response_id), "analyzer_job_id": str(job_id)},
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    )
                )
                return self._receipt(row_values, replayed=False, connection=connection)
        except IntegrityError as exc:
            raise ParticipantTurnError("IDEMPOTENCY_CONFLICT") from exc
        except FoundationProtocolError as exc:
            if exc.code is c.FoundationRejectionCode.STALE_STATE:
                raise ParticipantTurnError("TURN_ANALYSIS_IN_PROGRESS") from exc
            raise

    @staticmethod
    def _receipt(row: Any, *, replayed: bool, connection) -> ParticipantTurnReceipt:
        jobs = V0_RUNTIME_TABLES["workshop_analyzer_jobs"]
        job = connection.execute(
            select(jobs).where(jobs.c.subject_id == row["response_id"])
        ).mappings().one()
        snapshot = TypedResponseSnapshot(
            response_id=UUID(row["response_id"]),
            session_id=UUID(row["session_id"]),
            question_id=UUID(row["question_id"]),
            question_version=row["question_version"],
            turn_sequence=row["turn_sequence"],
            response_version=row["response_version"],
            normalized_text=row["normalized_text"],
            content_hash=row["content_hash"],
            final_source_ref=SourceRef.model_validate_json(row["final_source_ref_json"]),
            client_submission_id=UUID(row["client_submission_id"]),
            correction_of_response_id=(
                None
                if row["correction_of_response_id"] is None
                else UUID(row["correction_of_response_id"])
            ),
            input_channel=InputChannel(row["input_channel"]),
            channel_confirmation_receipt_id=(
                None
                if row["channel_confirmation_receipt_id"] is None
                else UUID(row["channel_confirmation_receipt_id"])
            ),
            created_at=_parse_instant(row["created_at"]),
        )
        return ParticipantTurnReceipt(
            snapshot=snapshot,
            analyzer_job_id=UUID(job["job_id"]),
            recovery_code=(
                "ANALYSIS_FAILED" if job["state"] == "FAILED" else "ANALYSIS_QUEUED"
            ),
            replayed=replayed,
        )


class WorkshopConversationProjector:
    """Rebuild the shared channel context solely from durable state."""

    def __init__(
        self,
        foundation: WorkshopFoundationService,
        *,
        case_id: UUID,
        session_id: UUID,
    ) -> None:
        self.foundation = foundation
        self.case_id = case_id
        self.session_id = session_id

    def project(self) -> WorkshopConversationContext:
        responses = TASK28_RUNTIME_TABLES["workshop_typed_responses"]
        jobs = V0_RUNTIME_TABLES["workshop_analyzer_jobs"]
        actions = TASK28_RUNTIME_TABLES["workshop_visual_proposal_actions"]
        views = WORKSHOP_PROTOCOL_TABLES["workshop_decision_views"]
        with self.foundation.engine.connect() as connection:
            rows = connection.execute(
                select(responses)
                .where(
                    responses.c.case_id == str(self.case_id),
                    responses.c.session_id == str(self.session_id),
                )
                .order_by(responses.c.turn_sequence, responses.c.response_version)
            ).mappings().all()
            view_rows = connection.execute(
                select(views)
                .where(views.c.case_id == str(self.case_id))
                .order_by(views.c.generated_at, views.c.view_id)
            ).mappings().all()
            action_rows = connection.execute(
                select(actions)
                .where(actions.c.case_id == str(self.case_id))
                .order_by(actions.c.action_sequence)
            ).mappings().all()
            turn_submission_status = _turn_submission_status(
                connection, jobs=jobs, case_id=self.case_id
            )
        committed = tuple(
            CommittedQuestionResponseTurn(
                question=FoundationAdmittedQuestion.model_validate_json(
                    row["question_snapshot_json"]
                ),
                response=TypedResponseSnapshot(
                    response_id=UUID(row["response_id"]),
                    session_id=UUID(row["session_id"]),
                    question_id=UUID(row["question_id"]),
                    question_version=row["question_version"],
                    turn_sequence=row["turn_sequence"],
                    response_version=row["response_version"],
                    normalized_text=row["normalized_text"],
                    content_hash=row["content_hash"],
                    final_source_ref=SourceRef.model_validate_json(
                        row["final_source_ref_json"]
                    ),
                    client_submission_id=UUID(row["client_submission_id"]),
                    correction_of_response_id=(
                        None
                        if row["correction_of_response_id"] is None
                        else UUID(row["correction_of_response_id"])
                    ),
                    input_channel=InputChannel(row["input_channel"]),
                    channel_confirmation_receipt_id=(
                        None
                        if row["channel_confirmation_receipt_id"] is None
                        else UUID(row["channel_confirmation_receipt_id"])
                    ),
                    created_at=_parse_instant(row["created_at"]),
                ),
            )
            for row in rows
        )
        guidance = self.foundation.current_admitted_guidance(self.case_id)
        runway_value = self.foundation.runway_projection(self.case_id)
        runway_questions = (
            ()
            if guidance is None
            else tuple(
                _question_snapshot(guidance, row)
                for row in runway_value["questions"]
            )
        )
        action_by_view = {row["proposal_ref"]: row for row in action_rows}
        proposals: list[ProposalStatusProjection] = []
        for index, row in enumerate(view_rows, start=1):
            view = c.DecisionBatchReviewView.model_validate_json(row["view_json"])
            proposal_ref = str(view.view_id)
            action = action_by_view.get(proposal_ref)
            action_status = (
                ProposalInteractionStatus.PENDING
                if action is None
                else ProposalInteractionStatus(
                    json.loads(action["result_json"])["status"]
                )
            )
            status = (
                ProposalInteractionStatus.SUPERSEDED
                if index < len(view_rows)
                and action_status
                in {
                    ProposalInteractionStatus.PENDING,
                    ProposalInteractionStatus.EDIT_REQUESTED,
                }
                else action_status
            )
            proposals.append(
                ProposalStatusProjection(
                    binding=ProposalBinding(
                        proposal_ref=proposal_ref,
                        proposal_version=index,
                        base_case_revision=view.based_on_case_revision,
                        payload_hash=view.view_hash.removeprefix("sha256:"),
                    ),
                    status=status,
                    view=view,
                )
            )
        proposals.sort(
            key=lambda item: (item.binding.proposal_ref, item.binding.proposal_version)
        )
        preparation = self.foundation.preparation_projection(self.case_id)
        completion = self.foundation.workshop_completion_projection(self.case_id)
        if preparation["phase"] == "FAILED":
            workshop_state = "BLOCKED"
        elif completion["state"] == "COMPLETE":
            workshop_state = "COMPLETED"
        elif completion["state"] != "ACTIVE":
            workshop_state = "FINISHING"
        elif preparation["phase"] == "READY":
            workshop_state = "ACTIVE"
        else:
            workshop_state = "NOT_STARTED"
        if completion["state"] == "COMPLETE":
            conversation_phase = "COMPLETE"
            completion_status = "HANDOFF_READY"
        elif completion["state"] != "ACTIVE":
            conversation_phase = "WORKSHOP"
            failed = any(
                item["state"] == "FAILED"
                for item in self.foundation.analyzer_jobs(self.case_id)
            )
            completion_status = "FINISH_FAILED" if failed else "FINISHING_ANALYSIS"
        else:
            conversation_phase = "WORKSHOP"
            completion_status = None
        return WorkshopConversationContext(
            session_id=self.session_id,
            case_revision=self.foundation.case_revision(self.case_id),
            workshop_state=workshop_state,
            conversation_phase=conversation_phase,
            session_card=self.foundation.voice_session_card(self.case_id),
            committed_turns=committed,
            question_runway=QuestionRunway(
                questions=runway_questions,
                runway_depth=len(runway_questions),
            ),
            turn_submission_status=turn_submission_status,
            proposal_statuses=tuple(proposals),
            completion_status=completion_status,
            generated_at=self.foundation.now(),
        )


class VisualProposalActionService:
    """Map explicit visual controls to the existing Foundation command seam."""

    def __init__(self, projector: WorkshopConversationProjector, orchestrator) -> None:
        self.projector = projector
        self.orchestrator = orchestrator
        self.foundation = projector.foundation
        self.case_id = projector.case_id

    def _current(self, binding: ProposalBinding) -> ProposalStatusProjection:
        context = self.projector.project()
        current = next(
            (
                item
                for item in context.proposal_statuses
                if item.binding.proposal_ref == binding.proposal_ref
                and item.binding.proposal_version == binding.proposal_version
            ),
            None,
        )
        if current is None:
            raise ParticipantTurnError("NO_CURRENT_PROPOSAL")
        if current.binding != binding or binding.base_case_revision > context.case_revision:
            raise ParticipantTurnError("STALE_PROPOSAL")
        return current

    def _replay(self, value: VisualProposalActionIntent, action: str):
        table = TASK28_RUNTIME_TABLES["workshop_visual_proposal_actions"]
        fingerprint = _canonical_fingerprint(
            {"action": action, "binding": value.binding}
        )
        with self.foundation.engine.connect() as connection:
            row = connection.execute(
                select(table).where(table.c.client_action_id == str(value.client_action_id))
            ).mappings().one_or_none()
        if row is None:
            return fingerprint, None
        if row["request_fingerprint"] != fingerprint:
            raise ParticipantTurnError("IDEMPOTENCY_CONFLICT")
        result = json.loads(row["result_json"])
        return fingerprint, VisualProposalActionReceipt(
            client_action_id=value.client_action_id,
            binding=value.binding,
            status=ProposalInteractionStatus(result["status"]),
            replayed=True,
        )

    def _store(
        self,
        value: VisualProposalActionIntent,
        action: str,
        status: ProposalInteractionStatus,
        fingerprint: str,
    ) -> VisualProposalActionReceipt:
        table = TASK28_RUNTIME_TABLES["workshop_visual_proposal_actions"]
        result = {"status": status.value}
        try:
            with self.foundation.engine.begin() as connection:
                max_sequence = connection.execute(
                    select(func.max(table.c.action_sequence)).where(
                        table.c.case_id == str(self.case_id)
                    )
                ).scalar_one()
                connection.execute(
                    insert(table).values(
                        client_action_id=str(value.client_action_id),
                        case_id=str(self.case_id),
                        proposal_ref=value.binding.proposal_ref,
                        proposal_version=value.binding.proposal_version,
                        action_sequence=1 if max_sequence is None else max_sequence + 1,
                        action=action,
                        binding_json=value.binding.model_dump_json(),
                        request_fingerprint=fingerprint,
                        result_json=json.dumps(result, sort_keys=True, separators=(",", ":")),
                        created_at=_instant(self.foundation.now()),
                    )
                )
        except IntegrityError as exc:
            raise ParticipantTurnError("IDEMPOTENCY_CONFLICT") from exc
        return VisualProposalActionReceipt(
            client_action_id=value.client_action_id,
            binding=value.binding,
            status=status,
            replayed=False,
        )

    def edit(self, value: VisualProposalActionIntent) -> VisualProposalActionReceipt:
        fingerprint, replay = self._replay(value, "EDIT")
        if replay is not None:
            return replay
        current = self._current(value.binding)
        if current.status is not ProposalInteractionStatus.PENDING:
            raise ParticipantTurnError("STALE_PROPOSAL")
        return self._store(
            value, "EDIT", ProposalInteractionStatus.EDIT_REQUESTED, fingerprint
        )

    def reject(self, value: VisualProposalActionIntent) -> VisualProposalActionReceipt:
        fingerprint, replay = self._replay(value, "REJECT")
        if replay is not None:
            return replay
        current = self._current(value.binding)
        if current.status not in {
            ProposalInteractionStatus.PENDING,
            ProposalInteractionStatus.EDIT_REQUESTED,
        }:
            raise ParticipantTurnError("STALE_PROPOSAL")
        return self._store(
            value, "REJECT", ProposalInteractionStatus.REJECTED, fingerprint
        )

    def confirm(self, value: VisualProposalActionIntent) -> VisualProposalActionReceipt:
        fingerprint, replay = self._replay(value, "CONFIRM")
        if replay is not None:
            return replay
        current = self._current(value.binding)
        if current.status is not ProposalInteractionStatus.PENDING:
            raise ParticipantTurnError("STALE_PROPOSAL")
        typed = TASK28_RUNTIME_TABLES["workshop_typed_responses"]
        with self.foundation.engine.connect() as connection:
            latest_response_id = connection.execute(
                select(typed.c.response_id)
                .where(typed.c.case_id == str(self.case_id))
                .order_by(typed.c.turn_sequence.desc(), typed.c.response_version.desc())
                .limit(1)
            ).scalar_one_or_none()
        if latest_response_id is None:
            raise ParticipantTurnError("NO_COMMITTED_RESPONSE")
        actor_id = self.foundation.case_actor(self.case_id, "PM")
        authentication = c.EnterpriseSsoAuthentication(
            authentication_method=c.ActorAuthenticationMethod.ENTERPRISE_SSO,
            assurance_level=c.AssuranceLevel.VERIFIED,
            actor_id=actor_id,
            identity_provider="LOCAL_DEMO",
            external_subject=str(actor_id),
            authentication_event_id=uuid5(
                RESPONSE_NAMESPACE, f"{value.client_action_id}:visual-authentication"
            ),
        )
        selections = tuple(
            c.VoiceConfirmationSelectionItemCandidate(
                handle=item.handle,
                action=c.ConfirmationAction.CONFIRM,
                revision_span=None,
            )
            for item in current.view.items
        )
        self.orchestrator.apply_review_selection(
            operation_key=f"visual-confirm-{value.client_action_id}",
            response_transcript_event_id=UUID(latest_response_id),
            selections=selections,
            authentication=authentication,
        )
        return self._store(
            value, "CONFIRM", ProposalInteractionStatus.COMMITTED, fingerprint
        )


@dataclass(frozen=True)
class FakeFutureVoiceContextConsumer:
    """Read-only proof seam; deliberately has no transcript or audio method."""

    projector: WorkshopConversationProjector

    def activate(self) -> WorkshopConversationContext:
        return self.projector.project()

    def refresh_before_future_final_transcript(self) -> WorkshopConversationContext:
        return self.projector.project()
