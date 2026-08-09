from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from .artifacts import ArtifactRoot
from .enums import ApprovalScope, Domain, FindingStatus, PlanTarget
from .models import ItemBinding, ResolutionRecord, ReviewRequest, SourceArtifactIdentity, SourceRef, SpecPackageItem
from .models import OperationIntent, OperationResult, RemoteObservation
from .enums import Action, OperationStatus, System


@dataclass
class DelegationState:
    id: UUID; delegator_id: UUID; delegate_id: UUID; domain: Domain; command_names: frozenset[str]
    artifact_kind: str | None; artifact_id: UUID | None; valid_from: datetime; valid_until: datetime
    later_review_required: bool; created_at: datetime; revoked_at: datetime | None = None


@dataclass
class AmbiguityState:
    id: UUID; category: str; domain: Domain; severity: str; evidence_refs: list[SourceRef]; clarification_question: str
    created_at: datetime; status: FindingStatus = FindingStatus.OPEN; resolutions: list[ResolutionRecord] = field(default_factory=list)
    resolved_at: datetime | None = None; item_binding: ItemBinding | None = None


@dataclass
class PackageItemState:
    definition: SpecPackageItem
    binding: ItemBinding
    domain: Domain
    complete: bool
    marked_ready: bool = False


@dataclass
class ApprovalState:
    id: UUID; artifact_kind: str; artifact_id: UUID; artifact_version: int; artifact_hash: str; scope: ApprovalScope
    actor_id: UUID; delegation_id: UUID | None; delegator_id: UUID | None; later_review_required: bool; approved_at: datetime


@dataclass
class BindingState:
    id: UUID; case_id: UUID; system: System; plan_id: UUID; item_id: UUID; generation_key: str
    external_identity: Any; current_plan_version: int; current_observation_sequence: int; confirmed_at: datetime
    snapshots: list[RemoteObservation] = field(default_factory=list)


@dataclass
class OperationState:
    id: UUID; intent: OperationIntent; idempotency_key: str; status: OperationStatus; attempt: int
    created_at: datetime; updated_at: datetime; failure_code: str | None = None; confirmation: RemoteObservation | None = None
    confirmed_snapshot_sequence: int | None = None; last_result: OperationResult | None = None; attempt_started_at: datetime | None = None


@dataclass
class CaseState:
    id: UUID; pm_actor_id: UUID; dev_lead_actor_id: UUID; created_at: datetime; revision: int = 0
    participants: set[UUID] = field(default_factory=set)
    delegations: dict[UUID, DelegationState] = field(default_factory=dict)
    sources: dict[tuple[UUID, int], SourceArtifactIdentity] = field(default_factory=dict)
    ambiguities: dict[UUID, AmbiguityState] = field(default_factory=dict)
    package: ArtifactRoot | None = None
    package_items: dict[UUID, list[PackageItemState]] = field(default_factory=dict)
    package_version_items: dict[int, list[UUID]] = field(default_factory=dict)
    review_requests: dict[UUID, ReviewRequest] = field(default_factory=dict)
    plans: dict[PlanTarget, ArtifactRoot] = field(default_factory=dict)
    policy: ArtifactRoot | None = None
    approvals: list[ApprovalState] = field(default_factory=list)
    operations: dict[UUID, OperationState] = field(default_factory=dict)
    bindings: dict[UUID, BindingState] = field(default_factory=dict)
    command_results: dict[UUID, tuple[str, BaseModel]] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
