from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import Column, Integer, MetaData, Table, Text, UniqueConstraint, create_engine, event, insert, literal_column, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

from specops_workflow.models import SourceRef

from ..contracts import (
    AnalyzerCheckpoint,
    AnalyzerCheckpointStage,
    AnalyzerCheckpointStatus,
    AnalyzerFailureKind,
    AnalyzerRecoveryAction,
    CallState,
    ConversationPhase,
    FoundationOutbox,
    OutboxStatus,
    PackageProposalRecord,
    ProposalStatus,
    TranscriptSnapshot,
    WorkshopSession,
    WorkshopState,
)
from ..telemetry import LatencySpan, SpanOutcome, TelemetryStage


metadata = MetaData()
sessions = Table(
    "workshop_sessions", metadata,
    Column("session_id", Text, primary_key=True),
    Column("case_id", Text, nullable=False),
    Column("pm_actor_id", Text, nullable=False),
    Column("workshop_state", Text, nullable=False),
    Column("conversation_phase", Text, nullable=False),
    Column("call_state", Text, nullable=False),
    Column("last_activity_at", Text, nullable=False),
    Column("expected_foundation_revision", Integer, nullable=False),
    Column("pending_proposal_ref", Text),
    Column("revision_locked", Integer, nullable=False),
    Column("revision_lock_reason", Text),
    Column("created_at", Text, nullable=False),
    Column("updated_at", Text, nullable=False),
)
transcript_snapshots = Table(
    "transcript_snapshots", metadata,
    Column("session_id", Text, primary_key=True),
    Column("turn_sequence", Integer, primary_key=True),
    Column("version", Integer, primary_key=True),
    Column("stable_artifact_id", Text, nullable=False),
    Column("normalized_text", Text, nullable=False),
    Column("content_hash", Text, nullable=False),
    Column("final_source_ref", Text, nullable=False),
    Column("provider_request_id", Text, nullable=False),
    Column("correction_of_version", Integer),
    Column("created_at", Text, nullable=False),
    UniqueConstraint("session_id", "stable_artifact_id", "version"),
    UniqueConstraint("session_id", "provider_request_id"),
)
foundation_outbox = Table(
    "foundation_outbox", metadata,
    Column("outbox_id", Text, primary_key=True),
    Column("session_id", Text, nullable=False),
    Column("command_id", Text, nullable=False, unique=True),
    Column("logical_action_key", Text, nullable=False, unique=True),
    Column("command_name", Text, nullable=False),
    Column("command_fingerprint", Text, nullable=False),
    Column("command_json", Text, nullable=False),
    Column("expected_foundation_revision", Integer, nullable=False),
    Column("status", Text, nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("result_json", Text),
    Column("created_at", Text, nullable=False),
    Column("updated_at", Text, nullable=False),
)
agent_interruptions = Table(
    "agent_interruptions", metadata,
    Column("session_id", Text, primary_key=True),
    Column("agent_turn_id", Text, primary_key=True),
    Column("provider_request_id", Text, nullable=False),
    Column("interrupted_at", Text, nullable=False),
)
analyzer_requests = Table(
    "analyzer_requests", metadata,
    Column("request_id", Text, primary_key=True),
    Column("session_id", Text, nullable=False),
    Column("turn_sequence", Integer, nullable=False),
    Column("effort", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("attempt_count", Integer, nullable=False),
    Column("created_at", Text, nullable=False),
    Column("updated_at", Text, nullable=False),
)
analyzer_checkpoints = Table(
    "analyzer_checkpoints", metadata,
    Column("request_id", Text, primary_key=True),
    Column("session_id", Text, nullable=False),
    Column("turn_sequence", Integer, nullable=False),
    Column("request_fingerprint", Text, nullable=False),
    Column("source_fingerprint", Text, nullable=False),
    Column("retrieval_fingerprint", Text, nullable=False),
    Column("selected_aliases_json", Text, nullable=False),
    Column("stage", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("failure_kind", Text),
    Column("recovery_action", Text),
    Column("proposal_ref", Text),
    Column("provider_call_count", Integer, nullable=False),
    Column("created_at", Text, nullable=False),
    Column("updated_at", Text, nullable=False),
)
package_proposals = Table(
    "package_proposals", metadata,
    Column("proposal_ref", Text, primary_key=True),
    Column("session_id", Text, nullable=False),
    Column("version", Integer, nullable=False),
    Column("base_foundation_revision", Integer, nullable=False),
    Column("analyzer_result_json", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("created_at", Text, nullable=False),
    Column("updated_at", Text, nullable=False),
)
latency_spans = Table(
    "latency_spans", metadata,
    Column("span_id", Text, primary_key=True),
    Column("session_id", Text, nullable=False),
    Column("stage", Text, nullable=False),
    Column("started_at", Text, nullable=False),
    Column("ended_at", Text),
    Column("duration_ms", Integer),
    Column("outcome", Text),
)


@event.listens_for(Engine, "connect")
def _sqlite_pragmas(connection, _record) -> None:
    if connection.__class__.__module__.startswith("sqlite3"):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def _instant(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


class WorkshopStore:
    def __init__(self, database_url: str) -> None:
        if not database_url.startswith("sqlite:///"):
            raise ValueError("Workshop v0 supports only a separate SQLite store")
        memory = database_url == "sqlite:///:memory:"
        self.engine = create_engine(
            database_url,
            future=True,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool if memory else None,
        )
        metadata.create_all(self.engine)
        with self.engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TRIGGER IF NOT EXISTS transcript_snapshots_no_update BEFORE UPDATE ON transcript_snapshots "
                "BEGIN SELECT RAISE(ABORT, 'TRANSCRIPT_APPEND_ONLY'); END"
            )
            connection.exec_driver_sql(
                "CREATE TRIGGER IF NOT EXISTS transcript_snapshots_no_delete BEFORE DELETE ON transcript_snapshots "
                "BEGIN SELECT RAISE(ABORT, 'TRANSCRIPT_APPEND_ONLY'); END"
            )

    def create_session(self, value: WorkshopSession) -> WorkshopSession:
        existing = self.get_session(value.session_id, required=False)
        if existing is not None:
            if existing.case_id != value.case_id or existing.pm_actor_id != value.pm_actor_id:
                raise ValueError("session identity conflict")
            return existing
        with self.engine.begin() as connection:
            connection.execute(insert(sessions).values(**self._session_values(value)))
        return value

    def get_session(self, session_id: UUID, *, required: bool = True) -> WorkshopSession | None:
        with self.engine.connect() as connection:
            row = connection.execute(select(sessions).where(sessions.c.session_id == str(session_id))).mappings().one_or_none()
        if row is None:
            if required:
                raise KeyError(session_id)
            return None
        return self._session(row)

    def enqueue_final(self, snapshot: TranscriptSnapshot, outbox: FoundationOutbox) -> tuple[TranscriptSnapshot, FoundationOutbox]:
        with self.engine.begin() as connection:
            current = connection.execute(
                select(transcript_snapshots).where(
                    transcript_snapshots.c.session_id == str(snapshot.session_id),
                    transcript_snapshots.c.turn_sequence == snapshot.turn_sequence,
                    transcript_snapshots.c.version == snapshot.version,
                )
            ).mappings().one_or_none()
            if current is not None:
                existing_snapshot = self._snapshot(current)
                existing_outbox = connection.execute(
                    select(foundation_outbox).where(foundation_outbox.c.logical_action_key == outbox.logical_action_key)
                ).mappings().one()
                if existing_snapshot != snapshot or existing_outbox["command_fingerprint"] != outbox.command_fingerprint:
                    raise ValueError("final transcript identity conflict")
                return existing_snapshot, self._outbox(existing_outbox)
            latest = connection.execute(
                select(transcript_snapshots.c.version).where(
                    transcript_snapshots.c.session_id == str(snapshot.session_id),
                    transcript_snapshots.c.turn_sequence == snapshot.turn_sequence,
                ).order_by(transcript_snapshots.c.version.desc()).limit(1)
            ).scalar_one_or_none()
            expected_version = 1 if latest is None else latest + 1
            if snapshot.version != expected_version:
                raise ValueError("transcript correction version must be contiguous")
            if snapshot.correction_of_version != (None if latest is None else latest):
                raise ValueError("correction must reference the latest immutable version")
            connection.execute(insert(transcript_snapshots).values(
                session_id=str(snapshot.session_id), turn_sequence=snapshot.turn_sequence, version=snapshot.version,
                stable_artifact_id=str(snapshot.stable_artifact_id), normalized_text=snapshot.normalized_text,
                content_hash=snapshot.content_hash, final_source_ref=snapshot.final_source_ref.model_dump_json(),
                provider_request_id=snapshot.provider_request_id, correction_of_version=snapshot.correction_of_version,
                created_at=_instant(snapshot.created_at),
            ))
            connection.execute(insert(foundation_outbox).values(**self._outbox_values(outbox)))
            connection.execute(update(sessions).where(sessions.c.session_id == str(snapshot.session_id)).values(
                last_activity_at=_instant(snapshot.created_at), updated_at=_instant(snapshot.created_at), call_state=CallState.COMMITTING.value,
            ))
        return snapshot, outbox

    def latest_turn_version(self, session_id: UUID, turn_sequence: int) -> int | None:
        with self.engine.connect() as connection:
            return connection.execute(select(transcript_snapshots.c.version).where(
                transcript_snapshots.c.session_id == str(session_id),
                transcript_snapshots.c.turn_sequence == turn_sequence,
            ).order_by(transcript_snapshots.c.version.desc()).limit(1)).scalar_one_or_none()

    def snapshot_for_provider_request(self, session_id: UUID, provider_request_id: str) -> TranscriptSnapshot | None:
        with self.engine.connect() as connection:
            row = connection.execute(select(transcript_snapshots).where(
                transcript_snapshots.c.session_id == str(session_id),
                transcript_snapshots.c.provider_request_id == provider_request_id,
            )).mappings().one_or_none()
        return None if row is None else self._snapshot(row)

    def outbox_for_action(self, logical_action_key: str) -> FoundationOutbox:
        with self.engine.connect() as connection:
            row = connection.execute(select(foundation_outbox).where(
                foundation_outbox.c.logical_action_key == logical_action_key
            )).mappings().one()
        return self._outbox(row)

    def find_outbox_for_action(self, logical_action_key: str) -> FoundationOutbox | None:
        with self.engine.connect() as connection:
            row = connection.execute(select(foundation_outbox).where(
                foundation_outbox.c.logical_action_key == logical_action_key
            )).mappings().one_or_none()
        return None if row is None else self._outbox(row)

    def enqueue_command(self, value: FoundationOutbox) -> FoundationOutbox:
        existing = self.find_outbox_for_action(value.logical_action_key)
        if existing is not None:
            if (
                existing.command_id != value.command_id
                or existing.command_name != value.command_name
                or existing.command_fingerprint != value.command_fingerprint
                or existing.command_json != value.command_json
            ):
                raise ValueError("foundation logical action identity conflict")
            return existing
        with self.engine.begin() as connection:
            connection.execute(insert(foundation_outbox).values(**self._outbox_values(value)))
        return value

    def get_outbox(self, outbox_id: UUID) -> FoundationOutbox:
        with self.engine.connect() as connection:
            row = connection.execute(select(foundation_outbox).where(foundation_outbox.c.outbox_id == str(outbox_id))).mappings().one()
        return self._outbox(row)

    def replayable_outbox(self, session_id: UUID) -> tuple[FoundationOutbox, ...]:
        with self.engine.connect() as connection:
            rows = connection.execute(select(foundation_outbox).where(
                foundation_outbox.c.session_id == str(session_id),
                foundation_outbox.c.status.in_((OutboxStatus.PENDING.value, OutboxStatus.UNKNOWN.value)),
            ).order_by(foundation_outbox.c.created_at, foundation_outbox.c.outbox_id)).mappings()
            return tuple(self._outbox(row) for row in rows)

    def record_dispatch(
        self,
        outbox_id: UUID,
        *,
        status: OutboxStatus,
        now: datetime,
        result_json: str | None = None,
        confirmed_revision: int | None = None,
        lock_reason: str | None = None,
    ) -> None:
        with self.engine.begin() as connection:
            row = connection.execute(select(foundation_outbox).where(foundation_outbox.c.outbox_id == str(outbox_id))).mappings().one()
            connection.execute(update(foundation_outbox).where(foundation_outbox.c.outbox_id == str(outbox_id)).values(
                status=status.value, attempts=row["attempts"] + 1, result_json=result_json, updated_at=_instant(now),
            ))
            session_values: dict[str, object] = {"updated_at": _instant(now)}
            if confirmed_revision is not None:
                session_values.update(
                    expected_foundation_revision=confirmed_revision,
                    call_state=CallState.LISTENING.value,
                    workshop_state=WorkshopState.ACTIVE.value,
                    revision_locked=0,
                    revision_lock_reason=None,
                )
            if lock_reason is not None:
                session_values.update(
                    revision_locked=1, revision_lock_reason=lock_reason,
                    workshop_state=WorkshopState.BLOCKED.value, call_state=CallState.DISCONNECTED.value,
                )
            connection.execute(update(sessions).where(sessions.c.session_id == row["session_id"]).values(**session_values))

    def lock_session(self, session_id: UUID, reason: str, now: datetime) -> None:
        with self.engine.begin() as connection:
            connection.execute(update(sessions).where(sessions.c.session_id == str(session_id)).values(
                revision_locked=1, revision_lock_reason=reason, workshop_state=WorkshopState.BLOCKED.value,
                call_state=CallState.DISCONNECTED.value, updated_at=_instant(now),
            ))

    def update_phase(
        self,
        session_id: UUID,
        *,
        call_state: CallState | None = None,
        conversation_phase: ConversationPhase | None = None,
        workshop_state: WorkshopState | None = None,
        now: datetime,
    ) -> WorkshopSession:
        current = self.get_session(session_id)
        values: dict[str, object] = {"updated_at": _instant(now), "last_activity_at": _instant(now)}
        if call_state is not None:
            values["call_state"] = call_state.value
        if conversation_phase is not None:
            values["conversation_phase"] = conversation_phase.value
        if workshop_state is not None:
            values["workshop_state"] = workshop_state.value
        with self.engine.begin() as connection:
            connection.execute(update(sessions).where(sessions.c.session_id == str(session_id)).values(**values))
        return self.get_session(session_id)

    def latest_snapshots(self, session_id: UUID) -> tuple[TranscriptSnapshot, ...]:
        snapshots = self.all_snapshots(session_id)
        latest: dict[int, TranscriptSnapshot] = {}
        for snapshot in snapshots:
            latest[snapshot.turn_sequence] = snapshot
        return tuple(latest[key] for key in sorted(latest))

    def all_snapshots(self, session_id: UUID) -> tuple[TranscriptSnapshot, ...]:
        with self.engine.connect() as connection:
            rows = list(connection.execute(select(transcript_snapshots).where(
                transcript_snapshots.c.session_id == str(session_id)
            ).order_by(transcript_snapshots.c.turn_sequence, transcript_snapshots.c.version)).mappings())
        return tuple(self._snapshot(row) for row in rows)

    def record_analyzer_attempt(
        self,
        *,
        request_id: UUID,
        session_id: UUID,
        turn_sequence: int,
        effort: str,
        status: str,
        attempt_count: int,
        now: datetime,
    ) -> None:
        with self.engine.begin() as connection:
            row = connection.execute(select(analyzer_requests).where(
                analyzer_requests.c.request_id == str(request_id)
            )).mappings().one_or_none()
            values = {
                "status": status,
                "attempt_count": attempt_count,
                "updated_at": _instant(now),
            }
            if row is None:
                connection.execute(insert(analyzer_requests).values(
                    request_id=str(request_id),
                    session_id=str(session_id),
                    turn_sequence=turn_sequence,
                    effort=effort,
                    created_at=_instant(now),
                    **values,
                ))
            else:
                if (
                    row["session_id"] != str(session_id)
                    or row["turn_sequence"] != turn_sequence
                    or row["effort"] != effort
                ):
                    raise ValueError("analyzer request identity conflict")
                connection.execute(update(analyzer_requests).where(
                    analyzer_requests.c.request_id == str(request_id)
                ).values(**values))

    def analyzer_checkpoint(self, request_id: UUID) -> AnalyzerCheckpoint | None:
        with self.engine.connect() as connection:
            row = connection.execute(select(analyzer_checkpoints).where(
                analyzer_checkpoints.c.request_id == str(request_id)
            )).mappings().one_or_none()
        return None if row is None else self._analyzer_checkpoint(row)

    def latest_analyzer_checkpoint(
        self, session_id: UUID
    ) -> AnalyzerCheckpoint | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(analyzer_checkpoints)
                .where(analyzer_checkpoints.c.session_id == str(session_id))
                .order_by(
                    analyzer_checkpoints.c.updated_at.desc(),
                    literal_column("rowid").desc(),
                )
                .limit(1)
            ).mappings().one_or_none()
        return None if row is None else self._analyzer_checkpoint(row)

    def save_analyzer_checkpoint(
        self, value: AnalyzerCheckpoint
    ) -> AnalyzerCheckpoint:
        values = self._analyzer_checkpoint_values(value)
        with self.engine.begin() as connection:
            existing = connection.execute(select(analyzer_checkpoints).where(
                analyzer_checkpoints.c.request_id == str(value.request_id)
            )).mappings().one_or_none()
            if existing is None:
                connection.execute(insert(analyzer_checkpoints).values(**values))
            else:
                self._assert_checkpoint_update(existing, value)
                connection.execute(update(analyzer_checkpoints).where(
                    analyzer_checkpoints.c.request_id == str(value.request_id)
                ).values(**values))
        return value

    def start_latency_span(self, value: LatencySpan) -> LatencySpan:
        if value.ended_at is not None or value.duration_ms is not None or value.outcome is not None:
            raise ValueError("a new latency span must be open")
        with self.engine.begin() as connection:
            existing = connection.execute(select(latency_spans).where(
                latency_spans.c.span_id == str(value.span_id)
            )).mappings().one_or_none()
            if existing is not None:
                hydrated = self._latency_span(existing)
                if hydrated != value:
                    raise ValueError("latency span identity conflict")
                return hydrated
            connection.execute(insert(latency_spans).values(
                span_id=str(value.span_id),
                session_id=str(value.session_id),
                stage=value.stage.value,
                started_at=_instant(value.started_at),
                ended_at=None,
                duration_ms=None,
                outcome=None,
            ))
        return value

    def finish_latency_span(
        self,
        span_id: UUID,
        *,
        ended_at: datetime,
        outcome: SpanOutcome,
    ) -> LatencySpan:
        with self.engine.begin() as connection:
            row = connection.execute(select(latency_spans).where(
                latency_spans.c.span_id == str(span_id)
            )).mappings().one()
            existing = self._latency_span(row)
            if existing.ended_at is not None:
                if existing.outcome != outcome:
                    raise ValueError("completed latency span outcome conflict")
                return existing
            normalized_end = ended_at.astimezone(timezone.utc)
            if normalized_end < existing.started_at:
                raise ValueError("latency span cannot end before it starts")
            duration_ms = int(
                (normalized_end - existing.started_at).total_seconds() * 1000
            )
            connection.execute(update(latency_spans).where(
                latency_spans.c.span_id == str(span_id)
            ).values(
                ended_at=_instant(normalized_end),
                duration_ms=duration_ms,
                outcome=outcome.value,
            ))
        return existing.model_copy(update={
            "ended_at": normalized_end,
            "duration_ms": duration_ms,
            "outcome": outcome,
        })

    def record_completed_latency_span(self, value: LatencySpan) -> LatencySpan:
        if value.ended_at is None or value.duration_ms is None or value.outcome is None:
            raise ValueError("a completed latency span requires end, duration, and outcome")
        expected = int((value.ended_at - value.started_at).total_seconds() * 1000)
        if value.duration_ms != expected:
            raise ValueError("latency span duration does not match its timestamps")
        self.start_latency_span(value.model_copy(update={
            "ended_at": None,
            "duration_ms": None,
            "outcome": None,
        }))
        return self.finish_latency_span(
            value.span_id,
            ended_at=value.ended_at,
            outcome=value.outcome,
        )

    def list_latency_spans(self, session_id: UUID) -> tuple[LatencySpan, ...]:
        with self.engine.connect() as connection:
            rows = connection.execute(select(latency_spans).where(
                latency_spans.c.session_id == str(session_id)
            ).order_by(latency_spans.c.started_at, latency_spans.c.span_id)).mappings()
            return tuple(self._latency_span(row) for row in rows)

    def record_agent_interruption(
        self,
        *,
        session_id: UUID,
        agent_turn_id: str,
        provider_request_id: str,
        interrupted_at: datetime,
    ) -> None:
        with self.engine.begin() as connection:
            existing = connection.execute(select(agent_interruptions).where(
                agent_interruptions.c.session_id == str(session_id),
                agent_interruptions.c.agent_turn_id == agent_turn_id,
            )).mappings().one_or_none()
            values = {
                "session_id": str(session_id),
                "agent_turn_id": agent_turn_id,
                "provider_request_id": provider_request_id,
                "interrupted_at": _instant(interrupted_at),
            }
            if existing is not None:
                if dict(existing) != values:
                    raise ValueError("agent interruption identity conflict")
                return
            connection.execute(insert(agent_interruptions).values(**values))

    def list_agent_interruptions(self, session_id: UUID) -> tuple[dict[str, object], ...]:
        with self.engine.connect() as connection:
            rows = connection.execute(select(agent_interruptions).where(
                agent_interruptions.c.session_id == str(session_id)
            ).order_by(agent_interruptions.c.interrupted_at, agent_interruptions.c.agent_turn_id)).mappings()
            return tuple({
                "session_id": UUID(row["session_id"]),
                "agent_turn_id": row["agent_turn_id"],
                "provider_request_id": row["provider_request_id"],
                "interrupted_at": _parse_instant(row["interrupted_at"]),
            } for row in rows)

    def save_proposal(self, value: PackageProposalRecord) -> PackageProposalRecord:
        with self.engine.begin() as connection:
            prior = connection.execute(select(package_proposals).where(
                package_proposals.c.session_id == str(value.session_id),
                package_proposals.c.status == ProposalStatus.PENDING.value,
            )).mappings().one_or_none()
            if prior is not None:
                connection.execute(update(package_proposals).where(
                    package_proposals.c.proposal_ref == prior["proposal_ref"]
                ).values(status=ProposalStatus.SUPERSEDED.value, updated_at=_instant(value.created_at)))
            connection.execute(insert(package_proposals).values(
                proposal_ref=value.proposal_ref, session_id=str(value.session_id), version=value.version,
                base_foundation_revision=value.base_foundation_revision,
                analyzer_result_json=value.analyzer_result_json, status=value.status.value,
                created_at=_instant(value.created_at), updated_at=_instant(value.updated_at),
            ))
            connection.execute(update(sessions).where(sessions.c.session_id == str(value.session_id)).values(
                pending_proposal_ref=value.proposal_ref, updated_at=_instant(value.updated_at),
            ))
        return value

    def save_validated_proposal_checkpoint(
        self,
        proposal: PackageProposalRecord,
        checkpoint: AnalyzerCheckpoint,
        *,
        commit_guard: Callable[[], None] | None = None,
    ) -> PackageProposalRecord:
        if (
            checkpoint.stage != AnalyzerCheckpointStage.PENDING_MATERIALIZED
            or checkpoint.proposal_ref != proposal.proposal_ref
            or checkpoint.session_id != proposal.session_id
        ):
            raise ValueError("atomic proposal checkpoint identity mismatch")
        checkpoint_values = self._analyzer_checkpoint_values(checkpoint)
        with self.engine.begin() as connection:
            if commit_guard is not None:
                commit_guard()
            existing_checkpoint = connection.execute(
                select(analyzer_checkpoints).where(
                    analyzer_checkpoints.c.request_id == str(checkpoint.request_id)
                )
            ).mappings().one_or_none()
            if existing_checkpoint is not None:
                self._assert_checkpoint_update(existing_checkpoint, checkpoint)
            existing_proposal = connection.execute(
                select(package_proposals).where(
                    package_proposals.c.proposal_ref == proposal.proposal_ref
                )
            ).mappings().one_or_none()
            if existing_proposal is None:
                prior = connection.execute(select(package_proposals).where(
                    package_proposals.c.session_id == str(proposal.session_id),
                    package_proposals.c.status == ProposalStatus.PENDING.value,
                )).mappings().one_or_none()
                if prior is not None:
                    connection.execute(update(package_proposals).where(
                        package_proposals.c.proposal_ref == prior["proposal_ref"]
                    ).values(
                        status=ProposalStatus.SUPERSEDED.value,
                        updated_at=_instant(proposal.created_at),
                    ))
                connection.execute(insert(package_proposals).values(
                    proposal_ref=proposal.proposal_ref,
                    session_id=str(proposal.session_id),
                    version=proposal.version,
                    base_foundation_revision=proposal.base_foundation_revision,
                    analyzer_result_json=proposal.analyzer_result_json,
                    status=proposal.status.value,
                    created_at=_instant(proposal.created_at),
                    updated_at=_instant(proposal.updated_at),
                ))
            elif self._proposal(existing_proposal) != proposal:
                raise ValueError("proposal checkpoint content conflict")
            if existing_checkpoint is None:
                connection.execute(
                    insert(analyzer_checkpoints).values(**checkpoint_values)
                )
            else:
                connection.execute(update(analyzer_checkpoints).where(
                    analyzer_checkpoints.c.request_id == str(checkpoint.request_id)
                ).values(**checkpoint_values))
            connection.execute(update(sessions).where(
                sessions.c.session_id == str(proposal.session_id)
            ).values(
                pending_proposal_ref=(
                    proposal.proposal_ref
                    if proposal.status == ProposalStatus.PENDING
                    else None
                ),
                updated_at=_instant(proposal.updated_at),
            ))
            if commit_guard is not None:
                commit_guard()
        return proposal

    def pending_proposal(self, session_id: UUID) -> PackageProposalRecord | None:
        with self.engine.connect() as connection:
            row = connection.execute(select(package_proposals).where(
                package_proposals.c.session_id == str(session_id),
                package_proposals.c.status == ProposalStatus.PENDING.value,
            )).mappings().one_or_none()
        return None if row is None else self._proposal(row)

    def proposal(self, proposal_ref: str) -> PackageProposalRecord | None:
        with self.engine.connect() as connection:
            row = connection.execute(select(package_proposals).where(
                package_proposals.c.proposal_ref == proposal_ref
            )).mappings().one_or_none()
        return None if row is None else self._proposal(row)

    def latest_proposal(
        self, session_id: UUID, *, status: ProposalStatus | None = None
    ) -> PackageProposalRecord | None:
        clauses = [package_proposals.c.session_id == str(session_id)]
        if status is not None:
            clauses.append(package_proposals.c.status == status.value)
        with self.engine.connect() as connection:
            row = connection.execute(
                select(package_proposals)
                .where(*clauses)
                .order_by(package_proposals.c.version.desc())
                .limit(1)
            ).mappings().one_or_none()
        return None if row is None else self._proposal(row)

    def set_proposal_status(self, proposal_ref: str, status: ProposalStatus, now: datetime) -> PackageProposalRecord:
        with self.engine.begin() as connection:
            row = connection.execute(select(package_proposals).where(
                package_proposals.c.proposal_ref == proposal_ref
            )).mappings().one()
            connection.execute(update(package_proposals).where(
                package_proposals.c.proposal_ref == proposal_ref
            ).values(status=status.value, updated_at=_instant(now)))
            connection.execute(update(sessions).where(sessions.c.session_id == row["session_id"]).values(
                pending_proposal_ref=None if status != ProposalStatus.PENDING else proposal_ref,
                updated_at=_instant(now),
            ))
        updated = dict(row); updated["status"] = status.value; updated["updated_at"] = _instant(now)
        return self._proposal(updated)

    def set_foundation_revision(self, session_id: UUID, revision: int, now: datetime) -> None:
        with self.engine.begin() as connection:
            connection.execute(update(sessions).where(sessions.c.session_id == str(session_id)).values(
                expected_foundation_revision=revision, updated_at=_instant(now),
            ))

    @staticmethod
    def _session_values(value: WorkshopSession) -> dict[str, object]:
        return {
            "session_id": str(value.session_id), "case_id": str(value.case_id), "pm_actor_id": str(value.pm_actor_id),
            "workshop_state": value.workshop_state.value, "conversation_phase": value.conversation_phase.value,
            "call_state": value.call_state.value, "last_activity_at": _instant(value.last_activity_at),
            "expected_foundation_revision": value.expected_foundation_revision, "pending_proposal_ref": value.pending_proposal_ref,
            "revision_locked": int(value.revision_locked), "revision_lock_reason": value.revision_lock_reason,
            "created_at": _instant(value.created_at), "updated_at": _instant(value.updated_at),
        }

    @staticmethod
    def _outbox_values(value: FoundationOutbox) -> dict[str, object]:
        result = value.model_dump(mode="python")
        result.update(outbox_id=str(value.outbox_id), session_id=str(value.session_id), command_id=str(value.command_id),
                      status=value.status.value, created_at=_instant(value.created_at), updated_at=_instant(value.updated_at))
        return result

    @staticmethod
    def _analyzer_checkpoint_values(
        value: AnalyzerCheckpoint,
    ) -> dict[str, object]:
        return {
            "request_id": str(value.request_id),
            "session_id": str(value.session_id),
            "turn_sequence": value.turn_sequence,
            "request_fingerprint": value.request_fingerprint,
            "source_fingerprint": value.source_fingerprint,
            "retrieval_fingerprint": value.retrieval_fingerprint,
            "selected_aliases_json": json.dumps(
                value.selected_aliases, separators=(",", ":")
            ),
            "stage": value.stage.value,
            "status": value.status.value,
            "failure_kind": (
                None if value.failure_kind is None else value.failure_kind.value
            ),
            "recovery_action": (
                None if value.recovery_action is None else value.recovery_action.value
            ),
            "proposal_ref": value.proposal_ref,
            "provider_call_count": value.provider_call_count,
            "created_at": _instant(value.created_at),
            "updated_at": _instant(value.updated_at),
        }

    @staticmethod
    def _assert_checkpoint_update(existing, value: AnalyzerCheckpoint) -> None:
        immutable = {
            "session_id": str(value.session_id),
            "turn_sequence": value.turn_sequence,
            "request_fingerprint": value.request_fingerprint,
            "source_fingerprint": value.source_fingerprint,
            "retrieval_fingerprint": value.retrieval_fingerprint,
            "selected_aliases_json": json.dumps(
                value.selected_aliases, separators=(",", ":")
            ),
        }
        if any(existing[key] != expected for key, expected in immutable.items()):
            raise ValueError("analyzer checkpoint identity conflict")
        if (
            existing["stage"] == AnalyzerCheckpointStage.PENDING_MATERIALIZED.value
            and value.stage != AnalyzerCheckpointStage.PENDING_MATERIALIZED
        ):
            raise ValueError("analyzer checkpoint cannot regress")
        if value.provider_call_count < existing["provider_call_count"]:
            raise ValueError("analyzer provider call count cannot regress")

    @staticmethod
    def _session(row) -> WorkshopSession:
        return WorkshopSession(
            session_id=UUID(row["session_id"]), case_id=UUID(row["case_id"]), pm_actor_id=UUID(row["pm_actor_id"]),
            workshop_state=WorkshopState(row["workshop_state"]), conversation_phase=ConversationPhase(row["conversation_phase"]),
            call_state=CallState(row["call_state"]), last_activity_at=_parse_instant(row["last_activity_at"]),
            expected_foundation_revision=row["expected_foundation_revision"], pending_proposal_ref=row["pending_proposal_ref"],
            revision_locked=bool(row["revision_locked"]), revision_lock_reason=row["revision_lock_reason"],
            created_at=_parse_instant(row["created_at"]), updated_at=_parse_instant(row["updated_at"]),
        )

    @staticmethod
    def _snapshot(row) -> TranscriptSnapshot:
        return TranscriptSnapshot(
            session_id=UUID(row["session_id"]), turn_sequence=row["turn_sequence"], version=row["version"],
            stable_artifact_id=UUID(row["stable_artifact_id"]), normalized_text=row["normalized_text"],
            content_hash=row["content_hash"], final_source_ref=SourceRef.model_validate_json(row["final_source_ref"]),
            provider_request_id=row["provider_request_id"], correction_of_version=row["correction_of_version"],
            created_at=_parse_instant(row["created_at"]),
        )

    @staticmethod
    def _outbox(row) -> FoundationOutbox:
        return FoundationOutbox(
            outbox_id=UUID(row["outbox_id"]), session_id=UUID(row["session_id"]), command_id=UUID(row["command_id"]),
            logical_action_key=row["logical_action_key"], command_name=row["command_name"],
            command_fingerprint=row["command_fingerprint"], command_json=row["command_json"],
            expected_foundation_revision=row["expected_foundation_revision"], status=OutboxStatus(row["status"]),
            attempts=row["attempts"], result_json=row["result_json"], created_at=_parse_instant(row["created_at"]),
            updated_at=_parse_instant(row["updated_at"]),
        )

    @staticmethod
    def _proposal(row) -> PackageProposalRecord:
        return PackageProposalRecord(
            proposal_ref=row["proposal_ref"], session_id=UUID(row["session_id"]), version=row["version"],
            base_foundation_revision=row["base_foundation_revision"], analyzer_result_json=row["analyzer_result_json"],
            status=ProposalStatus(row["status"]), created_at=_parse_instant(row["created_at"]),
            updated_at=_parse_instant(row["updated_at"]),
        )

    @staticmethod
    def _analyzer_checkpoint(row) -> AnalyzerCheckpoint:
        return AnalyzerCheckpoint(
            request_id=UUID(row["request_id"]),
            session_id=UUID(row["session_id"]),
            turn_sequence=row["turn_sequence"],
            request_fingerprint=row["request_fingerprint"],
            source_fingerprint=row["source_fingerprint"],
            retrieval_fingerprint=row["retrieval_fingerprint"],
            selected_aliases=tuple(json.loads(row["selected_aliases_json"])),
            stage=AnalyzerCheckpointStage(row["stage"]),
            status=AnalyzerCheckpointStatus(row["status"]),
            failure_kind=(
                None
                if row["failure_kind"] is None
                else AnalyzerFailureKind(row["failure_kind"])
            ),
            recovery_action=(
                None
                if row["recovery_action"] is None
                else AnalyzerRecoveryAction(row["recovery_action"])
            ),
            proposal_ref=row["proposal_ref"],
            provider_call_count=row["provider_call_count"],
            created_at=_parse_instant(row["created_at"]),
            updated_at=_parse_instant(row["updated_at"]),
        )

    @staticmethod
    def _latency_span(row) -> LatencySpan:
        return LatencySpan(
            span_id=UUID(row["span_id"]),
            session_id=UUID(row["session_id"]),
            stage=TelemetryStage(row["stage"]),
            started_at=_parse_instant(row["started_at"]),
            ended_at=None if row["ended_at"] is None else _parse_instant(row["ended_at"]),
            duration_ms=row["duration_ms"],
            outcome=None if row["outcome"] is None else SpanOutcome(row["outcome"]),
        )
