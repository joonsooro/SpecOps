from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from specops_workflow.models import SourceRef


class WorkshopModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class WorkshopState(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    ACTIVE = "ACTIVE"
    FINISHING = "FINISHING"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"


class ConversationPhase(StrEnum):
    WORKSHOP = "WORKSHOP"
    HANDOFF_READY = "HANDOFF_READY"
    COMPLETE = "COMPLETE"


class CallState(StrEnum):
    READY = "READY"
    CONNECTING = "CONNECTING"
    LISTENING = "LISTENING"
    AGENT_SPEAKING = "AGENT_SPEAKING"
    COMMITTING = "COMMITTING"
    DISCONNECTED = "DISCONNECTED"
    ENDED = "ENDED"


class OutboxStatus(StrEnum):
    PENDING = "PENDING"
    UNKNOWN = "UNKNOWN"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"
class ProposalStatus(StrEnum):
    PENDING = "PENDING"; SUPERSEDED = "SUPERSEDED"; REJECTED = "REJECTED"; COMMITTED = "COMMITTED"


class WorkshopSession(WorkshopModel):
    session_id: UUID
    case_id: UUID
    pm_actor_id: UUID
    workshop_state: WorkshopState
    conversation_phase: ConversationPhase
    call_state: CallState
    last_activity_at: datetime
    expected_foundation_revision: int = Field(ge=0)
    pending_proposal_ref: str | None = None
    revision_locked: bool = False
    revision_lock_reason: str | None = None
    created_at: datetime
    updated_at: datetime


class TranscriptSnapshot(WorkshopModel):
    session_id: UUID
    turn_sequence: int = Field(ge=1)
    version: int = Field(ge=1)
    stable_artifact_id: UUID
    normalized_text: str = Field(min_length=1, max_length=100_000)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    final_source_ref: SourceRef
    provider_request_id: str = Field(min_length=1, max_length=256)
    correction_of_version: int | None = Field(default=None, ge=1)
    created_at: datetime


class FoundationOutbox(WorkshopModel):
    outbox_id: UUID
    session_id: UUID
    command_id: UUID
    logical_action_key: str = Field(min_length=1, max_length=256)
    command_name: str = Field(min_length=1, max_length=100)
    command_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    command_json: str
    expected_foundation_revision: int = Field(ge=0)
    status: OutboxStatus
    attempts: int = Field(ge=0)
    result_json: str | None = None
    created_at: datetime
    updated_at: datetime


class RecoveryView(WorkshopModel):
    session: WorkshopSession
    final_transcripts: tuple[TranscriptSnapshot, ...]
    pending_outbox_ids: tuple[UUID, ...]


class PackageProposalRecord(WorkshopModel):
    proposal_ref: str
    session_id: UUID
    version: int = Field(ge=1)
    base_foundation_revision: int = Field(ge=0)
    analyzer_result_json: str
    status: ProposalStatus
    created_at: datetime
    updated_at: datetime


class FinalTurnInput(WorkshopModel):
    turn_sequence: int = Field(ge=1)
    text: str = Field(min_length=1, max_length=100_000)
    provider_request_id: str = Field(min_length=1, max_length=256)
    correction_of_version: int | None = Field(default=None, ge=1)
