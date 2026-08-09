from __future__ import annotations

import re
from datetime import timezone
from hashlib import sha256
from typing import Protocol
from uuid import UUID, uuid5

from specops_workflow import Clock, WorkflowService
from specops_workflow.canonical import sha256 as canonical_sha256
from specops_workflow.enums import SourceArtifactType
from specops_workflow.errors import DomainError, ErrorCode
from specops_workflow.models import (
    AmbiguityFindingResult,
    ApproveSpecPackageItemCommand,
    CreateReviewRequestCommand,
    CreateSpecPackageV2Command,
    ItemGovernanceResult,
    LineRange,
    MarkSpecPackageItemReadyCommand,
    QueryOne,
    RecordItemAmbiguityFindingCommand,
    RegisterSourceArtifactCommand,
    ReviseSpecPackageV2Command,
    ReviewRequestResult,
    SourceArtifactIdentity,
    SourceArtifactResult,
    SourceRef,
    SpecPackageResult,
)

from ..contracts import (
    CallState,
    ConversationPhase,
    FoundationOutbox,
    OutboxStatus,
    RecoveryView,
    TranscriptSnapshot,
    WorkshopSession,
    WorkshopState,
)
from ..sessions import WorkshopStore
from ..telemetry import SpanOutcome, TelemetryStage


TRANSCRIPT_NAMESPACE = UUID("f620329f-45dc-5126-a43f-5e4e3e7a3769")
FOUNDATION_OUTBOX_NAMESPACE = UUID("cfe59799-88f6-50d2-8962-96f09a159a17")
FOUNDATION_COMMANDS = {
    "register_source_artifact": (RegisterSourceArtifactCommand, SourceArtifactResult),
    "create_spec_package": (CreateSpecPackageV2Command, SpecPackageResult),
    "revise_spec_package": (ReviseSpecPackageV2Command, SpecPackageResult),
    "record_ambiguity_finding": (RecordItemAmbiguityFindingCommand, AmbiguityFindingResult),
    "mark_spec_package_item_ready": (MarkSpecPackageItemReadyCommand, ItemGovernanceResult),
    "approve_spec_package_item": (ApproveSpecPackageItemCommand, ItemGovernanceResult),
    "create_review_request": (CreateReviewRequestCommand, ReviewRequestResult),
}


class FoundationGateway(Protocol):
    def register_source_artifact(self, command: RegisterSourceArtifactCommand): ...
    def get_workflow_view(self, query: QueryOne): ...


class WorkshopCoordinator:
    def __init__(
        self,
        store: WorkshopStore,
        foundation: FoundationGateway | WorkflowService,
        *,
        clock: Clock,
        telemetry=None,
    ) -> None:
        self.store = store
        self.foundation = foundation
        self.clock = clock
        self.telemetry = telemetry

    def start_session(self, session_id: UUID, *, case_id: UUID, pm_actor_id: UUID) -> WorkshopSession:
        now = self.clock.now().astimezone(timezone.utc)
        revision = self.foundation.get_workflow_view(QueryOne(case_id=case_id, acting_actor_id=pm_actor_id)).revision
        return self.store.create_session(WorkshopSession(
            session_id=session_id, case_id=case_id, pm_actor_id=pm_actor_id,
            workshop_state=WorkshopState.ACTIVE, conversation_phase=ConversationPhase.WORKSHOP,
            call_state=CallState.READY, last_activity_at=now, expected_foundation_revision=revision,
            created_at=now, updated_at=now,
        ))

    def enqueue_final_turn(
        self,
        session_id: UUID,
        *,
        turn_sequence: int,
        text: str,
        provider_request_id: str,
        correction_of_version: int | None = None,
    ) -> FoundationOutbox:
        session = self.store.get_session(session_id)
        if session is None or session.revision_locked:
            raise ValueError("revision-locked Workshop cannot accept final turns")
        normalized = re.sub(r"\s+", " ", text).strip()
        if not normalized:
            raise ValueError("provider-final transcript cannot be empty")
        delivered = self.store.snapshot_for_provider_request(session_id, provider_request_id)
        if delivered is not None:
            if (
                delivered.turn_sequence != turn_sequence
                or delivered.normalized_text != normalized
                or delivered.correction_of_version != correction_of_version
            ):
                raise ValueError("provider request identity conflict")
            return self.store.outbox_for_action(
                f"transcript:{session_id}:{delivered.turn_sequence}:{delivered.version}"
            )
        if self.store.replayable_outbox(session_id):
            raise ValueError("a final transcript commit is already unresolved")
        latest = self.store.latest_turn_version(session_id, turn_sequence)
        version = 1 if latest is None else latest + 1
        if correction_of_version != (None if latest is None else latest):
            raise ValueError("correction must name the latest final transcript version")
        now = self.clock.now().astimezone(timezone.utc)
        artifact_id = uuid5(TRANSCRIPT_NAMESPACE, f"{session_id}:turn:{turn_sequence}")
        content_hash = sha256(normalized.encode("utf-8")).hexdigest()
        source_ref = SourceRef(
            artifact_id=artifact_id, version=version, content_hash=content_hash,
            location=LineRange(start=1, end=1),
        )
        command_id = uuid5(TRANSCRIPT_NAMESPACE, f"{session_id}:register:{turn_sequence}:{version}")
        command = RegisterSourceArtifactCommand(
            command_id=command_id, case_id=session.case_id, acting_actor_id=session.pm_actor_id,
            expected_case_revision=session.expected_foundation_revision,
            identity=SourceArtifactIdentity(
                artifact_id=artifact_id, case_id=session.case_id, type=SourceArtifactType.WORKSHOP_TRANSCRIPT,
                version=version, media_type="text/plain",
                canonical_locator=f"/workshop/{session_id}/turn-{turn_sequence:08d}/version-{version:04d}.txt",
                content_hash=content_hash,
            ),
        )
        action_key = f"transcript:{session_id}:{turn_sequence}:{version}"
        outbox = FoundationOutbox(
            outbox_id=uuid5(TRANSCRIPT_NAMESPACE, f"outbox:{action_key}"), session_id=session_id,
            command_id=command_id, logical_action_key=action_key, command_name="register_source_artifact",
            command_fingerprint=canonical_sha256({"schema": "command-v1", "command_name": "register_source_artifact", "payload": command.model_dump(mode="python", exclude_none=False)}),
            command_json=command.model_dump_json(), expected_foundation_revision=session.expected_foundation_revision,
            status=OutboxStatus.PENDING, attempts=0, created_at=now, updated_at=now,
        )
        snapshot = TranscriptSnapshot(
            session_id=session_id, turn_sequence=turn_sequence, version=version, stable_artifact_id=artifact_id,
            normalized_text=normalized, content_hash=content_hash, final_source_ref=source_ref,
            provider_request_id=provider_request_id, correction_of_version=correction_of_version, created_at=now,
        )
        return self.store.enqueue_final(snapshot, outbox)[1]

    def dispatch(self, outbox_id: UUID):
        outbox = self.store.get_outbox(outbox_id)
        command_type, result_type = FOUNDATION_COMMANDS[outbox.command_name]
        if outbox.status == OutboxStatus.CONFIRMED:
            return result_type.model_validate_json(outbox.result_json)
        if outbox.status == OutboxStatus.REJECTED:
            raise ValueError("rejected outbox entry cannot be retried")
        command = command_type.model_validate_json(outbox.command_json)
        now = self.clock.now().astimezone(timezone.utc)
        span_id = None
        if self.telemetry is not None:
            span_id = self.telemetry.start(
                session_id=outbox.session_id,
                stage=TelemetryStage.FOUNDATION,
                operation_id=f"{outbox.outbox_id}:{outbox.attempts + 1}",
            )
        try:
            result = getattr(self.foundation, outbox.command_name)(command)
        except DomainError as exc:
            if exc.code == ErrorCode.STALE_ARTIFACT_BINDING:
                self.store.record_dispatch(outbox_id, status=OutboxStatus.REJECTED, now=now, lock_reason="FOUNDATION_REVISION_MISMATCH")
            else:
                self.store.record_dispatch(outbox_id, status=OutboxStatus.REJECTED, now=now, lock_reason="FOUNDATION_COMMAND_REJECTED")
            if span_id is not None:
                self.telemetry.finish(
                    span_id,
                    outcome=SpanOutcome.ERROR,
                    error_code=exc.code.value,
                )
            raise
        except Exception:
            self.store.record_dispatch(
                outbox_id, status=OutboxStatus.UNKNOWN, now=now,
                lock_reason="FOUNDATION_OUTCOME_UNKNOWN",
            )
            if span_id is not None:
                self.telemetry.finish(
                    span_id,
                    outcome=SpanOutcome.ERROR,
                    error_code="FOUNDATION_OUTCOME_UNKNOWN",
                )
            raise
        stored = result.stored_result if not result.mutated else result
        if not isinstance(stored, result_type):
            self.store.record_dispatch(outbox_id, status=OutboxStatus.REJECTED, now=now, lock_reason="FOUNDATION_RESULT_TYPE_MISMATCH")
            if span_id is not None:
                self.telemetry.finish(
                    span_id,
                    outcome=SpanOutcome.ERROR,
                    error_code="FOUNDATION_RESULT_TYPE_MISMATCH",
                )
            raise TypeError("foundation returned the wrong typed result")
        self.store.record_dispatch(
            outbox_id, status=OutboxStatus.CONFIRMED, now=now,
            result_json=stored.model_dump_json(), confirmed_revision=stored.receipt.revision,
        )
        if span_id is not None:
            self.telemetry.finish(span_id, outcome=SpanOutcome.OK)
        return stored

    def enqueue_foundation_command(
        self,
        session_id: UUID,
        *,
        logical_action_key: str,
        command_name: str,
        command,
    ) -> FoundationOutbox:
        if command_name not in FOUNDATION_COMMANDS:
            raise ValueError("unsupported foundation outbox command")
        now = self.clock.now().astimezone(timezone.utc)
        fingerprint = canonical_sha256({
            "schema": "command-v1",
            "command_name": command_name,
            "payload": command.model_dump(mode="python", exclude_none=False),
        })
        outbox = FoundationOutbox(
            outbox_id=uuid5(FOUNDATION_OUTBOX_NAMESPACE, f"outbox:{logical_action_key}"),
            session_id=session_id,
            command_id=command.command_id,
            logical_action_key=logical_action_key,
            command_name=command_name,
            command_fingerprint=fingerprint,
            command_json=command.model_dump_json(),
            expected_foundation_revision=command.expected_case_revision,
            status=OutboxStatus.PENDING,
            attempts=0,
            created_at=now,
            updated_at=now,
        )
        return self.store.enqueue_command(outbox)

    def commit_foundation_command(self, session_id: UUID, **values):
        outbox = self.enqueue_foundation_command(session_id, **values)
        return self.dispatch(outbox.outbox_id)

    def commit_final_turn(self, session_id: UUID, **values):
        outbox = self.enqueue_final_turn(session_id, **values)
        return self.dispatch(outbox.outbox_id)

    def recover(self, session_id: UUID) -> RecoveryView:
        for outbox in self.store.replayable_outbox(session_id):
            self.dispatch(outbox.outbox_id)
        session = self.store.get_session(session_id)
        current = self.foundation.get_workflow_view(QueryOne(case_id=session.case_id, acting_actor_id=session.pm_actor_id)).revision
        if current != session.expected_foundation_revision:
            self.store.lock_session(session_id, "UNEXPLAINED_FOUNDATION_REVISION", self.clock.now())
            session = self.store.get_session(session_id)
        return RecoveryView(
            session=session,
            final_transcripts=self.store.latest_snapshots(session_id),
            pending_outbox_ids=tuple(value.outbox_id for value in self.store.replayable_outbox(session_id)),
        )
