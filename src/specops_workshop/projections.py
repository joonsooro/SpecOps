from __future__ import annotations

from typing import Literal

from specops_workflow.models import ReviewRequest, SpecPackageGovernanceView

from .analyzer import AnalyzerTurnResult, ControlIntent
from .contracts import (
    PackageProposalRecord,
    TranscriptSnapshot,
    WorkshopModel,
    WorkshopSession,
)


class PendingProposalView(WorkshopModel):
    record: PackageProposalRecord
    result: AnalyzerTurnResult


class WorkshopProjection(WorkshopModel):
    session: WorkshopSession
    final_transcripts: tuple[TranscriptSnapshot, ...]
    pending_proposal: PendingProposalView | None
    governance: SpecPackageGovernanceView | None
    review_requests: tuple[ReviewRequest, ...]


class ProposalControlInput(WorkshopModel):
    intent: Literal[ControlIntent.CONFIRM, ControlIntent.EDIT, ControlIntent.REJECT]
    proposal_ref: str
    edit_instruction: str | None
    acknowledgement: str | None
