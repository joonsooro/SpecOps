from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import DatabaseError

from specops_workflow import FrozenClock
from specops_workflow.models import AddParticipantCommand, AuditQuery, QueryOne
from specops_workshop.legacy_test_app import DEMO_SESSION_ID, create_app
from specops_workshop.config import GEMINI_MODEL, TERRA_MODEL, Settings
from specops_workshop.contracts import OutboxStatus, WorkshopState
from specops_workshop.orchestration import WorkshopCoordinator
from specops_workshop.sources import SourceCatalog


SPEC_ENG_ROOT = Path("/Users/rudinro/Desktop/SpecOps/Spec_Eng")
NOW = datetime(2026, 8, 9, 11, tzinfo=timezone.utc)


def configured(tmp_path: Path) -> Settings:
    return Settings.load({
        "SPECOPS_DATABASE_URL": f"sqlite:///{tmp_path / 'foundation.sqlite'}",
        "WORKSHOP_DATABASE_URL": f"sqlite:///{tmp_path / 'workshop.sqlite'}",
        "GEMINI_API_KEY": "gemini-test-value",
        "OPENAI_API_KEY": "openai-test-value",
        "GEMINI_LIVE_MODEL": GEMINI_MODEL,
        "OPENAI_ANALYZER_MODEL": TERRA_MODEL,
    })


def runtime(tmp_path: Path):
    app = create_app(settings=configured(tmp_path), clock=FrozenClock(NOW), source_catalog=SourceCatalog(SPEC_ENG_ROOT))
    return app, app.state.coordinator, app.state.workshop_store, app.state.workflow


def test_final_transcripts_are_immutable_correctable_deduplicated_and_resumable(tmp_path):
    app, coordinator, store, _ = runtime(tmp_path)
    first = coordinator.commit_final_turn(
        DEMO_SESSION_ID, turn_sequence=1, text="  The export\n must include every page. ", provider_request_id="provider-final-1",
    )
    duplicate = coordinator.commit_final_turn(
        DEMO_SESSION_ID, turn_sequence=1, text="The export must include every page.", provider_request_id="provider-final-1",
    )
    assert duplicate == first
    original = store.latest_snapshots(DEMO_SESSION_ID)[0]
    assert original.normalized_text == "The export must include every page."
    assert original.version == 1 and original.correction_of_version is None

    coordinator.commit_final_turn(
        DEMO_SESSION_ID, turn_sequence=1, text="The export must include all filtered pages.",
        provider_request_id="provider-final-1-correction", correction_of_version=1,
    )
    corrected = store.latest_snapshots(DEMO_SESSION_ID)[0]
    assert corrected.version == 2 and corrected.correction_of_version == 1
    assert corrected.stable_artifact_id == original.stable_artifact_id
    with pytest.raises(DatabaseError, match="TRANSCRIPT_APPEND_ONLY"):
        with store.engine.begin() as connection:
            connection.execute(text("UPDATE transcript_snapshots SET normalized_text='changed'"))

    restarted = create_app(settings=app.state.settings, clock=FrozenClock(NOW), source_catalog=SourceCatalog(SPEC_ENG_ROOT))
    recovered = restarted.state.coordinator.recover(DEMO_SESSION_ID)
    assert recovered.final_transcripts == (corrected,)
    assert recovered.pending_outbox_ids == ()
    assert recovered.session.expected_foundation_revision == first.receipt.revision + 1


class CrashAfterFoundationCommit:
    def __init__(self, foundation) -> None:
        self.foundation = foundation
        self.crashed = False

    def get_workflow_view(self, query):
        return self.foundation.get_workflow_view(query)

    def register_source_artifact(self, command):
        result = self.foundation.register_source_artifact(command)
        if not self.crashed:
            self.crashed = True
            raise ConnectionError("simulated lost response")
        return result


def test_unknown_after_foundation_commit_replays_same_command_without_duplicate_audit(tmp_path):
    _, coordinator, store, workflow = runtime(tmp_path)
    session = store.get_session(DEMO_SESSION_ID)
    before = workflow.list_audit_events(AuditQuery(case_id=session.case_id, acting_actor_id=session.pm_actor_id)).items
    crashing = WorkshopCoordinator(store, CrashAfterFoundationCommit(workflow), clock=FrozenClock(NOW))
    with pytest.raises(ConnectionError, match="lost response"):
        crashing.commit_final_turn(
            DEMO_SESSION_ID, turn_sequence=1, text="Keep the six-column schema fixed.", provider_request_id="provider-crash-1",
        )
    unknown = store.replayable_outbox(DEMO_SESSION_ID)[0]
    assert unknown.status == OutboxStatus.UNKNOWN and unknown.attempts == 1
    assert store.get_session(DEMO_SESSION_ID).revision_lock_reason == "FOUNDATION_OUTCOME_UNKNOWN"
    after_commit = workflow.list_audit_events(AuditQuery(case_id=session.case_id, acting_actor_id=session.pm_actor_id)).items
    assert len(after_commit) == len(before) + 1

    recovered = coordinator.recover(DEMO_SESSION_ID)
    assert recovered.pending_outbox_ids == ()
    confirmed = store.get_outbox(unknown.outbox_id)
    assert confirmed.status == OutboxStatus.CONFIRMED and confirmed.attempts == 2
    assert recovered.session.revision_locked is False
    assert recovered.session.workshop_state == WorkshopState.ACTIVE
    after_replay = workflow.list_audit_events(AuditQuery(case_id=session.case_id, acting_actor_id=session.pm_actor_id)).items
    assert after_replay == after_commit


def test_unexplained_foundation_revision_locks_session_without_alternate_state(tmp_path):
    _, coordinator, store, workflow = runtime(tmp_path)
    pending = coordinator.enqueue_final_turn(
        DEMO_SESSION_ID, turn_sequence=1, text="Authorization must be rechecked.", provider_request_id="provider-stale-1",
    )
    session = store.get_session(DEMO_SESSION_ID)
    workflow.add_participant(AddParticipantCommand(
        command_id=uuid4(), case_id=session.case_id, acting_actor_id=session.pm_actor_id,
        expected_case_revision=session.expected_foundation_revision, actor_id=uuid4(),
    ))
    with pytest.raises(Exception):
        coordinator.dispatch(pending.outbox_id)
    locked = store.get_session(DEMO_SESSION_ID)
    assert locked.revision_locked is True
    assert locked.workshop_state == WorkshopState.BLOCKED
    assert locked.revision_lock_reason == "FOUNDATION_REVISION_MISMATCH"
    assert store.get_outbox(pending.outbox_id).status == OutboxStatus.REJECTED
    assert store.latest_snapshots(DEMO_SESSION_ID)[0].normalized_text == "Authorization must be rechecked."


def test_workshop_schema_has_no_audio_or_canonical_governance_storage(tmp_path):
    _, _, store, _ = runtime(tmp_path)
    inspector = inspect(store.engine)
    table_names = set(inspector.get_table_names())
    assert table_names == {
        "agent_interruptions", "analyzer_checkpoints", "analyzer_requests", "foundation_outbox", "latency_spans",
        "package_proposals", "transcript_snapshots", "workshop_sessions",
    }
    columns = {
        f"{table}.{column['name']}".lower()
        for table in table_names
        for column in inspector.get_columns(table)
    }
    assert not any(token in column for column in columns for token in ("raw_audio", "readiness", "approval", "review_request"))
    assert coordinator_text_columns(store) == {"transcript_snapshots.normalized_text"}


def coordinator_text_columns(store) -> set[str]:
    inspector = inspect(store.engine)
    return {
        f"{table}.{column['name']}"
        for table in inspector.get_table_names()
        for column in inspector.get_columns(table)
        if column["name"] == "normalized_text"
    }
