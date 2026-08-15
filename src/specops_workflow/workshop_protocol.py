"""Foundation-owned Workshop Interaction Protocol command handlers.

The handler is intentionally independent from provider and UI code.  It accepts
only strict generated commands, owns the idempotency ledger, mints identities,
persists immutable review projections, and performs confirmation validation in
one transaction against the Foundation database.
"""

from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import UUID, uuid4, uuid5

from jsonschema import Draft202012Validator
from pydantic import TypeAdapter
from referencing import Registry, Resource
from sqlalchemy import and_, func, insert, select, update
from sqlalchemy.exc import IntegrityError

from specops_contracts import workshop_v1 as c
from specops_contracts.canonical import (
    canonical_bytes,
    decision_view_hash,
    domain_hash,
    foundation_command_fingerprint,
    payload_hash,
    transcript_hash,
)

from .persistence import (
    WORKSHOP_PROTOCOL_TABLES as V4_WORKSHOP_PROTOCOL_TABLES,
    V0_RUNTIME_TABLES,
    audit_events,
    case_participants,
    cases as foundation_cases,
    delegations,
    engine_for,
    source_artifacts,
)

WORKSHOP_PROTOCOL_TABLES = {**V4_WORKSHOP_PROTOCOL_TABLES, **V0_RUNTIME_TABLES}


RUNTIME_NAMESPACE = UUID("31bfc764-d069-4f27-b614-444d1f47da7a")
TURN_CORRECTION_SUBJECT_PREFIX = "turn-correction:"
POST_BOOTSTRAP_RUNWAY_TARGET = 5


def _runtime_id(*parts: object) -> UUID:
    return uuid5(RUNTIME_NAMESPACE, ":".join(str(part) for part in parts))


def is_turn_correction_subject(subject_id: str) -> bool:
    return subject_id.startswith(TURN_CORRECTION_SUBJECT_PREFIX)


def turn_correction_parent_job_id(subject_id: str) -> str:
    if not is_turn_correction_subject(subject_id):
        raise ValueError("Analyzer job is not a TURN_ANALYSIS correction")
    return subject_id.removeprefix(TURN_CORRECTION_SUBJECT_PREFIX)
from .artifact_projection import (
    artifact_envelope,
    build_review_view,
    draft_governance,
    validate_governance,
    validate_exact,
)
from .artifact_quality_foundation import ArtifactQualityFoundationMixin
from .workshop_completion import CompletionUtterance, classify_completion_utterance


SCHEMA_ROOT = Path(__file__).resolve().parents[1] / "specops_contracts" / "schemas"
SEMANTIC_ENTITY_KINDS = (
    ("EVIDENCE", "evidence_candidates"),
    ("PROBLEM", "problems"),
    ("CLUSTER", "problem_clusters"),
    ("QUESTION", "questions"),
)
TURN_ENTITY_KINDS = (
    ("EVIDENCE", "evidence_candidates"),
    ("PROBLEM", "new_problems"),
    ("CLUSTER", "new_problem_clusters"),
    ("QUESTION", "new_questions"),
    ("FACT", "low_risk_facts"),
    ("DECISION", "decisions"),
    ("FINDING", "evidence_findings"),
)


class FoundationProtocolError(RuntimeError):
    def __init__(self, code: c.FoundationRejectionCode) -> None:
        super().__init__(code.value)
        self.code = code


class _GuidanceBranchUnavailable(RuntimeError):
    """One current question has an unresolved prerequisite or no open target."""


@dataclass(frozen=True)
class ProtocolCase:
    case_id: UUID
    session_id: UUID
    source_set_hash: str
    active_context_id: UUID | None
    readiness: c.Readiness
    review_obligation: c.ReviewObligation


@dataclass(frozen=True)
class TurnAnalysisPartition:
    """A source-verified subset plus the dependency closure held for correction."""

    verified_candidate: c.TurnAnalysisCandidate | None
    rejected_evidence_keys: tuple[str, ...]
    quarantined_candidate_keys: tuple[str, ...]


def _candidate_ref_keys(value: Any) -> set[str]:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="python", exclude_none=False)
    if isinstance(value, dict):
        found = (
            {value["candidate_key"]}
            if value.get("ref_kind") == "CANDIDATE_KEY"
            else set()
        )
        for item in value.values():
            found.update(_candidate_ref_keys(item))
        return found
    if isinstance(value, (list, tuple)):
        found: set[str] = set()
        for item in value:
            found.update(_candidate_ref_keys(item))
        return found
    return set()


def _partition_turn_candidate(
    candidate: c.TurnAnalysisCandidate,
    rejected_evidence_keys: tuple[str, ...],
) -> TurnAnalysisPartition:
    if not rejected_evidence_keys:
        return TurnAnalysisPartition(candidate, (), ())

    keyed_fields = (
        "evidence_candidates",
        "new_problems",
        "new_problem_clusters",
        "new_questions",
        "low_risk_facts",
        "decisions",
        "evidence_findings",
    )
    quarantined = set(rejected_evidence_keys)
    changed = True
    while changed:
        changed = False
        for field_name in keyed_fields:
            for item in getattr(candidate, field_name):
                if item.candidate_key in quarantined:
                    continue
                if _candidate_ref_keys(item).intersection(quarantined):
                    quarantined.add(item.candidate_key)
                    changed = True

    updates: dict[str, Any] = {
        field_name: tuple(
            item
            for item in getattr(candidate, field_name)
            if item.candidate_key not in quarantined
        )
        for field_name in keyed_fields
    }
    for field_name in (
        "revised_problem_clusters",
        "revised_questions",
        "problem_assessments",
    ):
        updates[field_name] = tuple(
            item
            for item in getattr(candidate, field_name)
            if not _candidate_ref_keys(item).intersection(quarantined)
        )

    semantic_fields = (
        *keyed_fields,
        "revised_problem_clusters",
        "revised_questions",
        "problem_assessments",
    )
    if not any(updates[field_name] for field_name in semantic_fields):
        verified = None
    else:
        updates.update(
            disposition=c.TurnDisposition.SUBSTANTIVE,
            no_change_reason_code=None,
        )
        material = candidate.model_dump(mode="python", exclude_none=False)
        material.update(updates)
        verified = c.TurnAnalysisCandidate.model_validate(material)
    return TurnAnalysisPartition(
        verified_candidate=verified,
        rejected_evidence_keys=tuple(sorted(rejected_evidence_keys)),
        quarantined_candidate_keys=tuple(sorted(quarantined)),
    )


def _instant(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _json(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json", exclude_none=False)
    return canonical_bytes(value).decode("utf-8")


def _payload_validator(filename: str) -> Draft202012Validator:
    path = SCHEMA_ROOT / filename
    schema = json.loads(path.read_text(encoding="utf-8"))
    registry = Registry()
    for schema_path in SCHEMA_ROOT.glob("*.json"):
        value = json.loads(schema_path.read_text(encoding="utf-8"))
        resource = Resource.from_contents(value)
        registry = registry.with_resource(value.get("$id", schema_path.as_uri()), resource)
    return Draft202012Validator(schema, registry=registry)


class WorkshopFoundationService(ArtifactQualityFoundationMixin):
    def __init__(
        self,
        database_url: str,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        new_id: Callable[[], UUID] = uuid4,
    ) -> None:
        self.engine = engine_for(database_url)
        self.now = now
        self.new_id = new_id

    def register_case(
        self,
        *,
        case_id: UUID,
        session_id: UUID,
        source_set_hash: str,
    ) -> ProtocolCase:
        table = WORKSHOP_PROTOCOL_TABLES["workshop_protocol_cases"]
        preparation = WORKSHOP_PROTOCOL_TABLES["workshop_preparations"]
        resources = WORKSHOP_PROTOCOL_TABLES["workshop_preparation_resources"]
        now = _instant(self.now())
        preparation_id = _runtime_id(case_id, "preparation", source_set_hash)
        with self.engine.begin() as connection:
            existing = connection.execute(
                select(table).where(table.c.case_id == str(case_id))
            ).mappings().one_or_none()
            if existing is None:
                connection.execute(
                    insert(table).values(
                        case_id=str(case_id),
                        session_id=str(session_id),
                        source_set_hash=source_set_hash,
                        active_context_id=None,
                        readiness=c.Readiness.FORMULATING.value,
                        review_obligation=c.ReviewObligation.NONE.value,
                        created_at=now,
                        updated_at=now,
                    )
                )
            elif (
                existing["session_id"] != str(session_id)
                or existing["source_set_hash"] != source_set_hash
            ):
                raise FoundationProtocolError(c.FoundationRejectionCode.DUPLICATE_CONFLICT)

            # A 0004 database can already contain the canonical V4 case.  The
            # 0005 operational rows are therefore backfilled idempotently when
            # that case is registered after upgrade instead of assuming this is
            # a brand-new case.
            existing_preparation = connection.execute(
                select(preparation.c.case_id).where(preparation.c.case_id == str(case_id))
            ).scalar_one_or_none()
            if existing_preparation is None:
                connection.execute(
                    insert(preparation).values(
                        case_id=str(case_id),
                        preparation_id=str(preparation_id),
                        phase="VALIDATING_DOCUMENTS",
                        started_at=now,
                        updated_at=now,
                        ready_at=None,
                        failure_code=None,
                        cleanup_state="NOT_REQUIRED",
                        cleanup_reason=None,
                        last_client_disconnected_at=None,
                        restart_grace_until=None,
                        workshop_complete_at=None,
                        cleanup_available_at=None,
                        cleanup_last_error_code=None,
                    )
                )
            existing_resources = connection.execute(
                select(resources.c.case_id).where(resources.c.case_id == str(case_id))
            ).scalar_one_or_none()
            if existing_resources is None:
                connection.execute(
                    insert(resources).values(
                        case_id=str(case_id),
                        source_set_hash=source_set_hash,
                        pm_file_id=None,
                        technical_file_id=None,
                        provider_conversation_id=None,
                        bootstrap_request_id=f"specops-bootstrap-{preparation_id}",
                        bootstrap_response_id=None,
                        bootstrap_candidate_json=None,
                        context_json=None,
                        updated_at=now,
                    )
                )
        return self.get_case(case_id)

    def get_case(self, case_id: UUID) -> ProtocolCase:
        table = WORKSHOP_PROTOCOL_TABLES["workshop_protocol_cases"]
        with self.engine.connect() as connection:
            row = connection.execute(
                select(table).where(table.c.case_id == str(case_id))
            ).mappings().one()
        return ProtocolCase(
            case_id=UUID(row["case_id"]),
            session_id=UUID(row["session_id"]),
            source_set_hash=row["source_set_hash"],
            active_context_id=UUID(row["active_context_id"]) if row["active_context_id"] else None,
            readiness=c.Readiness(row["readiness"]),
            review_obligation=c.ReviewObligation(row["review_obligation"]),
        )

    def case_revision(self, case_id: UUID) -> int:
        """Return the one canonical Foundation revision for orchestration reads."""

        with self.engine.connect() as connection:
            return self._case_revision(connection, case_id)

    def idempotent_receipt(
        self, case_id: UUID, idempotency_key: str
    ) -> c.FoundationReceipt | None:
        table = WORKSHOP_PROTOCOL_TABLES["workshop_command_ledger"]
        with self.engine.connect() as connection:
            value = connection.execute(
                select(table.c.receipt_json).where(
                    table.c.case_id == str(case_id),
                    table.c.idempotency_key == idempotency_key,
                )
            ).scalar_one_or_none()
        return None if value is None else TypeAdapter(c.FoundationReceipt).validate_json(value)

    def workshop_completion_receipt(
        self, case_id: UUID
    ) -> c.WorkshopCompletionReceipt | None:
        table = WORKSHOP_PROTOCOL_TABLES["workshop_command_ledger"]
        with self.engine.connect() as connection:
            value = connection.execute(
                select(table.c.receipt_json)
                .where(
                    table.c.case_id == str(case_id),
                    table.c.command_type == "CLAIM_WORKSHOP_COMPLETE",
                )
                .order_by(table.c.recorded_at)
                .limit(1)
            ).scalar_one_or_none()
        return None if value is None else c.WorkshopCompletionReceipt.model_validate_json(value)

    def active_analyzer_context(self, case_id: UUID) -> c.AnalyzerContextBinding | None:
        """Recover the persisted provider binding; provider IDs never live in UI state."""

        table = WORKSHOP_PROTOCOL_TABLES["workshop_analyzer_contexts"]
        with self.engine.connect() as connection:
            row = connection.execute(
                select(table.c.binding_json).where(
                    table.c.case_id == str(case_id),
                    table.c.status == c.ContextStatus.ACTIVE.value,
                )
            ).scalar_one_or_none()
        return None if row is None else c.AnalyzerContextBinding.model_validate_json(row)

    def latest_final_transcript(self, case_id: UUID) -> c.TranscriptFinalizedEvent | None:
        table = WORKSHOP_PROTOCOL_TABLES["workshop_final_transcripts"]
        with self.engine.connect() as connection:
            row = connection.execute(
                select(table.c.event_json)
                .where(table.c.case_id == str(case_id))
                .order_by(table.c.sequence_number.desc())
                .limit(1)
            ).scalar_one_or_none()
        return None if row is None else c.TranscriptFinalizedEvent.model_validate_json(row)

    def final_transcripts(self, case_id: UUID) -> tuple[c.TranscriptFinalizedEvent, ...]:
        table = WORKSHOP_PROTOCOL_TABLES["workshop_final_transcripts"]
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(table.c.event_json)
                .where(table.c.case_id == str(case_id))
                .order_by(table.c.sequence_number)
            ).scalars().all()
        return tuple(c.TranscriptFinalizedEvent.model_validate_json(row) for row in rows)

    def set_preparation_phase(
        self,
        case_id: UUID,
        phase: str,
        *,
        failure_code: str | None = None,
        cleanup_state: str | None = None,
    ) -> None:
        table = WORKSHOP_PROTOCOL_TABLES["workshop_preparations"]
        values: dict[str, Any] = {
            "phase": phase,
            "updated_at": _instant(self.now()),
            "failure_code": failure_code,
        }
        if phase == "READY":
            values["ready_at"] = _instant(self.now())
        if cleanup_state is not None:
            values["cleanup_state"] = cleanup_state
        with self.engine.begin() as connection:
            result = connection.execute(
                update(table).where(table.c.case_id == str(case_id)).values(**values)
            )
            if result.rowcount != 1:
                raise ValueError("unknown Workshop preparation")

    def preparation_projection(self, case_id: UUID) -> dict[str, Any]:
        table = WORKSHOP_PROTOCOL_TABLES["workshop_preparations"]
        with self.engine.connect() as connection:
            row = connection.execute(
                select(table).where(table.c.case_id == str(case_id))
            ).mappings().one()
        started = datetime.fromisoformat(row["started_at"].replace("Z", "+00:00"))
        delayed = row["phase"] not in {"READY", "FAILED"} and (
            self.now() - started
        ).total_seconds() >= 30
        messages = {
            "VALIDATING_DOCUMENTS": "Validating the uploaded documents…",
            "PREPARING_ANALYZER": "Preparing the SpecOps Analyzer…",
            "ANALYZER_REVIEWING_DOCUMENTS": "SpecOps Analyzer is reviewing the documents…",
            "FORMULATING_WORKSHOP_PLAN": "Formulating the Spec Workshop plan…",
            "VALIDATING_INITIAL_RUNWAY": "Validating the initial clarification runway…",
            "READY": "Your Spec Workshop is ready.",
            "FAILED": "Workshop preparation could not establish a safe clarification runway.",
        }
        return {
            "preparation_id": row["preparation_id"],
            "phase": row["phase"],
            "message": messages[row["phase"]],
            "started_at": row["started_at"],
            "updated_at": row["updated_at"],
            "ready_at": row["ready_at"],
            "failure_code": row["failure_code"],
            "cleanup_state": row["cleanup_state"],
            "cleanup_reason": row["cleanup_reason"],
            "last_client_disconnected_at": row["last_client_disconnected_at"],
            "restart_grace_until": row["restart_grace_until"],
            "workshop_complete_at": row["workshop_complete_at"],
            "cleanup_available_at": row["cleanup_available_at"],
            "cleanup_last_error_code": row["cleanup_last_error_code"],
            "delayed": delayed,
            "delayed_message": (
                "SpecOps Analyzer is taking a little longer to formulate your Workshop plan. "
                "Your documents are safe, and preparation is continuing."
                if delayed
                else None
            ),
        }

    def workshop_completion_projection(self, case_id: UUID) -> dict[str, Any]:
        preparation = self.preparation_projection(case_id)
        completed_at = preparation["workshop_complete_at"]
        if completed_at is None:
            return {"state": "ACTIVE", "completed_at": None}
        jobs_terminal = all(
            job["state"] in {"COMPLETED", "FAILED"}
            for job in self.analyzer_jobs(case_id)
        )
        if not jobs_terminal:
            state = c.WorkshopCompletionState.FINISHING_ANALYSIS.value
        elif preparation["cleanup_state"] == "COMPLETED":
            state = c.WorkshopCompletionState.COMPLETE.value
        else:
            state = c.WorkshopCompletionState.CLEANUP_PENDING.value
        return {"state": state, "completed_at": completed_at}

    def set_provider_resource_lifecycle(self, case_id: UUID, **values: Any) -> None:
        """Persist content-free retention/release state independently of preparation."""

        allowed = {
            "cleanup_state",
            "cleanup_reason",
            "last_client_disconnected_at",
            "restart_grace_until",
            "workshop_complete_at",
            "cleanup_available_at",
            "cleanup_last_error_code",
        }
        if not values or not set(values).issubset(allowed):
            raise ValueError("invalid provider resource lifecycle update")
        values["updated_at"] = _instant(self.now())
        table = WORKSHOP_PROTOCOL_TABLES["workshop_preparations"]
        with self.engine.begin() as connection:
            result = connection.execute(
                update(table).where(table.c.case_id == str(case_id)).values(**values)
            )
            if result.rowcount != 1:
                raise ValueError("unknown Workshop preparation")

    def reset_preparation_after_provider_cleanup(self, case_id: UUID) -> None:
        """Start a fresh provider context while retaining canonical Foundation state."""

        now = _instant(self.now())
        table = WORKSHOP_PROTOCOL_TABLES["workshop_preparations"]
        resources = WORKSHOP_PROTOCOL_TABLES["workshop_preparation_resources"]
        with self.engine.begin() as connection:
            result = connection.execute(
                update(table)
                .where(table.c.case_id == str(case_id))
                .values(
                    phase="VALIDATING_DOCUMENTS",
                    started_at=now,
                    updated_at=now,
                    ready_at=None,
                    failure_code=None,
                    cleanup_state="NOT_REQUIRED",
                    cleanup_reason=None,
                    last_client_disconnected_at=None,
                    restart_grace_until=None,
                    cleanup_available_at=None,
                    cleanup_last_error_code=None,
                )
            )
            if result.rowcount != 1:
                raise ValueError("unknown Workshop preparation")
            resource_result = connection.execute(
                update(resources)
                .where(resources.c.case_id == str(case_id))
                .values(
                    bootstrap_candidate_json=None,
                    context_json=None,
                    updated_at=now,
                )
            )
            if resource_result.rowcount != 1:
                raise ValueError("unknown Workshop preparation resources")

    def preparation_resources(self, case_id: UUID) -> dict[str, Any]:
        table = WORKSHOP_PROTOCOL_TABLES["workshop_preparation_resources"]
        with self.engine.connect() as connection:
            return dict(
                connection.execute(
                    select(table).where(table.c.case_id == str(case_id))
                ).mappings().one()
            )

    def checkpoint_provider_response(
        self,
        case_id: UUID,
        *,
        client_request_id: str,
        operation: str,
        provider_response_id: str,
    ) -> None:
        """Durably retain one stored post-bootstrap Response until cleanup."""

        table = V0_RUNTIME_TABLES["workshop_provider_responses"]
        now = _instant(self.now())
        with self.engine.begin() as connection:
            existing = connection.execute(
                select(table).where(
                    table.c.case_id == str(case_id),
                    table.c.client_request_id == client_request_id,
                )
            ).mappings().one_or_none()
            if existing is not None:
                if (
                    existing["operation"] != operation
                    or existing["provider_response_id"] != provider_response_id
                ):
                    raise RuntimeError("provider Response identity conflict")
                return
            connection.execute(
                insert(table).values(
                    case_id=str(case_id),
                    client_request_id=client_request_id,
                    operation=operation,
                    provider_response_id=provider_response_id,
                    recorded_at=now,
                    cleared_at=None,
                )
            )

    def pending_provider_responses(self, case_id: UUID) -> tuple[dict[str, Any], ...]:
        table = V0_RUNTIME_TABLES["workshop_provider_responses"]
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(table)
                .where(
                    table.c.case_id == str(case_id),
                    table.c.provider_response_id.is_not(None),
                )
                .order_by(table.c.recorded_at, table.c.client_request_id)
            ).mappings().all()
        return tuple(dict(row) for row in rows)

    def clear_provider_response(self, case_id: UUID, provider_response_id: str) -> None:
        table = V0_RUNTIME_TABLES["workshop_provider_responses"]
        with self.engine.begin() as connection:
            result = connection.execute(
                update(table)
                .where(
                    table.c.case_id == str(case_id),
                    table.c.provider_response_id == provider_response_id,
                )
                .values(
                    provider_response_id=None,
                    cleared_at=_instant(self.now()),
                )
            )
            if result.rowcount != 1:
                raise RuntimeError("tracked provider Response was not found")

    def checkpoint_preparation_resource(self, case_id: UUID, **values: Any) -> None:
        allowed = {
            "pm_file_id",
            "technical_file_id",
            "provider_conversation_id",
            "bootstrap_response_id",
            "bootstrap_candidate_json",
            "context_json",
        }
        if not values or not set(values).issubset(allowed):
            raise ValueError("invalid preparation resource checkpoint")
        values["updated_at"] = _instant(self.now())
        table = WORKSHOP_PROTOCOL_TABLES["workshop_preparation_resources"]
        with self.engine.begin() as connection:
            connection.execute(
                update(table).where(table.c.case_id == str(case_id)).values(**values)
            )

    def current_admitted_guidance(self, case_id: UUID) -> c.AdmittedGuidance | None:
        table = WORKSHOP_PROTOCOL_TABLES["workshop_guidance"]
        with self.engine.connect() as connection:
            value = connection.execute(
                select(table.c.payload_json)
                .where(table.c.case_id == str(case_id), table.c.valid == 1)
                .order_by(table.c.admitted_at.desc())
                .limit(1)
            ).scalar_one_or_none()
        return None if value is None else c.AdmittedGuidance.model_validate_json(value)

    def runway_projection(self, case_id: UUID) -> dict[str, Any]:
        guidance = self.current_admitted_guidance(case_id)
        if guidance is None:
            return {"guidance_id": None, "depth": 0, "questions": [], "asked": []}
        table = WORKSHOP_PROTOCOL_TABLES["workshop_runway_items"]
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(table)
                .where(
                    table.c.case_id == str(case_id),
                    table.c.guidance_id == str(guidance.guidance_id),
                )
                .order_by(table.c.position)
            ).mappings().all()
        questions = [
            {
                "question_id": row["question_id"],
                "question_version": row["question_version"],
                "position": row["position"],
                "exact_text": row["exact_text"],
                "reason": row["reason"],
            }
            for row in rows
            if row["status"] == "AVAILABLE"
        ]
        return {
            "guidance_id": str(guidance.guidance_id),
            "guidance_version": guidance.guidance_version,
            "depth": len(questions),
            "questions": questions,
            "asked": [row["question_id"] for row in rows if row["status"] == "ASKED"],
        }

    def voice_session_card(self, case_id: UUID) -> c.VoiceSessionCard:
        case = self.get_case(case_id)
        guidance = self.current_admitted_guidance(case_id)
        depth = self.runway_projection(case_id)["depth"]
        health = (
            c.RunwayHealth.HEALTHY
            if depth >= 3
            else c.RunwayHealth.PRIORITIZE_RUNWAY_REPLENISHMENT
            if depth == 2
            else c.RunwayHealth.PAUSE_DEEP_SYNTHESIS
            if depth == 1
            else c.RunwayHealth.SAFE_RECOVERY_ONLY
        )
        return c.VoiceSessionCard(
            protocol_version=c.PROTOCOL_VERSION,
            view_type="VOICE_SESSION_CARD",
            session_id=case.session_id,
            case_revision=self.case_revision(case_id),
            readiness=(c.Readiness.READY if depth else c.Readiness.NEEDS_CLARIFICATION),
            review_obligation=case.review_obligation,
            committed_summary=(),
            admitted_guidance=guidance,
            runway_health=health,
            pending_decision_batch_view_id=None,
            admitted_review_narration=None,
            generated_at=self.now(),
        )

    def analyzer_jobs(self, case_id: UUID) -> tuple[dict[str, Any], ...]:
        table = WORKSHOP_PROTOCOL_TABLES["workshop_analyzer_jobs"]
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(table)
                .where(table.c.case_id == str(case_id))
                .order_by(table.c.created_at, table.c.priority, table.c.job_id)
            ).mappings().all()
        return tuple(dict(row) for row in rows)

    def partition_turn_analysis_candidate(
        self,
        case_id: UUID,
        context_id: UUID,
        candidate: c.TurnAnalysisCandidate,
    ) -> TurnAnalysisPartition:
        """Verify every source locator before releasing independent branches."""

        rejected: list[str] = []
        with self.engine.connect() as connection:
            active_context = self._active_context(connection, case_id, context_id)
            if active_context is None:
                raise FoundationProtocolError(
                    c.FoundationRejectionCode.PROVIDER_REQUEST_BINDING_FAILED
                )
            for evidence in candidate.evidence_candidates:
                try:
                    self._admit_evidence_candidate(
                        connection, case_id, evidence, active_context
                    )
                except FoundationProtocolError as exc:
                    if exc.code is not c.FoundationRejectionCode.EVIDENCE_BINDING_FAILED:
                        raise
                    rejected.append(evidence.candidate_key)
        return _partition_turn_candidate(candidate, tuple(rejected))

    def enqueue_turn_analysis_correction(
        self,
        parent_job_id: str,
        *,
        worker_id: str,
    ) -> dict[str, Any]:
        """Create the sole durable correction child for one completed primary turn."""

        table = WORKSHOP_PROTOCOL_TABLES["workshop_analyzer_jobs"]
        subject_id = f"{TURN_CORRECTION_SUBJECT_PREFIX}{parent_job_id}"
        now = _instant(self.now())
        with self.engine.begin() as connection:
            parent = connection.execute(
                select(table).where(
                    table.c.job_id == parent_job_id,
                    table.c.operation == c.AnalyzerOperation.TURN_ANALYSIS.value,
                    table.c.lease_owner == worker_id,
                )
            ).mappings().one_or_none()
            if parent is None or is_turn_correction_subject(parent["subject_id"]):
                raise RuntimeError("primary TURN_ANALYSIS lease was lost")
            existing = connection.execute(
                select(table).where(
                    table.c.case_id == parent["case_id"],
                    table.c.operation == c.AnalyzerOperation.TURN_ANALYSIS.value,
                    table.c.subject_id == subject_id,
                )
            ).mappings().one_or_none()
            if existing is not None:
                return dict(existing)
            job_id = _runtime_id(
                parent["case_id"], "TURN_ANALYSIS_CORRECTION", parent_job_id
            )
            connection.execute(
                insert(table).values(
                    job_id=str(job_id),
                    case_id=parent["case_id"],
                    session_id=parent["session_id"],
                    operation=c.AnalyzerOperation.TURN_ANALYSIS.value,
                    subject_id=subject_id,
                    dedupe_key=f"turn-correction:{parent['case_id']}:{parent_job_id}",
                    # Primary turns remain first; selection-only Guidance may use
                    # the verified subset before this quarantined branch finishes.
                    priority=30,
                    state="ANALYSIS_PENDING",
                    provider_request_id=f"specops-turn_analysis-{job_id}",
                    request_json=None,
                    candidate_json=None,
                    admission_receipt_json=None,
                    attempt_count=0,
                    lease_owner=None,
                    lease_expires_at=None,
                    available_at=now,
                    last_error_code=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            return dict(
                connection.execute(select(table).where(table.c.job_id == str(job_id)))
                .mappings()
                .one()
            )

    def claim_analyzer_job(
        self, case_id: UUID, *, worker_id: str, lease_seconds: int = 30
    ) -> dict[str, Any] | None:
        table = WORKSHOP_PROTOCOL_TABLES["workshop_analyzer_jobs"]
        now = self.now()
        now_text = _instant(now)
        with self.engine.begin() as connection:
            rows = connection.execute(
                select(table)
                .where(
                    table.c.case_id == str(case_id),
                    table.c.state != "COMPLETED",
                    table.c.state != "FAILED",
                    (table.c.lease_expires_at.is_(None) | (table.c.lease_expires_at <= now_text)),
                    table.c.available_at <= now_text,
                )
                .order_by(table.c.priority, table.c.created_at, table.c.job_id)
            ).mappings().all()
            for row in rows:
                if row["operation"] == c.AnalyzerOperation.GUIDANCE.value:
                    older_turn_states = connection.execute(
                        select(table.c.state, table.c.subject_id).where(
                            table.c.case_id == str(case_id),
                            table.c.operation == c.AnalyzerOperation.TURN_ANALYSIS.value,
                            table.c.created_at <= row["created_at"],
                        )
                    ).all()
                    older_primary_states = [
                        state
                        for state, subject_id in older_turn_states
                        if not is_turn_correction_subject(subject_id)
                    ]
                    if any(
                        state not in {"COMPLETED", "FAILED"}
                        for state in older_primary_states
                    ):
                        continue
                    if any(state == "FAILED" for state in older_primary_states):
                        connection.execute(
                            update(table)
                            .where(
                                table.c.job_id == row["job_id"],
                                table.c.state.not_in(("COMPLETED", "FAILED")),
                            )
                            .values(
                                state="FAILED",
                                last_error_code="TURN_ANALYSIS_DEPENDENCY_FAILED",
                                updated_at=now_text,
                            )
                        )
                        continue
                lease_until = _instant(now + timedelta(seconds=lease_seconds))
                result = connection.execute(
                    update(table)
                    .where(
                        table.c.job_id == row["job_id"],
                        (table.c.lease_expires_at.is_(None) | (table.c.lease_expires_at <= now_text)),
                    )
                    .values(
                        lease_owner=worker_id,
                        lease_expires_at=lease_until,
                        attempt_count=table.c.attempt_count + 1,
                        updated_at=now_text,
                    )
                )
                if result.rowcount == 1:
                    claimed = dict(row)
                    claimed.update(
                        lease_owner=worker_id,
                        lease_expires_at=lease_until,
                        attempt_count=row["attempt_count"] + 1,
                    )
                    return claimed
        return None

    def checkpoint_analyzer_job(
        self,
        job_id: str,
        *,
        worker_id: str,
        state: str,
        request_json: str | None = None,
        candidate_json: str | None = None,
        admission_receipt_json: str | None = None,
    ) -> None:
        table = WORKSHOP_PROTOCOL_TABLES["workshop_analyzer_jobs"]
        values: dict[str, Any] = {"state": state, "updated_at": _instant(self.now())}
        if request_json is not None:
            values["request_json"] = request_json
        if candidate_json is not None:
            values["candidate_json"] = candidate_json
        if admission_receipt_json is not None:
            values["admission_receipt_json"] = admission_receipt_json
        if state == "COMPLETED":
            values.update(lease_owner=None, lease_expires_at=None)
        with self.engine.begin() as connection:
            result = connection.execute(
                update(table)
                .where(table.c.job_id == job_id, table.c.lease_owner == worker_id)
                .values(**values)
            )
            if result.rowcount != 1:
                raise RuntimeError("Analyzer job lease was lost")

    def release_analyzer_job(
        self, job_id: str, *, worker_id: str, error_code: str
    ) -> None:
        table = WORKSHOP_PROTOCOL_TABLES["workshop_analyzer_jobs"]
        with self.engine.begin() as connection:
            connection.execute(
                update(table)
                .where(table.c.job_id == job_id, table.c.lease_owner == worker_id)
                .values(
                    lease_owner=None,
                    lease_expires_at=None,
                    last_error_code=error_code[:128],
                    available_at=_instant(self.now() + timedelta(seconds=1)),
                    updated_at=_instant(self.now()),
                )
            )

    def fail_analyzer_job(
        self, job_id: str, *, worker_id: str, error_code: str
    ) -> None:
        table = WORKSHOP_PROTOCOL_TABLES["workshop_analyzer_jobs"]
        with self.engine.begin() as connection:
            connection.execute(
                update(table)
                .where(table.c.job_id == job_id, table.c.lease_owner == worker_id)
                .values(
                    state="FAILED",
                    lease_owner=None,
                    lease_expires_at=None,
                    last_error_code=error_code[:128],
                    updated_at=_instant(self.now()),
                )
            )

    def case_actor(self, case_id: UUID, role: str) -> UUID:
        column = {
            "PM": foundation_cases.c.pm_actor_id,
            "DEV_LEAD": foundation_cases.c.dev_lead_actor_id,
        }.get(role)
        if column is None:
            raise ValueError("unknown canonical case role")
        with self.engine.connect() as connection:
            value = connection.execute(
                select(column).where(foundation_cases.c.id == str(case_id))
            ).scalar_one()
        return UUID(value)

    def semantic_snapshot(self, case_id: UUID) -> c.FoundationSemanticSnapshot:
        """Reconstruct the bounded, provider-safe semantic snapshot from Foundation state."""

        case = self.get_case(case_id)
        records = WORKSHOP_PROTOCOL_TABLES["workshop_semantic_records"]
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(records)
                .where(
                    records.c.case_id == str(case_id),
                    records.c.status != c.SemanticRecordStatus.STALE.value,
                )
                .order_by(records.c.entity_kind, records.c.foundation_id, records.c.record_version)
            ).mappings().all()

        evidence = []
        problems = []
        questions = []
        facts = []
        decisions = []
        findings = []
        revision_requests = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            foundation_id = UUID(row["foundation_id"])
            version = row["record_version"]
            status = c.SemanticRecordStatus(row["status"])
            kind = row["entity_kind"]
            if kind == "EVIDENCE":
                binding = payload["source_binding"]
                evidence.append(
                    c.FoundationEvidenceSnapshot(
                        evidence_id=foundation_id,
                        evidence_version=version,
                        source_role=c.SourceRole(payload["source_role"]),
                        source_hash=binding["source_hash"],
                        locator=TypeAdapter(c.SourceLocator).validate_json(
                            _json(payload["locator"])
                        ),
                        exact_excerpt=payload["exact_excerpt"],
                        excerpt_hash=binding["excerpt_hash"],
                        relevance_claim=payload["relevance_claim"],
                    )
                )
            elif kind == "PROBLEM":
                problems.append(
                    c.FoundationProblemSnapshot(
                        problem_id=foundation_id,
                        problem_version=version,
                        status=status,
                        problem_kind=c.ProblemKind(payload["problem_kind"]),
                        domain=c.Domain(payload["domain"]),
                        severity=c.Severity(payload["severity"]),
                        statement=payload["statement"],
                        consequence=payload["consequence"],
                        evidence_ids=tuple(
                            UUID(item["foundation_id"]) for item in payload["evidence_refs"]
                        ),
                    )
                )
            elif kind == "QUESTION":
                questions.append(
                    c.FoundationQuestionSnapshot(
                        question_id=foundation_id,
                        question_version=version,
                        status=status,
                        text=payload["text"],
                        rationale=payload["rationale"],
                        question_shape=c.QuestionShape(payload["question_shape"]),
                        capture_policy=c.CapturePolicy(payload["capture_policy"]),
                        answer_options=tuple(payload["answer_options"]),
                        addresses_problem_ids=tuple(
                            UUID(item["foundation_id"])
                            for item in payload["addresses_problem_refs"]
                        ),
                        prerequisite_problem_ids=tuple(
                            UUID(item["foundation_id"])
                            for item in payload["prerequisite_problem_refs"]
                        ),
                    )
                )
            elif kind == "FACT":
                source_question = payload["source_question_ref"]
                facts.append(
                    c.FoundationFactSnapshot(
                        fact_id=foundation_id,
                        fact_version=version,
                        status=status,
                        source_question_id=UUID(source_question["foundation_id"]),
                        value_type=c.NormalizedScalarType(payload["value_type"]),
                        normalized_value=payload["normalized_value"],
                        source_transcript_event_id=UUID(payload["transcript_event_id"]),
                    )
                )
            elif kind == "DECISION":
                decisions.append(
                    c.FoundationDecisionSnapshot(
                        decision_id=foundation_id,
                        decision_version=version,
                        status=status,
                        classification=c.Domain(payload["classification"]),
                        statement=payload["statement"],
                        rationale=payload["rationale"],
                        alternatives_considered=tuple(payload["alternatives_considered"]),
                        problem_ids=tuple(
                            UUID(item["problem_ref"]["foundation_id"])
                            for item in payload["problem_links"]
                        ),
                        evidence_ids=tuple(
                            UUID(item["foundation_id"]) for item in payload["evidence_refs"]
                        ),
                    )
                )
            elif kind == "FINDING":
                findings.append(
                    c.AdmittedSemanticEvidenceFinding.model_validate_json(
                        row["payload_json"]
                    )
                )
            elif kind == "REVISION_REQUEST":
                revision_requests.append(
                    c.FoundationRevisionRequestSnapshot(
                        revision_request_id=foundation_id,
                        revision_request_version=version,
                        pending_decision_id=UUID(payload["pending_decision_id"]),
                        source_transcript_span=c.TranscriptSpan.model_validate_json(
                            _json(payload["source_transcript_span"])
                        ),
                        status=payload["status"],
                    )
                )
        return c.FoundationSemanticSnapshot(
            case_revision=self.case_revision(case_id),
            source_set_hash=case.source_set_hash,
            readiness=case.readiness,
            review_obligation=case.review_obligation,
            evidence=tuple(evidence),
            problems=tuple(problems),
            questions=tuple(questions),
            facts=tuple(facts),
            decisions=tuple(decisions),
            evidence_findings=tuple(findings),
            revision_requests=tuple(revision_requests),
        )

    def confirmed_spec_binding(self, case_id: UUID) -> c.ConfirmedSpecSynthesisBinding | None:
        records = WORKSHOP_PROTOCOL_TABLES["workshop_artifact_records"]
        confirmations = WORKSHOP_PROTOCOL_TABLES["workshop_artifact_confirmations"]
        with self.engine.connect() as connection:
            row = connection.execute(
                select(records, confirmations.c.confirmation_id, confirmations.c.confirmed_case_revision)
                .join(
                    confirmations,
                    and_(
                        confirmations.c.artifact_id == records.c.artifact_id,
                        confirmations.c.artifact_version == records.c.artifact_version,
                        confirmations.c.record_revision == records.c.record_revision,
                    ),
                )
                .where(
                    records.c.case_id == str(case_id),
                    records.c.artifact_type == "SPEC_PACKAGE",
                    records.c.status == "CONFIRMED",
                    confirmations.c.revoked_at.is_(None),
                    confirmations.c.superseded_by_confirmation_id.is_(None),
                )
                .order_by(records.c.artifact_version.desc())
                .limit(1)
            ).mappings().one_or_none()
        if row is None:
            return None
        return c.ConfirmedSpecSynthesisBinding(
            foundation_artifact_id=UUID(row["artifact_id"]),
            artifact_key=row["artifact_key"],
            artifact_version=row["artifact_version"],
            record_revision=row["record_revision"],
            confirmed_case_revision=row["confirmed_case_revision"],
            payload_hash=row["payload_hash"],
            confirmation_id=UUID(row["confirmation_id"]),
            canonical_payload_json=row["payload_json"],
        )

    def confirmed_decision_synthesis_bindings(
        self, case_id: UUID
    ) -> tuple[c.ConfirmedDecisionSynthesisBinding, ...]:
        """Project exact committed decision ceremonies for Spec synthesis."""

        snapshot = self.semantic_snapshot(case_id)
        confirmed = {
            item.decision_id: item
            for item in snapshot.decisions
            if item.status is c.SemanticRecordStatus.CONFIRMED
        }
        if not confirmed:
            return ()
        events = WORKSHOP_PROTOCOL_TABLES["workshop_protocol_events"]
        views = WORKSHOP_PROTOCOL_TABLES["workshop_decision_views"]
        actor_ref = self.case_actor(case_id, "PM")
        result: dict[UUID, c.ConfirmedDecisionSynthesisBinding] = {}
        with self.engine.connect() as connection:
            applied = connection.execute(
                select(events)
                .where(
                    events.c.case_id == str(case_id),
                    events.c.event_type == "APPLY_DECISION_BATCH_RESPONSE_APPLIED",
                )
                .order_by(events.c.event_sequence.desc())
            ).mappings().all()
            for event in applied:
                receipt = json.loads(event["event_json"])
                view_row = connection.execute(
                    select(views).where(
                        views.c.case_id == str(case_id),
                        views.c.view_id == receipt["decision_batch_view_id"],
                    )
                ).mappings().one()
                view = json.loads(view_row["view_json"])
                view_items = {
                    UUID(item["pending_decision_id"]): item for item in view["items"]
                }
                transcript_event = connection.execute(
                    select(events)
                    .where(
                        events.c.case_id == str(case_id),
                        events.c.event_sequence < event["event_sequence"],
                        events.c.event_type == "RECORD_FINAL_TRANSCRIPT_APPLIED",
                    )
                    .order_by(events.c.event_sequence.desc())
                    .limit(1)
                ).mappings().one()
                transcript_receipt = json.loads(transcript_event["event_json"])
                command = receipt["command"]
                for item in receipt["item_results"]:
                    if item["outcome"] != c.DecisionItemOutcome.COMMITTED.value:
                        continue
                    decision_id = UUID(item["committed_decision_id"])
                    if decision_id not in confirmed or decision_id in result:
                        continue
                    decision = confirmed[decision_id]
                    review_item = view_items[decision_id]
                    result[decision_id] = c.ConfirmedDecisionSynthesisBinding(
                        decision_id=decision.decision_id,
                        decision_version=decision.decision_version,
                        classification=decision.classification,
                        statement=decision.statement,
                        rationale=decision.rationale,
                        alternatives_considered=decision.alternatives_considered,
                        problem_ids=decision.problem_ids,
                        evidence_ids=decision.evidence_ids,
                        confirmation_id=UUID(command["command_id"]),
                        decision_batch_view_id=UUID(view["view_id"]),
                        decision_batch_view_hash=view["view_hash"],
                        review_item_id=UUID(review_item["review_item_id"]),
                        confirmed_case_revision=command["resulting_case_revision"],
                        actor_ref=actor_ref,
                        authority_validation_id=UUID(command["command_id"]),
                        transcript_event_id=UUID(
                            transcript_receipt["transcript_event_id"]
                        ),
                        confirmed_at=datetime.fromisoformat(
                            command["occurred_at"].replace("Z", "+00:00")
                        ),
                    )
        if set(result) != set(confirmed):
            raise ValueError("confirmed decision lacks an exact response ceremony")
        return tuple(result[key] for key in sorted(result, key=str))

    def issue_artifact_identity_plan(
        self,
        case_id: UUID,
        artifact_type: str,
        entity_kinds: tuple[str, ...],
    ) -> c.ArtifactSynthesisIdentityPlan:
        """Mint a synthesis target/identity plan from current Foundation state.

        The plan is persisted atomically as CONSUMED only when a valid provider
        candidate is admitted, so an abandoned provider request creates no
        canonical artifact record.
        """

        if artifact_type not in {"SPEC_PACKAGE", "TECHNICAL_CONTRACT"} or not entity_kinds:
            raise ValueError("a closed artifact type and at least one entity kind are required")
        revision = self.case_revision(case_id)
        records = WORKSHOP_PROTOCOL_TABLES["workshop_artifact_records"]
        prefix = "SPEC" if artifact_type == "SPEC_PACKAGE" else "CONTRACT"
        with self.engine.connect() as connection:
            prior = connection.execute(
                select(records)
                .where(
                    records.c.case_id == str(case_id),
                    records.c.artifact_type == artifact_type,
                )
                .order_by(records.c.artifact_version.desc())
                .limit(1)
            ).mappings().one_or_none()
        artifact_id = self.new_id() if prior is None else UUID(prior["artifact_id"])
        next_version = 1 if prior is None else prior["artifact_version"] + 1
        target = c.ArtifactDraftTarget(
            artifact_type=artifact_type,
            foundation_artifact_id=artifact_id,
            artifact_key=(
                f"{prefix}-{str(artifact_id).replace('-', '').upper()[:12]}"
                if prior is None
                else prior["artifact_key"]
            ),
            next_artifact_version=next_version,
        )
        snapshot = self.semantic_snapshot(case_id)
        source_refs = self._artifact_plan_source_refs(snapshot)
        canonical_identities: dict[str, list[tuple[UUID, int]]] = {}
        if artifact_type == "SPEC_PACKAGE":
            canonical_identities = {
                "ACTOR": [(self.case_actor(case_id, "PM"), 1)],
                "DECISION": [
                    (item.decision_id, item.decision_version)
                    for item in sorted(snapshot.decisions, key=lambda value: str(value.decision_id))
                    if item.status is c.SemanticRecordStatus.CONFIRMED
                ],
            }

        def planned_identity(kind: str) -> tuple[UUID, int]:
            available = canonical_identities.get(kind, [])
            if available:
                return available.pop(0)
            return self.new_id(), 1

        return c.ArtifactSynthesisIdentityPlan(
            identity_plan_id=self.new_id(),
            identity_plan_version=1,
            target=target,
            based_on_case_revision=revision,
            semantic_state_hash=domain_hash(
                "SPECOPS:SEMANTIC_STATE:v1", snapshot.model_dump(mode="json")
            ),
            planned_identities=tuple(
                c.PlannedArtifactIdentity(
                    foundation_id=identity,
                    foundation_version=version,
                    entity_kind=kind,
                    source_entity_refs=source_refs,
                )
                for kind in entity_kinds
                for identity, version in (planned_identity(kind),)
            ),
        )

    @staticmethod
    def _artifact_plan_source_refs(
        snapshot: c.FoundationSemanticSnapshot,
    ) -> tuple[c.FoundationEntityRef, ...]:
        """Bind every planned identity to a bounded canonical semantic basis."""

        identities = [
            (item.evidence_id, item.evidence_version) for item in snapshot.evidence
        ] + [
            (item.problem_id, item.problem_version) for item in snapshot.problems
        ] + [
            (item.question_id, item.question_version) for item in snapshot.questions
        ] + [
            (item.fact_id, item.fact_version) for item in snapshot.facts
        ] + [
            (item.decision_id, item.decision_version) for item in snapshot.decisions
        ] + [
            (item.finding_id, item.finding_version)
            for item in snapshot.evidence_findings
        ] + [
            (item.revision_request_id, item.revision_request_version)
            for item in snapshot.revision_requests
        ]
        return tuple(
            c.FoundationEntityRef(
                ref_kind="FOUNDATION_ID",
                foundation_id=identity,
                expected_version=version,
            )
            for identity, version in sorted(set(identities), key=lambda item: str(item[0]))[:100]
        )

    @staticmethod
    def artifact_identity_allocation_policy(
        artifact_type: str, *, confirmed_decision_count: int = 0
    ) -> tuple[str, ...]:
        """Server-owned bounded identity capacity derived from V4 payload paths."""

        policies = {
            "SPEC_PACKAGE": tuple(
                kind
                for kind, count in (
                    ("PACKAGE_ITEM", 1),
                    ("EVIDENCE", 8),
                    ("SEMANTIC_EVIDENCE_FINDING", 8),
                    ("ACTOR", 1),
                    ("GLOSSARY_TERM", 4),
                    ("OUTCOME", 1),
                    ("SCOPE_ITEM", 3),
                    ("SCOPE_BOUNDARY", 1),
                    ("JOURNEY", 2),
                    ("BEHAVIOUR_RULE", 8),
                    ("REQUIREMENT", 7),
                    ("DATA_RULE", 7),
                    ("EXPERIENCE_STATE", 7),
                    ("SCENARIO", 7),
                    ("QUALITY_ATTRIBUTE", 3),
                    ("CONSTRAINT", 3),
                    ("DEPENDENCY", 2),
                    ("RISK", 3),
                    ("DECISION", min(26, max(4, confirmed_decision_count + 2))),
                    ("OPEN_ITEM", 2),
                    ("ACCEPTANCE_CHECK", 10),
                )
                for _ in range(count)
            ),
            "TECHNICAL_CONTRACT": (
                "EVIDENCE", "SEMANTIC_EVIDENCE_FINDING", "ARCHITECTURE_NODE",
                "COMPONENT", "INTERFACE", "DATA_CONTRACT",
                "WORKFLOW", "FAILURE_CONTRACT", "SECURITY_CONTROL", "QUALITY_BUDGET",
                "SUBSTRATE_DEPENDENCY",
                "OBSERVABILITY_EVENT", "OBSERVABILITY_METRIC", "OBSERVABILITY_TRACE",
                "AUDIT_RECORD", "OBSERVABILITY_ALERT",
                "ROLLOUT_STEP", "BUILD_UNIT", "VERIFICATION_ITEM",
                "ENGINEERING_DECISION", "REVIEW_OBLIGATION",
            ),
        }
        try:
            return policies[artifact_type]
        except KeyError as exc:
            raise ValueError("unknown artifact type") from exc

    def execute(self, command: c.FoundationCommand) -> c.FoundationReceipt:
        case = self.get_case(command.case_id)
        if command.session_id != case.session_id:
            raise FoundationProtocolError(c.FoundationRejectionCode.MALFORMED_COMMAND)
        fingerprint = foundation_command_fingerprint(command.model_dump(mode="json"))
        ledger = WORKSHOP_PROTOCOL_TABLES["workshop_command_ledger"]

        with self.engine.begin() as connection:
            existing = connection.execute(
                select(ledger).where(
                    ledger.c.case_id == str(command.case_id),
                    ledger.c.idempotency_key == command.idempotency_key,
                )
            ).mappings().one_or_none()
            if existing is not None:
                if existing["command_fingerprint"] != fingerprint:
                    raise FoundationProtocolError(c.FoundationRejectionCode.DUPLICATE_CONFLICT)
                return TypeAdapter(c.FoundationReceipt).validate_json(existing["receipt_json"])

            current_revision = self._case_revision(connection, command.case_id)
            self._validate_concurrency(command, current_revision)
            receipt = self._dispatch(connection, command, current_revision)
            occurred_at = receipt.command.occurred_at
            connection.execute(
                insert(ledger).values(
                    case_id=str(command.case_id),
                    idempotency_key=command.idempotency_key,
                    command_id=str(command.command_id),
                    command_type=command.command_type,
                    command_fingerprint=fingerprint,
                    receipt_json=receipt.model_dump_json(),
                    recorded_at=_instant(occurred_at),
                )
            )
            self._record_audit_and_event(
                connection,
                command=command,
                fingerprint=fingerprint,
                receipt=receipt,
            )
            return receipt

    def _record_audit_and_event(self, connection, *, command, fingerprint, receipt) -> None:
        """Append the protocol command to the one Foundation revision stream."""

        prior = receipt.command.prior_case_revision
        resulting = receipt.command.resulting_case_revision
        connection.execute(
            insert(audit_events).values(
                event_id=str(self.new_id()),
                case_id=str(command.case_id),
                case_sequence=resulting,
                command_id=str(command.command_id),
                command_name=f"workshop.{command.command_type.lower()}",
                command_fingerprint=fingerprint.removeprefix("sha256:"),
                actor=str(command.acting_actor_id),
                occurred_at=receipt.command.occurred_at,
                target_ids="[]",
                before_case_revision=prior,
                after_case_revision=resulting,
                metadata=_json(
                    {
                        "protocol_version": "1.0.0",
                        "receipt_type": receipt.receipt_type,
                    }
                ),
                result=receipt.model_dump_json(),
            )
        )
        events = WORKSHOP_PROTOCOL_TABLES["workshop_protocol_events"]
        connection.execute(
            insert(events).values(
                event_id=str(self.new_id()),
                case_id=str(command.case_id),
                event_sequence=resulting,
                event_type=f"{command.command_type}_APPLIED",
                event_json=receipt.model_dump_json(),
                occurred_at=_instant(receipt.command.occurred_at),
            )
        )

    @staticmethod
    def _case_revision(connection, case_id: UUID) -> int:
        cases = connection.execute(
            select(WORKSHOP_PROTOCOL_TABLES["workshop_protocol_cases"]).where(
                WORKSHOP_PROTOCOL_TABLES["workshop_protocol_cases"].c.case_id == str(case_id)
            )
        ).mappings().one()
        from .persistence import cases as foundation_cases

        revision = connection.execute(
            select(foundation_cases.c.revision).where(foundation_cases.c.id == str(case_id))
        ).scalar_one()
        return revision

    @staticmethod
    def _validate_concurrency(command, revision: int) -> None:
        expected = getattr(command, "expected_case_revision", None)
        if expected is not None and expected != revision:
            raise FoundationProtocolError(c.FoundationRejectionCode.STALE_STATE)
        observed = getattr(command, "observed_case_revision", None)
        if observed is not None and observed > revision:
            raise FoundationProtocolError(c.FoundationRejectionCode.STALE_STATE)

    def _dispatch(self, connection, command, prior_revision: int):
        if isinstance(command, c.ActivateAnalyzerContextCommand):
            return self._activate_context(connection, command, prior_revision)
        if isinstance(command, c.InvalidateAnalyzerContextCommand):
            return self._invalidate_context(connection, command, prior_revision)
        if isinstance(command, c.RecordFinalTranscriptCommand):
            return self._record_transcript(connection, command, prior_revision)
        if isinstance(command, c.ClaimWorkshopCompleteCommand):
            return self._claim_workshop_complete(connection, command, prior_revision)
        if isinstance(command, c.AdmitInterviewBriefCommand):
            return self._admit_candidate(
                connection,
                command,
                prior_revision,
                SEMANTIC_ENTITY_KINDS,
            )
        if isinstance(command, c.AdmitTurnAnalysisCommand):
            return self._admit_candidate(
                connection,
                command,
                prior_revision,
                TURN_ENTITY_KINDS,
            )
        if isinstance(command, c.AdmitGuidanceCommand):
            return self._admit_guidance(connection, command, prior_revision)
        if isinstance(command, c.AdmitReviewNarrationCommand):
            return self._admit_narration(connection, command, prior_revision)
        if isinstance(command, c.CaptureLowRiskFactCommand):
            return self._capture_low_risk_fact(connection, command, prior_revision)
        if isinstance(command, c.MaterializeDecisionBatchReviewCommand):
            return self._materialize_decision_review(connection, command, prior_revision)
        if isinstance(command, c.ApplyDecisionBatchResponseCommand):
            return self._apply_decision_response(connection, command, prior_revision)
        if isinstance(command, c.AdmitSpecPackageSynthesisCommand):
            return self._admit_artifact(connection, command, prior_revision, "SPEC_PACKAGE")
        if isinstance(command, c.AdmitTechnicalContractSynthesisCommand):
            return self._admit_artifact(connection, command, prior_revision, "TECHNICAL_CONTRACT")
        if isinstance(command, c.MaterializeArtifactReviewCommand):
            return self._materialize_artifact_review(connection, command, prior_revision)
        if isinstance(command, c.ConfirmArtifactCommand):
            return self._confirm_artifact(connection, command, prior_revision)
        raise FoundationProtocolError(c.FoundationRejectionCode.INVALID_TRANSITION)

    def _base_receipt(self, command, prior_revision: int) -> c.FoundationCommandReceipt:
        return c.FoundationCommandReceipt(
            receipt_type="FOUNDATION_COMMAND",
            command_id=command.command_id,
            idempotency_key=command.idempotency_key,
            outcome=c.CommandOutcome.APPLIED,
            prior_case_revision=prior_revision,
            resulting_case_revision=prior_revision + 1,
            occurred_at=self.now(),
            rejection_code=None,
        )

    def _advance_revision(self, connection, case_id: UUID, prior_revision: int) -> None:
        from .persistence import cases as foundation_cases

        result = connection.execute(
            update(foundation_cases)
            .where(
                foundation_cases.c.id == str(case_id),
                foundation_cases.c.revision == prior_revision,
            )
            .values(revision=prior_revision + 1)
        )
        if result.rowcount != 1:
            raise FoundationProtocolError(c.FoundationRejectionCode.STALE_STATE)

    def _activate_context(self, connection, command, prior_revision):
        table = WORKSHOP_PROTOCOL_TABLES["workshop_analyzer_contexts"]
        case_table = WORKSHOP_PROTOCOL_TABLES["workshop_protocol_cases"]
        existing = connection.execute(
            select(table).where(table.c.context_id == str(command.context.context_id))
        ).mappings().one_or_none()
        if existing is not None:
            raise FoundationProtocolError(c.FoundationRejectionCode.DUPLICATE_CONFLICT)
        case = connection.execute(
            select(case_table).where(case_table.c.case_id == str(command.case_id))
        ).mappings().one()
        if command.context.source_set.source_set_hash != case["source_set_hash"]:
            raise FoundationProtocolError(c.FoundationRejectionCode.SOURCE_BINDING_FAILED)
        connection.execute(
            update(table)
            .where(table.c.case_id == str(command.case_id), table.c.status == c.ContextStatus.ACTIVE.value)
            .values(status=c.ContextStatus.REBUILD_REQUIRED.value, invalidated_at=_instant(self.now()))
        )
        connection.execute(
            insert(table).values(
                context_id=str(command.context.context_id),
                case_id=str(command.case_id),
                session_id=str(command.session_id),
                source_set_hash=command.context.source_set.source_set_hash,
                provider_conversation_id=command.context.provider_conversation_id,
                status=command.context.status.value,
                binding_json=command.context.model_dump_json(),
                created_at=_instant(command.context.created_at),
                invalidated_at=None,
            )
        )
        connection.execute(
            update(case_table)
            .where(case_table.c.case_id == str(command.case_id))
            .values(active_context_id=str(command.context.context_id), updated_at=_instant(self.now()))
        )
        self._advance_revision(connection, command.case_id, prior_revision)
        return c.AnalyzerContextCommandReceipt(
            receipt_type="ANALYZER_CONTEXT",
            command=self._base_receipt(command, prior_revision),
            context_id=command.context.context_id,
            context_status=c.ContextStatus.ACTIVE,
        )

    def _invalidate_context(self, connection, command, prior_revision):
        table = WORKSHOP_PROTOCOL_TABLES["workshop_analyzer_contexts"]
        result = connection.execute(
            update(table)
            .where(
                table.c.context_id == str(command.context_id),
                table.c.case_id == str(command.case_id),
                table.c.status == c.ContextStatus.ACTIVE.value,
            )
            .values(status=c.ContextStatus.REBUILD_REQUIRED.value, invalidated_at=_instant(self.now()))
        )
        if result.rowcount != 1:
            raise FoundationProtocolError(c.FoundationRejectionCode.UNKNOWN_REFERENCE)
        self._advance_revision(connection, command.case_id, prior_revision)
        return c.AnalyzerContextCommandReceipt(
            receipt_type="ANALYZER_CONTEXT",
            command=self._base_receipt(command, prior_revision),
            context_id=command.context_id,
            context_status=c.ContextStatus.REBUILD_REQUIRED,
        )

    def _record_transcript(self, connection, command, prior_revision):
        preparation = WORKSHOP_PROTOCOL_TABLES["workshop_preparations"]
        completed_at = connection.execute(
            select(preparation.c.workshop_complete_at).where(
                preparation.c.case_id == str(command.case_id)
            )
        ).scalar_one()
        if completed_at is not None:
            raise FoundationProtocolError(c.FoundationRejectionCode.INVALID_TRANSITION)
        table = WORKSHOP_PROTOCOL_TABLES["workshop_final_transcripts"]
        prior = connection.execute(
            select(func.max(table.c.sequence_number)).where(
                table.c.case_id == str(command.case_id),
                table.c.session_id == str(command.session_id),
            )
        ).scalar_one()
        expected = 1 if prior is None else prior + 1
        if command.transcript.sequence_number != expected:
            raise FoundationProtocolError(c.FoundationRejectionCode.TRANSCRIPT_BINDING_FAILED)
        if command.transcript.transcript_hash != transcript_hash(command.transcript.text):
            raise FoundationProtocolError(c.FoundationRejectionCode.TRANSCRIPT_BINDING_FAILED)
        if command.transcript.speaker_actor_id is not None:
            self._require_participant(
                connection, command.case_id, command.transcript.speaker_actor_id
            )
        connection.execute(
            insert(table).values(
                event_id=str(command.transcript.event_id),
                case_id=str(command.case_id),
                session_id=str(command.session_id),
                sequence_number=command.transcript.sequence_number,
                transcript_hash=command.transcript.transcript_hash,
                event_json=command.transcript.model_dump_json(),
                recorded_at=_instant(self.now()),
            )
        )
        # The transcript and its first eligible Analyzer work item deliberately
        # share this Foundation transaction. A caller can acknowledge this
        # receipt immediately; provider work begins only after commit.
        jobs = WORKSHOP_PROTOCOL_TABLES["workshop_analyzer_jobs"]
        event_id = command.transcript.event_id
        now = _instant(self.now())
        job_id = _runtime_id(command.case_id, "TURN_ANALYSIS", event_id)
        connection.execute(
            insert(jobs).values(
                job_id=str(job_id),
                case_id=str(command.case_id),
                session_id=str(command.session_id),
                operation=c.AnalyzerOperation.TURN_ANALYSIS.value,
                subject_id=str(event_id),
                dedupe_key=f"turn-analysis:{command.case_id}:{event_id}",
                priority=10,
                state="ANALYSIS_PENDING",
                provider_request_id=f"specops-turn_analysis-{job_id}",
                request_json=None,
                candidate_json=None,
                admission_receipt_json=None,
                attempt_count=0,
                lease_owner=None,
                lease_expires_at=None,
                available_at=now,
                last_error_code=None,
                created_at=now,
                updated_at=now,
            )
        )
        decision_reviews = WORKSHOP_PROTOCOL_TABLES["workshop_decision_views"]
        artifact_reviews = WORKSHOP_PROTOCOL_TABLES["workshop_artifact_reviews"]
        active_review = any(
            connection.execute(
                select(func.count()).select_from(table).where(
                    table.c.case_id == str(command.case_id),
                    table.c.current == 1,
                )
            ).scalar_one()
            for table in (decision_reviews, artifact_reviews)
        )
        self._consume_runway_and_schedule_guidance(
            connection,
            command.case_id,
            command.session_id,
            now,
            consume_question=not active_review,
        )
        self._advance_revision(connection, command.case_id, prior_revision)
        return c.TranscriptRecordedReceipt(
            receipt_type="TRANSCRIPT_RECORDED",
            command=self._base_receipt(command, prior_revision),
            transcript_event_id=command.transcript.event_id,
            transcript_hash=command.transcript.transcript_hash,
        )

    def _claim_workshop_complete(self, connection, command, prior_revision):
        preparations = WORKSHOP_PROTOCOL_TABLES["workshop_preparations"]
        preparation = connection.execute(
            select(preparations).where(preparations.c.case_id == str(command.case_id))
        ).mappings().one()
        if preparation["phase"] != "READY" or preparation["workshop_complete_at"] is not None:
            raise FoundationProtocolError(c.FoundationRejectionCode.INVALID_TRANSITION)
        case = connection.execute(
            select(foundation_cases).where(foundation_cases.c.id == str(command.case_id))
        ).mappings().one()
        if case["pm_actor_id"] != str(command.acting_actor_id):
            raise FoundationProtocolError(c.FoundationRejectionCode.AUTHORITY_FAILED)
        self._require_participant(connection, command.case_id, command.acting_actor_id)

        if command.completion_source is not c.WorkshopCompletionSource.BUTTON:
            assert command.completion_transcript_event_id is not None
            completion = self._participant_transcript(
                connection,
                command.case_id,
                command.completion_transcript_event_id,
                command.acting_actor_id,
            )
            if completion is None:
                raise FoundationProtocolError(c.FoundationRejectionCode.TRANSCRIPT_BINDING_FAILED)
            classification = classify_completion_utterance(completion.text)
            if command.completion_source is c.WorkshopCompletionSource.VOICE_EXPLICIT:
                if classification is not CompletionUtterance.EXPLICIT:
                    raise FoundationProtocolError(c.FoundationRejectionCode.CONFIRMATION_BINDING_FAILED)
            else:
                assert command.confirmation_transcript_event_id is not None
                confirmation = self._participant_transcript(
                    connection,
                    command.case_id,
                    command.confirmation_transcript_event_id,
                    command.acting_actor_id,
                )
                if (
                    classification is not CompletionUtterance.AMBIGUOUS
                    or confirmation is None
                    or confirmation.sequence_number != completion.sequence_number + 1
                    or classify_completion_utterance(confirmation.text)
                    is not CompletionUtterance.AFFIRMATIVE
                ):
                    raise FoundationProtocolError(c.FoundationRejectionCode.CONFIRMATION_BINDING_FAILED)

        completed_at = self.now()
        connection.execute(
            update(preparations)
            .where(preparations.c.case_id == str(command.case_id))
            .values(
                workshop_complete_at=_instant(completed_at),
                cleanup_state="FINISHING_ANALYSIS",
                cleanup_reason="WORKSHOP_COMPLETE",
                last_client_disconnected_at=None,
                restart_grace_until=None,
                cleanup_available_at=None,
                cleanup_last_error_code=None,
                updated_at=_instant(completed_at),
            )
        )
        self._advance_revision(connection, command.case_id, prior_revision)
        return c.WorkshopCompletionReceipt(
            receipt_type="WORKSHOP_COMPLETION",
            command=self._base_receipt(command, prior_revision),
            completion_source=command.completion_source,
            completed_at=completed_at,
            completion_transcript_event_id=command.completion_transcript_event_id,
            confirmation_transcript_event_id=command.confirmation_transcript_event_id,
            state=c.WorkshopCompletionState.FINISHING_ANALYSIS,
        )

    def _consume_runway_and_schedule_guidance(
        self,
        connection,
        case_id: UUID,
        session_id: UUID,
        now: str,
        *,
        consume_question: bool = True,
    ) -> None:
        runway = WORKSHOP_PROTOCOL_TABLES["workshop_runway_items"]
        guidance = WORKSHOP_PROTOCOL_TABLES["workshop_guidance"]
        jobs = WORKSHOP_PROTOCOL_TABLES["workshop_analyzer_jobs"]
        current_guidance = connection.execute(
            select(guidance.c.guidance_id).where(
                guidance.c.case_id == str(case_id),
                guidance.c.valid == 1,
            )
        ).scalar_one_or_none()
        if current_guidance is None:
            return
        current = connection.execute(
            select(runway)
            .where(
                runway.c.case_id == str(case_id),
                runway.c.guidance_id == current_guidance,
                runway.c.status == "AVAILABLE",
            )
            .order_by(runway.c.position)
        ).mappings().all()
        if current and consume_question:
            connection.execute(
                update(runway)
                .where(
                    runway.c.case_id == str(case_id),
                    runway.c.guidance_id == current_guidance,
                    runway.c.question_id == current[0]["question_id"],
                )
                .values(status="ASKED", consumed_at=now)
            )
        depth = max(0, len(current) - (1 if current and consume_question else 0))
        if depth > 2:
            return
        subject_id = str(current_guidance)
        existing = connection.execute(
            select(jobs.c.job_id).where(
                jobs.c.case_id == str(case_id),
                jobs.c.operation == c.AnalyzerOperation.GUIDANCE.value,
                jobs.c.subject_id == subject_id,
            )
        ).scalar_one_or_none()
        if existing is not None:
            return
        job_id = _runtime_id(case_id, "GUIDANCE", subject_id)
        connection.execute(
            insert(jobs).values(
                job_id=str(job_id),
                case_id=str(case_id),
                session_id=str(session_id),
                operation=c.AnalyzerOperation.GUIDANCE.value,
                subject_id=subject_id,
                dedupe_key=f"guidance:{case_id}:{subject_id}",
                priority=20,
                state="ANALYSIS_PENDING",
                provider_request_id=f"specops-guidance-{job_id}",
                request_json=None,
                candidate_json=None,
                admission_receipt_json=None,
                attempt_count=0,
                lease_owner=None,
                lease_expires_at=None,
                available_at=now,
                last_error_code=None,
                created_at=now,
                updated_at=now,
            )
        )

    def _active_context(self, connection, case_id: UUID, context_id: UUID):
        table = WORKSHOP_PROTOCOL_TABLES["workshop_analyzer_contexts"]
        return connection.execute(
            select(table).where(
                table.c.case_id == str(case_id),
                table.c.context_id == str(context_id),
                table.c.status == c.ContextStatus.ACTIVE.value,
            )
        ).mappings().one_or_none()

    def _admit_candidate(self, connection, command, prior_revision, groups):
        active_context = self._active_context(connection, command.case_id, command.context_id)
        if active_context is None:
            raise FoundationProtocolError(c.FoundationRejectionCode.PROVIDER_REQUEST_BINDING_FAILED)
        mapping_table = WORKSHOP_PROTOCOL_TABLES["workshop_candidate_mappings"]
        record_table = WORKSHOP_PROTOCOL_TABLES["workshop_semantic_records"]
        mappings = []
        allocated: dict[str, tuple[str, UUID, int]] = {}
        candidates: list[tuple[str, Any]] = []
        for entity_kind, field_name in groups:
            for candidate in getattr(command.candidate, field_name):
                key = candidate.candidate_key
                candidates.append((entity_kind, candidate))
                existing = connection.execute(
                    select(mapping_table).where(
                        mapping_table.c.analyzer_run_id == str(command.analyzer_run_id),
                        mapping_table.c.candidate_key == key,
                    )
                ).mappings().one_or_none()
                if existing is not None:
                    if existing["entity_kind"] != entity_kind:
                        raise FoundationProtocolError(c.FoundationRejectionCode.DUPLICATE_CONFLICT)
                    foundation_id = UUID(existing["foundation_id"])
                    record_version = existing["record_version"]
                else:
                    foundation_id = self.new_id()
                    record_version = 1
                    connection.execute(
                        insert(mapping_table).values(
                            analyzer_run_id=str(command.analyzer_run_id),
                            candidate_key=key,
                            case_id=str(command.case_id),
                            entity_kind=entity_kind,
                            foundation_id=str(foundation_id),
                            record_version=record_version,
                        )
                    )
                allocated[key] = (entity_kind, foundation_id, record_version)
                mappings.append(
                    c.CandidateIdentityMapping(
                        analyzer_run_id=command.analyzer_run_id,
                        candidate_key=key,
                        entity_kind=entity_kind,
                        foundation_id=foundation_id,
                        record_version=record_version,
                    )
                )
        # Provider-local keys are transport-local only.  Resolve every nested
        # reference before any semantic record enters Foundation storage.
        for entity_kind, candidate in candidates:
            key = candidate.candidate_key
            _, foundation_id, record_version = allocated[key]
            exists = connection.execute(
                select(record_table.c.foundation_id).where(
                    record_table.c.foundation_id == str(foundation_id),
                    record_table.c.record_version == record_version,
                )
            ).scalar_one_or_none()
            if exists is not None:
                continue
            if entity_kind == "EVIDENCE":
                payload = self._admit_evidence_candidate(
                    connection, command.case_id, candidate, active_context
                )
            else:
                payload = self._resolve_provider_refs(
                    connection,
                    command.case_id,
                    candidate.model_dump(mode="json", exclude_none=False),
                    allocated,
                )
                # The bootstrap candidate uses compact provider-local key
                # arrays. Resolve them to durable Foundation references before
                # persistence, just like the turn-analysis EntityRef union.
                for key_field, ref_field in {
                    "evidence_candidate_keys": "evidence_refs",
                    "problem_keys": "problem_refs",
                    "addresses_problem_keys": "addresses_problem_refs",
                    "prerequisite_problem_keys": "prerequisite_problem_refs",
                }.items():
                    if key_field in payload:
                        payload[ref_field] = [
                            {
                                "ref_kind": "FOUNDATION_ID",
                                "foundation_id": str(allocated[key][1]),
                                "expected_version": allocated[key][2],
                            }
                            for key in payload.pop(key_field)
                        ]
            if entity_kind == "FINDING":
                payload = self._admitted_evidence_finding(
                    connection,
                    command,
                    candidate,
                    payload,
                    foundation_id,
                    record_version,
                    active_context,
                )
            connection.execute(
                insert(record_table).values(
                    foundation_id=str(foundation_id),
                    record_version=record_version,
                    case_id=str(command.case_id),
                    entity_kind=entity_kind,
                    status=(
                        c.SemanticRecordStatus.PENDING_CONFIRMATION.value
                        if entity_kind == "DECISION"
                        else c.SemanticRecordStatus.OPEN.value
                    ),
                    content_hash=payload_hash(payload),
                    payload_json=_json(payload),
                    analyzer_run_id=str(command.analyzer_run_id),
                    candidate_key=key,
                    created_at=_instant(self.now()),
                )
            )
        if isinstance(command, c.AdmitTurnAnalysisCommand):
            self._apply_turn_revisions(connection, command, allocated)
            self._apply_problem_assessments(connection, command, allocated)
        admitted_guidance_id = None
        if isinstance(command, c.AdmitInterviewBriefCommand):
            admitted_guidance_id = self._admit_initial_runway(
                connection, command, allocated
            )
        self._advance_revision(connection, command.case_id, prior_revision)
        return c.ProposalAdmissionReceipt(
            receipt_type="PROPOSAL_ADMISSION",
            command=self._base_receipt(command, prior_revision),
            analyzer_run_id=command.analyzer_run_id,
            identity_mappings=tuple(mappings),
            admitted_guidance_id=admitted_guidance_id,
        )

    def _admit_initial_runway(self, connection, command, allocated):
        """Map BOOTSTRAP-local question keys into one governed initial runway.

        A short or unsafe candidate is still valid provisional semantic input,
        but it never becomes guidance. Preparation therefore fails closed
        without weakening the exact initial-runway Voice gate.
        """

        candidate = command.candidate
        runway = candidate.initial_runway
        keys = (
            runway.recommended_question_key,
            *runway.safe_alternate_question_keys,
        )
        if len(keys) != c.INITIAL_RUNWAY_DEPTH or len(set(keys)) != c.INITIAL_RUNWAY_DEPTH:
            return None
        questions = {item.candidate_key: item for item in candidate.questions}
        if any(key not in questions for key in keys):
            return None
        if any(not questions[key].safe_without_current_turn_interpretation for key in keys):
            return None
        # Every BOOTSTRAP problem is newly OPEN. A question with a prerequisite
        # therefore cannot yet be independently safe in the initial runway.
        if any(questions[key].prerequisite_problem_keys for key in keys):
            return None

        def admitted_question(key):
            entity_kind, foundation_id, version = allocated[key]
            if entity_kind != "QUESTION":
                raise FoundationProtocolError(c.FoundationRejectionCode.UNKNOWN_REFERENCE)
            question = questions[key]
            # Every prerequisite is bound to an admitted, still-current problem.
            for problem_key in (
                *question.addresses_problem_keys,
                *question.prerequisite_problem_keys,
            ):
                problem_kind, problem_id, problem_version = allocated[problem_key]
                if problem_kind != "PROBLEM":
                    raise FoundationProtocolError(c.FoundationRejectionCode.UNKNOWN_REFERENCE)
                self._semantic_record_for_ref(
                    connection,
                    command.case_id,
                    {
                        "foundation_id": str(problem_id),
                        "expected_version": problem_version,
                    },
                )
            return c.AdmittedGuidanceQuestion(
                question_id=foundation_id,
                question_version=version,
                exact_text=question.text,
                reason=question.rationale,
            )

        recommended = admitted_question(keys[0])
        alternatives = tuple(admitted_question(key) for key in keys[1:])
        dependencies = [
            c.AdmittedGuidanceDependency(
                dependency_kind=c.GuidanceDependencyKind.SOURCE_SET,
                entity_id=None,
                expected_version=None,
                source_set_hash=candidate.source_set_hash,
            )
        ]
        dependency_refs: set[tuple[c.GuidanceDependencyKind, UUID, int]] = set()
        for key in keys:
            question = questions[key]
            _, question_id, question_version = allocated[key]
            dependency_refs.add(
                (c.GuidanceDependencyKind.QUESTION, question_id, question_version)
            )
            for problem_key in (
                *question.addresses_problem_keys,
                *question.prerequisite_problem_keys,
            ):
                _, problem_id, problem_version = allocated[problem_key]
                dependency_refs.add(
                    (c.GuidanceDependencyKind.PROBLEM, problem_id, problem_version)
                )
        dependencies.extend(
            c.AdmittedGuidanceDependency(
                dependency_kind=kind,
                entity_id=identity,
                expected_version=version,
                source_set_hash=None,
            )
            for kind, identity, version in sorted(
                dependency_refs, key=lambda item: (item[0].value, str(item[1]), item[2])
            )
        )
        do_not_ask = tuple(
            c.FoundationEntityRef(
                ref_kind="FOUNDATION_ID",
                foundation_id=allocated[key][1],
                expected_version=allocated[key][2],
            )
            for key in runway.do_not_ask_question_keys
        )
        guidance_id = _runtime_id(command.analyzer_run_id, "initial-guidance")
        admitted = c.AdmittedGuidance(
            guidance_id=guidance_id,
            guidance_version=1,
            source_analyzer_run_id=command.analyzer_run_id,
            source_context_id=command.context_id,
            source_request_hash=command.provider_request_hash,
            based_on_case_revision=command.expected_case_revision,
            source_set_hash=candidate.source_set_hash,
            recommended_question=recommended,
            safe_alternates=alternatives,
            do_not_ask_questions=do_not_ask,
            dependencies=tuple(dependencies),
            acknowledgement_suggestion="The first clarification area is ready.",
            invalidation_triggers=tuple(
                sorted(
                    {
                        c.GuidanceInvalidationTrigger.SOURCE_SET_CHANGED,
                        c.GuidanceInvalidationTrigger.FOUNDATION_ENTITY_CHANGED,
                        c.GuidanceInvalidationTrigger.QUESTION_ANSWERED,
                        c.GuidanceInvalidationTrigger.PROBLEM_RESOLVED,
                        c.GuidanceInvalidationTrigger.GUIDANCE_SUPERSEDED,
                        c.GuidanceInvalidationTrigger.WORKSHOP_CLOSED,
                    },
                    key=lambda item: item.value,
                )
            ),
            admitted_at=self.now(),
        )
        guidance_table = WORKSHOP_PROTOCOL_TABLES["workshop_guidance"]
        runway_table = WORKSHOP_PROTOCOL_TABLES["workshop_runway_items"]
        connection.execute(
            update(guidance_table)
            .where(guidance_table.c.case_id == str(command.case_id))
            .values(valid=0)
        )
        connection.execute(
            insert(guidance_table).values(
                guidance_id=str(guidance_id),
                guidance_version=1,
                case_id=str(command.case_id),
                payload_json=admitted.model_dump_json(),
                valid=1,
                admitted_at=_instant(admitted.admitted_at),
            )
        )
        for position, question in enumerate(
            (admitted.recommended_question, *admitted.safe_alternates), start=1
        ):
            connection.execute(
                insert(runway_table).values(
                    case_id=str(command.case_id),
                    guidance_id=str(guidance_id),
                    question_id=str(question.question_id),
                    question_version=question.question_version,
                    position=position,
                    exact_text=question.exact_text,
                    reason=question.reason,
                    status="AVAILABLE",
                    admitted_at=_instant(admitted.admitted_at),
                    consumed_at=None,
                )
            )
        return guidance_id

    def _admit_evidence_candidate(self, connection, case_id, candidate, active_context):
        context = c.AnalyzerContextBinding.model_validate_json(active_context["binding_json"])
        source_binding = next(
            (
                item
                for item in context.source_set.ordered_sources
                if item.source.role is candidate.source_role
            ),
            None,
        )
        if source_binding is None:
            raise FoundationProtocolError(c.FoundationRejectionCode.SOURCE_BINDING_FAILED)
        source = source_binding.source
        registered = connection.execute(
            select(source_artifacts).where(
                source_artifacts.c.case_id == str(case_id),
                source_artifacts.c.artifact_id == str(source.source_id),
                source_artifacts.c.version == source.version,
                source_artifacts.c.content_hash == source.payload_hash.removeprefix("sha256:"),
                source_artifacts.c.canonical_locator == source.canonical_locator,
            )
        ).mappings().one_or_none()
        if registered is None:
            raise FoundationProtocolError(c.FoundationRejectionCode.SOURCE_BINDING_FAILED)
        try:
            source_bytes = Path(source.canonical_locator).read_bytes()
            source_text = source_bytes.decode("utf-8")
        except (OSError, UnicodeError) as exc:
            raise FoundationProtocolError(c.FoundationRejectionCode.SOURCE_BINDING_FAILED) from exc
        if "sha256:" + hashlib.sha256(source_bytes).hexdigest() != source.payload_hash:
            raise FoundationProtocolError(c.FoundationRejectionCode.SOURCE_BINDING_FAILED)
        locator = candidate.locator
        if isinstance(locator, c.SourceLineLocator):
            lines = source_text.splitlines()
            if locator.end_line > len(lines):
                raise FoundationProtocolError(c.FoundationRejectionCode.EVIDENCE_BINDING_FAILED)
            excerpt = "\n".join(lines[locator.start_line - 1 : locator.end_line])
        elif isinstance(locator, c.JsonPointerLocator):
            try:
                value = json.loads(source_text)
                for raw in locator.pointer.split("/")[1:]:
                    token = raw.replace("~1", "/").replace("~0", "~")
                    value = value[int(token)] if isinstance(value, list) else value[token]
                excerpt = _json(value) if isinstance(value, (dict, list)) else str(value)
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                raise FoundationProtocolError(c.FoundationRejectionCode.EVIDENCE_BINDING_FAILED) from exc
        elif isinstance(locator, c.DocumentAnchorLocator):
            matches = [line for line in source_text.splitlines() if locator.anchor in line]
            if len(matches) != 1:
                raise FoundationProtocolError(c.FoundationRejectionCode.EVIDENCE_BINDING_FAILED)
            excerpt = matches[0]
        else:
            assert isinstance(locator, c.QuoteSearchLocator)
            starts = []
            start = 0
            while True:
                found = source_text.find(locator.exact_quote, start)
                if found < 0:
                    break
                starts.append(found)
                start = found + 1
            if len(starts) < locator.occurrence:
                raise FoundationProtocolError(c.FoundationRejectionCode.EVIDENCE_BINDING_FAILED)
            excerpt = locator.exact_quote
        if candidate.quoted_text_candidate is not None and candidate.quoted_text_candidate != excerpt:
            raise FoundationProtocolError(c.FoundationRejectionCode.EVIDENCE_BINDING_FAILED)
        payload = candidate.model_dump(mode="json", exclude_none=False)
        payload["exact_excerpt"] = excerpt
        payload["source_binding"] = {
            "source_id": str(source.source_id),
            "source_version": source.version,
            "source_hash": source.payload_hash,
            "excerpt_hash": "sha256:" + hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
        }
        return payload

    def _admitted_evidence_finding(
        self,
        connection,
        command,
        candidate,
        payload,
        foundation_id,
        record_version,
        active_context,
    ):
        claim = self._semantic_record_for_ref(connection, command.case_id, payload["claim_ref"])
        evidence = self._semantic_record_for_ref(
            connection, command.case_id, payload["evidence_ref"]
        )
        if evidence["entity_kind"] != "EVIDENCE":
            raise FoundationProtocolError(c.FoundationRejectionCode.EVIDENCE_BINDING_FAILED)
        evidence_payload = json.loads(evidence["payload_json"])
        source = evidence_payload.get("source_binding")
        if source is None:
            raise FoundationProtocolError(c.FoundationRejectionCode.EVIDENCE_BINDING_FAILED)
        context = c.AnalyzerContextBinding.model_validate_json(active_context["binding_json"])
        admitted = c.AdmittedSemanticEvidenceFinding(
            finding_id=foundation_id,
            finding_version=record_version,
            claim_id=UUID(claim["foundation_id"]),
            claim_version=claim["record_version"],
            claim_hash=claim["content_hash"],
            evidence_id=UUID(evidence["foundation_id"]),
            evidence_version=evidence["record_version"],
            source_hash=source["source_hash"],
            excerpt_hash=source["excerpt_hash"],
            assessment=candidate.assessment,
            confidence=candidate.confidence,
            source_analyzer_run_id=command.analyzer_run_id,
            analyzer_contract=context.analyzer_contract,
            admitted_at=self.now(),
        )
        return admitted.model_dump(mode="json", exclude_none=False)

    def _apply_turn_revisions(self, connection, command, allocated):
        records = WORKSHOP_PROTOCOL_TABLES["workshop_semantic_records"]
        for entity_kind, revisions in (
            ("CLUSTER", command.candidate.revised_problem_clusters),
            ("QUESTION", command.candidate.revised_questions),
        ):
            for revision in revisions:
                ref = revision.cluster_ref if entity_kind == "CLUSTER" else revision.question_ref
                prior = self._semantic_record_for_ref(connection, command.case_id, ref)
                if prior["entity_kind"] != entity_kind:
                    raise FoundationProtocolError(c.FoundationRejectionCode.UNKNOWN_REFERENCE)
                latest = connection.execute(
                    select(func.max(records.c.record_version)).where(
                        records.c.foundation_id == str(ref.foundation_id)
                    )
                ).scalar_one()
                if latest != ref.expected_version:
                    raise FoundationProtocolError(c.FoundationRejectionCode.STALE_ENTITY)
                payload = revision.model_dump(mode="json", exclude_none=False)
                payload.pop("cluster_ref" if entity_kind == "CLUSTER" else "question_ref")
                payload = self._resolve_provider_refs(
                    connection, command.case_id, payload, allocated
                )
                next_version = ref.expected_version + 1
                connection.execute(
                    update(records)
                    .where(
                        records.c.foundation_id == str(ref.foundation_id),
                        records.c.record_version == ref.expected_version,
                    )
                    .values(status=c.SemanticRecordStatus.STALE.value)
                )
                connection.execute(
                    insert(records).values(
                        foundation_id=str(ref.foundation_id),
                        record_version=next_version,
                        case_id=str(command.case_id),
                        entity_kind=entity_kind,
                        status=c.SemanticRecordStatus.OPEN.value,
                        content_hash=payload_hash(payload),
                        payload_json=_json(payload),
                        analyzer_run_id=str(command.analyzer_run_id),
                        candidate_key=f"revision-{ref.foundation_id}-{next_version}",
                        created_at=_instant(self.now()),
                    )
                )

    def _apply_problem_assessments(self, connection, command, allocated):
        records = WORKSHOP_PROTOCOL_TABLES["workshop_semantic_records"]
        for assessment in command.candidate.problem_assessments:
            ref = self._resolve_provider_refs(
                connection,
                command.case_id,
                assessment.problem_ref.model_dump(mode="json"),
                allocated,
            )
            problem = self._semantic_record_for_ref(connection, command.case_id, ref)
            if problem["entity_kind"] != "PROBLEM":
                raise FoundationProtocolError(c.FoundationRejectionCode.UNKNOWN_REFERENCE)
            status = (
                c.SemanticRecordStatus.RESOLVED
                if assessment.assessment == "RESOLVED"
                else c.SemanticRecordStatus.OPEN
            )
            connection.execute(
                update(records)
                .where(
                    records.c.foundation_id == problem["foundation_id"],
                    records.c.record_version == problem["record_version"],
                )
                .values(status=status.value)
            )

    def _resolve_provider_refs(self, connection, case_id: UUID, value, allocated):
        if isinstance(value, dict):
            if value.get("ref_kind") == "CANDIDATE_KEY":
                key = value.get("candidate_key")
                if key not in allocated:
                    raise FoundationProtocolError(c.FoundationRejectionCode.UNKNOWN_REFERENCE)
                _, foundation_id, version = allocated[key]
                return {
                    "ref_kind": "FOUNDATION_ID",
                    "foundation_id": str(foundation_id),
                    "expected_version": version,
                }
            if value.get("ref_kind") == "FOUNDATION_ID":
                self._semantic_record_for_ref(connection, case_id, value)
                return dict(value)
            return {
                key: self._resolve_provider_refs(connection, case_id, item, allocated)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self._resolve_provider_refs(connection, case_id, item, allocated) for item in value]
        return value

    @staticmethod
    def _semantic_record_for_ref(connection, case_id: UUID, ref):
        foundation_id = str(ref.foundation_id if hasattr(ref, "foundation_id") else ref["foundation_id"])
        expected_version = (
            ref.expected_version if hasattr(ref, "expected_version") else ref["expected_version"]
        )
        table = WORKSHOP_PROTOCOL_TABLES["workshop_semantic_records"]
        row = connection.execute(
            select(table).where(
                table.c.case_id == str(case_id),
                table.c.foundation_id == foundation_id,
                table.c.record_version == expected_version,
                table.c.status != c.SemanticRecordStatus.STALE.value,
            )
        ).mappings().one_or_none()
        if row is None:
            raise FoundationProtocolError(c.FoundationRejectionCode.STALE_ENTITY)
        return row

    def _admit_guidance(self, connection, command, prior_revision):
        if self._active_context(connection, command.case_id, command.context_id) is None:
            raise FoundationProtocolError(c.FoundationRejectionCode.PROVIDER_REQUEST_BINDING_FAILED)
        case = connection.execute(
            select(WORKSHOP_PROTOCOL_TABLES["workshop_protocol_cases"]).where(
                WORKSHOP_PROTOCOL_TABLES["workshop_protocol_cases"].c.case_id
                == str(command.case_id)
            )
        ).mappings().one()
        if command.candidate.source_set_hash != case["source_set_hash"]:
            raise FoundationProtocolError(c.FoundationRejectionCode.SOURCE_BINDING_FAILED)

        guidance_table = WORKSHOP_PROTOCOL_TABLES["workshop_guidance"]
        runway_table = WORKSHOP_PROTOCOL_TABLES["workshop_runway_items"]
        current_guidance_row = connection.execute(
            select(guidance_table).where(
                guidance_table.c.case_id == str(command.case_id),
                guidance_table.c.valid == 1,
            )
        ).mappings().one_or_none()
        prior_available_rows = (
            []
            if current_guidance_row is None
            else connection.execute(
                select(runway_table)
                .where(
                    runway_table.c.case_id == str(command.case_id),
                    runway_table.c.guidance_id == current_guidance_row["guidance_id"],
                    runway_table.c.status == "AVAILABLE",
                )
                .order_by(runway_table.c.position)
            ).mappings().all()
        )
        asked_rows = connection.execute(
            select(runway_table)
            .where(
                runway_table.c.case_id == str(command.case_id),
                runway_table.c.status == "ASKED",
            )
            .order_by(runway_table.c.consumed_at, runway_table.c.position)
        ).mappings().all()
        asked_by_question = {
            (row["question_id"], row["question_version"]): row for row in asked_rows
        }

        derived_dependency_refs: set[
            tuple[c.GuidanceDependencyKind, UUID, int]
        ] = set()

        def admitted_question(question, *, track_dependencies=False):
            row = self._semantic_record_for_ref(connection, command.case_id, question.question_ref)
            if row["entity_kind"] != "QUESTION":
                raise FoundationProtocolError(c.FoundationRejectionCode.UNKNOWN_REFERENCE)
            payload = json.loads(row["payload_json"])
            if payload.get("text") != question.exact_text:
                raise FoundationProtocolError(c.FoundationRejectionCode.STALE_ENTITY)
            if not payload.get("safe_without_current_turn_interpretation", False):
                raise FoundationProtocolError(c.FoundationRejectionCode.INVALID_TRANSITION)
            question_dependency_refs = {
                (
                    c.GuidanceDependencyKind.QUESTION,
                    question.question_ref.foundation_id,
                    question.question_ref.expected_version,
                )
            }
            addressed = payload.get("addresses_problem_refs", ())
            prerequisites = payload.get("prerequisite_problem_refs", ())

            def require_problem_status(problem_ref, required_status):
                problem = self._semantic_record_for_ref(
                    connection, command.case_id, problem_ref
                )
                if problem["entity_kind"] != "PROBLEM":
                    raise FoundationProtocolError(c.FoundationRejectionCode.UNKNOWN_REFERENCE)
                if problem["status"] != required_status:
                    raise _GuidanceBranchUnavailable
                question_dependency_refs.add(
                    (
                        c.GuidanceDependencyKind.PROBLEM,
                        UUID(problem_ref["foundation_id"]),
                        problem_ref["expected_version"],
                    )
                )

            for problem_ref in addressed:
                require_problem_status(
                    problem_ref, c.SemanticRecordStatus.OPEN.value
                )
            for problem_ref in prerequisites:
                require_problem_status(
                    problem_ref, c.SemanticRecordStatus.RESOLVED.value
                )
            if track_dependencies:
                derived_dependency_refs.update(question_dependency_refs)
            return c.AdmittedGuidanceQuestion(
                question_id=question.question_ref.foundation_id,
                question_version=question.question_ref.expected_version,
                exact_text=question.exact_text,
                reason=question.reason,
            )

        proposed = (
            command.candidate.recommended_question,
            *command.candidate.safe_alternates,
        )
        admitted_proposed_values: list[c.AdmittedGuidanceQuestion] = []
        for item in proposed:
            identity = (
                str(item.question_ref.foundation_id),
                item.question_ref.expected_version,
            )
            if identity in asked_by_question:
                # An exact previously asked question can never re-enter the
                # runway, but it must not discard independent safe selections.
                continue
            try:
                admitted_proposed_values.append(admitted_question(item))
            except _GuidanceBranchUnavailable:
                # A resolved target or unresolved prerequisite invalidates only
                # this branch. Independent current selections remain admissible.
                continue
            except FoundationProtocolError as exc:
                # An existing question that requires current-turn
                # interpretation is not safe for the live runway. Quarantine
                # only that selected branch; stale question/text or unknown
                # refs still fail the complete GUIDANCE admission closed.
                if exc.code is not c.FoundationRejectionCode.INVALID_TRANSITION:
                    raise
        admitted_proposed = tuple(admitted_proposed_values)

        # Replenishment is additive: keep still-current live questions first,
        # then append newly selected, source-verified questions without duplicates.
        prior_available: list[c.AdmittedGuidanceQuestion] = []
        for row in prior_available_rows:
            if (row["question_id"], row["question_version"]) in asked_by_question:
                continue
            try:
                prior_available.append(
                    admitted_question(
                        c.GuidanceQuestion(
                            question_ref=c.FoundationEntityRef(
                                ref_kind="FOUNDATION_ID",
                                foundation_id=UUID(row["question_id"]),
                                expected_version=row["question_version"],
                            ),
                            exact_text=row["exact_text"],
                            reason=row["reason"],
                        )
                    )
                )
            except _GuidanceBranchUnavailable:
                continue
            except FoundationProtocolError as exc:
                if exc.code not in {
                    c.FoundationRejectionCode.STALE_ENTITY,
                    c.FoundationRejectionCode.INVALID_TRANSITION,
                }:
                    raise

        records = WORKSHOP_PROTOCOL_TABLES["workshop_semantic_records"]
        eligible_foundation_questions: list[c.AdmittedGuidanceQuestion] = []
        for row in connection.execute(
            select(records)
            .where(
                records.c.case_id == str(command.case_id),
                records.c.entity_kind == "QUESTION",
                records.c.status == c.SemanticRecordStatus.OPEN.value,
            )
            .order_by(records.c.foundation_id, records.c.record_version)
        ).mappings():
            identity = (row["foundation_id"], row["record_version"])
            if identity in asked_by_question:
                continue
            payload = json.loads(row["payload_json"])
            try:
                eligible_foundation_questions.append(
                    admitted_question(
                        c.GuidanceQuestion(
                            question_ref=c.FoundationEntityRef(
                                ref_kind="FOUNDATION_ID",
                                foundation_id=UUID(row["foundation_id"]),
                                expected_version=row["record_version"],
                            ),
                            exact_text=payload["text"],
                            reason=payload["rationale"],
                        )
                    )
                )
            except _GuidanceBranchUnavailable:
                continue
            except FoundationProtocolError as exc:
                if exc.code is not c.FoundationRejectionCode.INVALID_TRANSITION:
                    raise

        severity_priority = {
            c.Severity.CRITICAL.value: 0,
            c.Severity.HIGH.value: 1,
            c.Severity.MEDIUM.value: 2,
            c.Severity.LOW.value: 3,
        }

        def guidance_priority(question: c.AdmittedGuidanceQuestion) -> int:
            row = self._semantic_record_for_ref(
                connection,
                command.case_id,
                {
                    "foundation_id": str(question.question_id),
                    "expected_version": question.question_version,
                },
            )
            payload = json.loads(row["payload_json"])
            return min(
                severity_priority[
                    json.loads(
                        self._semantic_record_for_ref(
                            connection, command.case_id, problem_ref
                        )["payload_json"]
                    )["severity"]
                ]
                for problem_ref in payload["addresses_problem_refs"]
            )

        merged_pool: list[c.AdmittedGuidanceQuestion] = []
        merged_ids: set[tuple[UUID, int]] = set()
        for question in (
            *prior_available,
            *admitted_proposed,
            *eligible_foundation_questions,
        ):
            identity = (question.question_id, question.question_version)
            if identity in merged_ids:
                continue
            merged_pool.append(question)
            merged_ids.add(identity)
        merged = sorted(merged_pool, key=guidance_priority)[
            :POST_BOOTSTRAP_RUNWAY_TARGET
        ]
        for ref in command.candidate.do_not_ask_question_refs:
            if self._semantic_record_for_ref(connection, command.case_id, ref)["entity_kind"] != "QUESTION":
                raise FoundationProtocolError(c.FoundationRejectionCode.UNKNOWN_REFERENCE)
        merged_identities = {
            (item.question_id, item.question_version) for item in merged
        }
        # Terra's do-not-ask list cannot suppress a question that Foundation
        # independently proved safe and eligible. Keep only genuinely unselected
        # refs in the admitted diagnostic list.
        do_not_ask = tuple(
            item
            for item in command.candidate.do_not_ask_question_refs
            if (item.foundation_id, item.expected_version) not in merged_identities
        )
        if not merged:
            raise FoundationProtocolError(c.FoundationRejectionCode.INVALID_TRANSITION)
        recommended, *alternate_values = merged
        alternates = tuple(alternate_values)
        for item in merged:
            admitted_question(
                c.GuidanceQuestion(
                    question_ref=c.FoundationEntityRef(
                        ref_kind="FOUNDATION_ID",
                        foundation_id=item.question_id,
                        expected_version=item.question_version,
                    ),
                    exact_text=item.exact_text,
                    reason=item.reason,
                ),
                track_dependencies=True,
            )

        dependencies = []
        admitted_dependency_refs: set[
            tuple[c.GuidanceDependencyKind, UUID, int]
        ] = set()
        triggers = {
            c.GuidanceInvalidationTrigger.GUIDANCE_SUPERSEDED,
            c.GuidanceInvalidationTrigger.WORKSHOP_CLOSED,
        }
        for dependency in command.candidate.dependencies:
            if dependency.dependency_kind is c.GuidanceDependencyKind.SOURCE_SET:
                dependencies.append(
                    c.AdmittedGuidanceDependency(
                        dependency_kind=dependency.dependency_kind,
                        entity_id=None,
                        expected_version=None,
                        source_set_hash=command.candidate.source_set_hash,
                    )
                )
                triggers.add(c.GuidanceInvalidationTrigger.SOURCE_SET_CHANGED)
                continue
            assert dependency.entity_ref is not None
            row = self._semantic_record_for_ref(connection, command.case_id, dependency.entity_ref)
            expected_kind = {
                c.GuidanceDependencyKind.QUESTION: "QUESTION",
                c.GuidanceDependencyKind.PROBLEM: "PROBLEM",
            }.get(dependency.dependency_kind)
            if expected_kind is not None and row["entity_kind"] != expected_kind:
                raise FoundationProtocolError(c.FoundationRejectionCode.UNKNOWN_REFERENCE)
            dependencies.append(
                c.AdmittedGuidanceDependency(
                    dependency_kind=dependency.dependency_kind,
                    entity_id=dependency.entity_ref.foundation_id,
                    expected_version=dependency.entity_ref.expected_version,
                    source_set_hash=None,
                )
            )
            admitted_dependency_refs.add(
                (
                    dependency.dependency_kind,
                    dependency.entity_ref.foundation_id,
                    dependency.entity_ref.expected_version,
                )
            )
            triggers.add(c.GuidanceInvalidationTrigger.FOUNDATION_ENTITY_CHANGED)
            if dependency.dependency_kind is c.GuidanceDependencyKind.QUESTION:
                triggers.add(c.GuidanceInvalidationTrigger.QUESTION_ANSWERED)
            if dependency.dependency_kind is c.GuidanceDependencyKind.PROBLEM:
                triggers.add(c.GuidanceInvalidationTrigger.PROBLEM_RESOLVED)

        # Foundation binds every selected question and each current open
        # problem it addresses, even if Terra omitted those dependency rows.
        # This strengthens admission without changing the GUIDANCE schema or
        # allowing GUIDANCE to create a question.
        for kind, identity, version in sorted(
            derived_dependency_refs - admitted_dependency_refs,
            key=lambda item: (item[0].value, str(item[1]), item[2]),
        ):
            dependencies.append(
                c.AdmittedGuidanceDependency(
                    dependency_kind=kind,
                    entity_id=identity,
                    expected_version=version,
                    source_set_hash=None,
                )
            )
            triggers.add(c.GuidanceInvalidationTrigger.FOUNDATION_ENTITY_CHANGED)
            if kind is c.GuidanceDependencyKind.QUESTION:
                triggers.add(c.GuidanceInvalidationTrigger.QUESTION_ANSWERED)
            else:
                triggers.add(c.GuidanceInvalidationTrigger.PROBLEM_RESOLVED)

        guidance_id = self.new_id()
        admitted = c.AdmittedGuidance(
            guidance_id=guidance_id,
            guidance_version=1,
            source_analyzer_run_id=command.analyzer_run_id,
            source_context_id=command.context_id,
            source_request_hash=command.provider_request_hash,
            based_on_case_revision=command.expected_case_revision,
            source_set_hash=command.candidate.source_set_hash,
            recommended_question=recommended,
            safe_alternates=alternates,
            do_not_ask_questions=do_not_ask,
            dependencies=tuple(dependencies),
            acknowledgement_suggestion=command.candidate.acknowledgement_suggestion,
            invalidation_triggers=tuple(sorted(triggers, key=lambda item: item.value)),
            admitted_at=self.now(),
        )
        connection.execute(
            update(guidance_table)
            .where(guidance_table.c.case_id == str(command.case_id))
            .values(valid=0)
        )
        connection.execute(
            insert(guidance_table).values(
                guidance_id=str(guidance_id),
                guidance_version=1,
                case_id=str(command.case_id),
                payload_json=admitted.model_dump_json(),
                valid=1,
                admitted_at=_instant(admitted.admitted_at),
            )
        )
        for position, question in enumerate((recommended, *alternates), start=1):
            connection.execute(
                insert(runway_table).values(
                    case_id=str(command.case_id),
                    guidance_id=str(guidance_id),
                    question_id=str(question.question_id),
                    question_version=question.question_version,
                    position=position,
                    exact_text=question.exact_text,
                    reason=question.reason,
                    status="AVAILABLE",
                    admitted_at=_instant(admitted.admitted_at),
                    consumed_at=None,
                )
            )
        for position, row in enumerate(
            asked_by_question.values(), start=len(merged) + 1
        ):
            connection.execute(
                insert(runway_table).values(
                    case_id=str(command.case_id),
                    guidance_id=str(guidance_id),
                    question_id=row["question_id"],
                    question_version=row["question_version"],
                    position=position,
                    exact_text=row["exact_text"],
                    reason=row["reason"],
                    status="ASKED",
                    admitted_at=row["admitted_at"],
                    consumed_at=row["consumed_at"],
                )
            )
        self._advance_revision(connection, command.case_id, prior_revision)
        return c.ProposalAdmissionReceipt(
            receipt_type="PROPOSAL_ADMISSION",
            command=self._base_receipt(command, prior_revision),
            analyzer_run_id=command.analyzer_run_id,
            identity_mappings=(),
            admitted_guidance_id=guidance_id,
        )

    def _capture_low_risk_fact(self, connection, command, prior_revision):
        records = WORKSHOP_PROTOCOL_TABLES["workshop_semantic_records"]
        fact = connection.execute(
            select(records).where(
                records.c.case_id == str(command.case_id),
                records.c.foundation_id == str(command.fact_proposal_id),
                records.c.entity_kind == "FACT",
                records.c.status == c.SemanticRecordStatus.OPEN.value,
            ).order_by(records.c.record_version.desc()).limit(1)
        ).mappings().one_or_none()
        if fact is None:
            raise FoundationProtocolError(c.FoundationRejectionCode.UNKNOWN_REFERENCE)
        payload = json.loads(fact["payload_json"])
        source_question = payload.get("source_question_ref", {})
        if (
            source_question.get("foundation_id") != str(command.question_id)
            or source_question.get("expected_version") != command.expected_question_version
        ):
            raise FoundationProtocolError(c.FoundationRejectionCode.CONFIRMATION_BINDING_FAILED)
        transcript = self._participant_transcript(
            connection,
            command.case_id,
            command.transcript_event_id,
            command.speaker_actor_id,
        )
        if transcript is None:
            raise FoundationProtocolError(c.FoundationRejectionCode.TRANSCRIPT_BINDING_FAILED)
        self._require_participant(connection, command.case_id, command.acting_actor_id)
        connection.execute(
            update(records)
            .where(
                records.c.foundation_id == str(command.fact_proposal_id),
                records.c.record_version == fact["record_version"],
            )
            .values(status=c.SemanticRecordStatus.CONFIRMED.value)
        )
        self._advance_revision(connection, command.case_id, prior_revision)
        return c.LowRiskFactCaptureReceipt(
            receipt_type="LOW_RISK_FACT_CAPTURE",
            command=self._base_receipt(command, prior_revision),
            fact_id=command.fact_proposal_id,
            fact_version=fact["record_version"],
            promoted_to_pending_decision_id=None,
        )

    def _participant_transcript(
        self,
        connection,
        case_id: UUID,
        transcript_event_id: UUID,
        speaker_actor_id: UUID,
    ):
        table = WORKSHOP_PROTOCOL_TABLES["workshop_final_transcripts"]
        row = connection.execute(
            select(table).where(
                table.c.case_id == str(case_id),
                table.c.event_id == str(transcript_event_id),
            )
        ).mappings().one_or_none()
        if row is None:
            return None
        event = c.TranscriptFinalizedEvent.model_validate_json(row["event_json"])
        return event if event.speaker_actor_id == speaker_actor_id else None

    @staticmethod
    def _require_participant(connection, case_id: UUID, actor_id: UUID) -> None:
        exists = connection.execute(
            select(case_participants.c.actor_id).where(
                case_participants.c.case_id == str(case_id),
                case_participants.c.actor_id == str(actor_id),
            )
        ).scalar_one_or_none()
        if exists is None:
            raise FoundationProtocolError(c.FoundationRejectionCode.AUTHORITY_FAILED)

    def _has_domain_authority(self, connection, case_id: UUID, actor_id: UUID, domain: str) -> bool:
        case = connection.execute(
            select(foundation_cases).where(foundation_cases.c.id == str(case_id))
        ).mappings().one()
        if domain in {"BUSINESS", "PRODUCT", "CROSS_DOMAIN", "POLICY", "product", "policy"}:
            return case["pm_actor_id"] == str(actor_id)
        if domain in {"TECHNICAL", "technical"} and case["dev_lead_actor_id"] == str(actor_id):
            return True
        now = self.now()
        rows = connection.execute(
            select(delegations).where(
                delegations.c.case_id == str(case_id),
                delegations.c.delegate_id == str(actor_id),
                delegations.c.revoked_at.is_(None),
                delegations.c.domain == "TECHNICAL",
            )
        ).mappings()
        return any(row["valid_from"] <= now <= row["valid_until"] for row in rows)

    def _admit_narration(self, connection, command, prior_revision):
        view = WORKSHOP_PROTOCOL_TABLES["workshop_decision_views"]
        row = connection.execute(
            select(view).where(
                view.c.view_id == str(command.candidate.decision_batch_view_id),
                view.c.case_id == str(command.case_id),
                view.c.view_hash == command.candidate.decision_batch_view_hash,
                view.c.current == 1,
            )
        ).mappings().one_or_none()
        if row is None:
            raise FoundationProtocolError(c.FoundationRejectionCode.STALE_VIEW)
        expected_handles = {item["handle"] for item in json.loads(row["view_json"])["items"]}
        actual_handles = {item.handle for item in command.candidate.items}
        if actual_handles != expected_handles:
            raise FoundationProtocolError(c.FoundationRejectionCode.CONFIRMATION_BINDING_FAILED)
        table = WORKSHOP_PROTOCOL_TABLES["workshop_review_narrations"]
        narration_id = self.new_id()
        connection.execute(
            insert(table).values(
                narration_id=str(narration_id),
                narration_version=1,
                case_id=str(command.case_id),
                decision_batch_view_id=str(command.candidate.decision_batch_view_id),
                payload_json=command.candidate.model_dump_json(),
                valid=1,
                admitted_at=_instant(self.now()),
            )
        )
        self._advance_revision(connection, command.case_id, prior_revision)
        return c.ReviewNarrationAdmissionReceipt(
            receipt_type="REVIEW_NARRATION_ADMISSION",
            command=self._base_receipt(command, prior_revision),
            narration_id=narration_id,
            narration_version=1,
            decision_batch_view_id=command.candidate.decision_batch_view_id,
        )

    def _materialize_decision_review(self, connection, command, prior_revision):
        records = WORKSHOP_PROTOCOL_TABLES["workshop_semantic_records"]
        rows = []
        for decision_id in command.pending_decision_ids:
            row = connection.execute(
                select(records).where(
                    records.c.case_id == str(command.case_id),
                    records.c.foundation_id == str(decision_id),
                    records.c.entity_kind == "DECISION",
                    records.c.status == c.SemanticRecordStatus.PENDING_CONFIRMATION.value,
                )
                .order_by(records.c.record_version.desc())
                .limit(1)
            ).mappings().one_or_none()
            if row is None:
                raise FoundationProtocolError(c.FoundationRejectionCode.UNKNOWN_REFERENCE)
            rows.append(row)
        view_id = self.new_id()
        items = []
        for index, row in enumerate(rows):
            payload = json.loads(row["payload_json"])
            handle = chr(ord("A") + index)
            origins = []
            for link in payload["problem_links"]:
                if link["problem_ref"]["ref_kind"] == "FOUNDATION_ID":
                    problem_id = link["problem_ref"]["foundation_id"]
                    problem_version = link["problem_ref"]["expected_version"]
                else:
                    raise FoundationProtocolError(c.FoundationRejectionCode.UNKNOWN_REFERENCE)
                problem = connection.execute(
                    select(records).where(
                        records.c.case_id == str(command.case_id),
                        records.c.foundation_id == problem_id,
                        records.c.record_version == problem_version,
                    )
                ).mappings().one_or_none()
                if problem is None:
                    raise FoundationProtocolError(c.FoundationRejectionCode.UNKNOWN_REFERENCE)
                problem_payload = json.loads(problem["payload_json"])
                origins.append(
                    c.ProblemOriginView(
                        problem_id=UUID(problem_id),
                        problem_version=problem_version,
                        problem_statement=problem_payload["statement"],
                        resolution_kind=c.ProblemResolutionKind(link["resolution_kind"]),
                        evidence_summary=problem_payload["consequence"],
                    )
                )
            items.append(
                c.DecisionReviewItemView(
                    review_item_id=self.new_id(),
                    handle=handle,
                    pending_decision_id=UUID(row["foundation_id"]),
                    pending_decision_version=row["record_version"],
                    classification=c.Domain(payload["classification"]),
                    exact_statement=payload["statement"],
                    rationale=payload["rationale"],
                    problem_origins=tuple(origins),
                )
            )
        base = {
            "protocol_version": "1.0.0",
            "view_type": "DECISION_BATCH_REVIEW",
            "view_id": view_id,
            "view_hash": "sha256:" + "0" * 64,
            "session_id": command.session_id,
            # The view becomes observable only at this command's resulting
            # revision; narration must bind that exact current snapshot.
            "based_on_case_revision": prior_revision + 1,
            "derived_from_cluster_ids": command.derived_from_cluster_ids,
            "items": tuple(items),
            "generated_at": self.now(),
        }
        base["view_hash"] = decision_view_hash(base)
        view = c.DecisionBatchReviewView(**base)
        table = WORKSHOP_PROTOCOL_TABLES["workshop_decision_views"]
        connection.execute(
            update(table).where(table.c.case_id == str(command.case_id)).values(current=0)
        )
        connection.execute(
            insert(table).values(
                view_id=str(view_id),
                case_id=str(command.case_id),
                view_hash=view.view_hash,
                based_on_case_revision=prior_revision + 1,
                view_json=view.model_dump_json(),
                current=1,
                generated_at=_instant(view.generated_at),
            )
        )
        self._advance_revision(connection, command.case_id, prior_revision)
        return c.DecisionBatchReviewReceipt(
            receipt_type="DECISION_BATCH_REVIEW",
            command=self._base_receipt(command, prior_revision),
            view_id=view_id,
            view_hash=view.view_hash,
        )

    def _apply_decision_response(self, connection, command, prior_revision):
        views = WORKSHOP_PROTOCOL_TABLES["workshop_decision_views"]
        view = connection.execute(
            select(views).where(
                views.c.view_id == str(command.decision_batch_view_id),
                views.c.view_hash == command.decision_batch_view_hash,
                views.c.case_id == str(command.case_id),
                views.c.current == 1,
            )
        ).mappings().one_or_none()
        if view is None:
            raise FoundationProtocolError(c.FoundationRejectionCode.STALE_VIEW)
        self._require_participant(connection, command.case_id, command.acting_actor_id)
        response_transcript = self._participant_transcript(
            connection,
            command.case_id,
            command.response_transcript_event_id,
            command.acting_actor_id,
        )
        if response_transcript is None:
            raise FoundationProtocolError(c.FoundationRejectionCode.TRANSCRIPT_BINDING_FAILED)
        view_items = {
            (
                item["review_item_id"],
                item["handle"],
                item["pending_decision_id"],
                item["pending_decision_version"],
            )
            for item in json.loads(view["view_json"])["items"]
        }
        records = WORKSHOP_PROTOCOL_TABLES["workshop_semantic_records"]
        results = []
        for action in command.item_actions:
            if action.revision_span is not None and (
                action.revision_span.transcript_event_id
                != command.response_transcript_event_id
                or action.revision_span.end_character_exclusive
                > len(response_transcript.text)
            ):
                raise FoundationProtocolError(
                    c.FoundationRejectionCode.TRANSCRIPT_BINDING_FAILED
                )
            action_binding = (
                str(action.review_item_id),
                action.handle,
                str(action.pending_decision_id),
                action.expected_pending_decision_version,
            )
            if action_binding not in view_items:
                raise FoundationProtocolError(c.FoundationRejectionCode.CONFIRMATION_BINDING_FAILED)
            row = connection.execute(
                select(records).where(
                    records.c.case_id == str(command.case_id),
                    records.c.foundation_id == str(action.pending_decision_id),
                    records.c.record_version == action.expected_pending_decision_version,
                    records.c.status == c.SemanticRecordStatus.PENDING_CONFIRMATION.value,
                )
            ).mappings().one_or_none()
            if row is None:
                results.append(
                    c.DecisionBatchItemReceipt(
                        review_item_id=action.review_item_id,
                        pending_decision_id=action.pending_decision_id,
                        outcome=c.DecisionItemOutcome.REJECTED_STALE,
                        committed_decision_id=None,
                        revision_request_id=None,
                    )
                )
                continue
            decision_payload = json.loads(row["payload_json"])
            if not self._has_domain_authority(
                connection,
                command.case_id,
                command.acting_actor_id,
                decision_payload["classification"],
            ):
                results.append(
                    c.DecisionBatchItemReceipt(
                        review_item_id=action.review_item_id,
                        pending_decision_id=action.pending_decision_id,
                        outcome=c.DecisionItemOutcome.REJECTED_AUTHORITY,
                        committed_decision_id=None,
                        revision_request_id=None,
                    )
                )
                continue
            outcome = {
                c.ConfirmationAction.CONFIRM: c.DecisionItemOutcome.COMMITTED,
                c.ConfirmationAction.REVISE: c.DecisionItemOutcome.REVISION_REQUESTED,
                c.ConfirmationAction.REJECT: c.DecisionItemOutcome.REJECTED,
                c.ConfirmationAction.DEFER: c.DecisionItemOutcome.DEFERRED,
            }[action.action]
            status = {
                c.ConfirmationAction.CONFIRM: c.SemanticRecordStatus.CONFIRMED,
                c.ConfirmationAction.REVISE: c.SemanticRecordStatus.OPEN,
                c.ConfirmationAction.REJECT: c.SemanticRecordStatus.REJECTED,
                c.ConfirmationAction.DEFER: c.SemanticRecordStatus.DEFERRED,
            }[action.action]
            connection.execute(
                update(records)
                .where(
                    records.c.foundation_id == str(action.pending_decision_id),
                    records.c.record_version == action.expected_pending_decision_version,
                )
                .values(status=status.value)
            )
            if outcome is c.DecisionItemOutcome.COMMITTED:
                for link in decision_payload["problem_links"]:
                    if link["resolution_kind"] != c.ProblemResolutionKind.FULL.value:
                        continue
                    problem = self._semantic_record_for_ref(
                        connection,
                        command.case_id,
                        link["problem_ref"],
                    )
                    if problem["entity_kind"] != "PROBLEM":
                        raise FoundationProtocolError(
                            c.FoundationRejectionCode.UNKNOWN_REFERENCE
                        )
                    if problem["status"] == c.SemanticRecordStatus.OPEN.value:
                        connection.execute(
                            update(records)
                            .where(
                                records.c.foundation_id == problem["foundation_id"],
                                records.c.record_version == problem["record_version"],
                                records.c.status
                                == c.SemanticRecordStatus.OPEN.value,
                            )
                            .values(status=c.SemanticRecordStatus.RESOLVED.value)
                        )
            revision_request_id = None
            if outcome is c.DecisionItemOutcome.REVISION_REQUESTED:
                revision_request_id = self.new_id()
                revision_payload = {
                    "pending_decision_id": str(action.pending_decision_id),
                    "source_transcript_span": action.revision_span.model_dump(mode="json"),
                    "status": "OPEN",
                }
                connection.execute(
                    insert(records).values(
                        foundation_id=str(revision_request_id),
                        record_version=1,
                        case_id=str(command.case_id),
                        entity_kind="REVISION_REQUEST",
                        status=c.SemanticRecordStatus.OPEN.value,
                        content_hash=payload_hash(revision_payload),
                        payload_json=_json(revision_payload),
                        analyzer_run_id=str(command.selection.selection_event_id),
                        candidate_key=f"revision-request-{action.pending_decision_id}",
                        created_at=_instant(self.now()),
                    )
                )
            results.append(
                c.DecisionBatchItemReceipt(
                    review_item_id=action.review_item_id,
                    pending_decision_id=action.pending_decision_id,
                    outcome=outcome,
                    committed_decision_id=(
                        action.pending_decision_id if outcome is c.DecisionItemOutcome.COMMITTED else None
                    ),
                    revision_request_id=revision_request_id,
                )
            )
        connection.execute(update(views).where(views.c.view_id == str(command.decision_batch_view_id)).values(current=0))
        self._advance_revision(connection, command.case_id, prior_revision)
        return c.DecisionBatchResponseReceipt(
            receipt_type="DECISION_BATCH_RESPONSE",
            command=self._base_receipt(command, prior_revision),
            decision_batch_view_id=command.decision_batch_view_id,
            item_results=tuple(results),
            resulting_readiness=c.Readiness.FORMULATING,
            resulting_review_obligation=c.ReviewObligation.NONE,
        )

    def _admit_artifact(self, connection, command, prior_revision, artifact_type):
        active_context = self._active_context(
            connection, command.case_id, command.candidate.context_id
        )
        if active_context is None:
            raise FoundationProtocolError(c.FoundationRejectionCode.PROVIDER_REQUEST_BINDING_FAILED)
        protocol_case = connection.execute(
            select(WORKSHOP_PROTOCOL_TABLES["workshop_protocol_cases"]).where(
                WORKSHOP_PROTOCOL_TABLES["workshop_protocol_cases"].c.case_id == str(command.case_id)
            )
        ).mappings().one()
        if command.candidate.source_set_hash != protocol_case["source_set_hash"]:
            raise FoundationProtocolError(c.FoundationRejectionCode.SOURCE_BINDING_FAILED)
        schema_name = (
            "spec-package-payload.schema.json"
            if artifact_type == "SPEC_PACKAGE"
            else "technical-contract-payload.schema.json"
        )
        try:
            payload = json.loads(
                command.candidate.candidate_payload_json,
                object_pairs_hook=self._reject_duplicate_keys,
            )
        except ValueError as exc:
            raise FoundationProtocolError(c.FoundationRejectionCode.PAYLOAD_SCHEMA_FAILED) from exc
        errors = list(_payload_validator(schema_name).iter_errors(payload))
        if errors:
            raise FoundationProtocolError(c.FoundationRejectionCode.PAYLOAD_SCHEMA_FAILED)
        if artifact_type == "SPEC_PACKAGE":
            self._validate_confirmed_decision_projection(command, payload)
        if artifact_type == "TECHNICAL_CONTRACT":
            self._validate_confirmed_spec_lineage(connection, command, prior_revision)
        planned = {str(item.foundation_id): item.entity_kind for item in command.identity_plan.planned_identities}
        identifier_kinds = self._artifact_identity_kinds(payload)
        if not set(identifier_kinds).issubset(planned) or any(
            planned[identity] != kind for identity, kind in identifier_kinds.items()
        ):
            raise FoundationProtocolError(c.FoundationRejectionCode.IDENTITY_PLAN_FAILED)
        for item in command.identity_plan.planned_identities:
            for ref in item.source_entity_refs:
                self._semantic_record_for_ref(connection, command.case_id, ref)
        records = WORKSHOP_PROTOCOL_TABLES["workshop_artifact_records"]
        prior_artifact = connection.execute(
            select(records).where(
                records.c.case_id == str(command.case_id),
                records.c.artifact_key == command.target.artifact_key,
            ).order_by(records.c.artifact_version.desc()).limit(1)
        ).mappings().one_or_none()
        if prior_artifact is None:
            if command.target.next_artifact_version != 1:
                raise FoundationProtocolError(c.FoundationRejectionCode.IDENTITY_PLAN_FAILED)
        elif (
            prior_artifact["artifact_id"] != str(command.target.foundation_artifact_id)
            or command.target.next_artifact_version != prior_artifact["artifact_version"] + 1
        ):
            raise FoundationProtocolError(c.FoundationRejectionCode.IDENTITY_PLAN_FAILED)
        plan_table = WORKSHOP_PROTOCOL_TABLES["workshop_identity_plans"]
        try:
            connection.execute(
                insert(plan_table).values(
                    identity_plan_id=str(command.identity_plan.identity_plan_id),
                    identity_plan_version=command.identity_plan.identity_plan_version,
                    case_id=str(command.case_id),
                    artifact_id=str(command.target.foundation_artifact_id),
                    artifact_version=command.target.next_artifact_version,
                    semantic_state_hash=command.identity_plan.semantic_state_hash,
                    plan_json=command.identity_plan.model_dump_json(),
                    status="CONSUMED",
                    created_at=_instant(self.now()),
                )
            )
        except IntegrityError as exc:
            raise FoundationProtocolError(c.FoundationRejectionCode.IDENTITY_PLAN_FAILED) from exc
        digest = payload_hash(payload)
        context = c.AnalyzerContextBinding.model_validate_json(active_context["binding_json"])
        governance = draft_governance(
            artifact_type=artifact_type,
            payload=payload,
            target=command.target,
            quality_hash=context.analyzer_contract.semantic_quality_contract_hash,
            now=self.now(),
            new_id=self.new_id,
        )
        complete_artifact = artifact_envelope(
            artifact_type=artifact_type,
            command=command,
            payload=payload,
            governance=governance,
            payload_hash=digest,
            now=self.now(),
        )
        try:
            validate_exact(
                "spec-package.schema.json"
                if artifact_type == "SPEC_PACKAGE"
                else "technical-contract.schema.json",
                complete_artifact,
            )
        except ValueError as exc:
            raise FoundationProtocolError(c.FoundationRejectionCode.PAYLOAD_SCHEMA_FAILED) from exc
        connection.execute(
            insert(records).values(
                artifact_id=str(command.target.foundation_artifact_id),
                artifact_version=command.target.next_artifact_version,
                case_id=str(command.case_id),
                artifact_type=artifact_type,
                artifact_key=command.target.artifact_key,
                record_revision=1,
                payload_hash=digest,
                payload_json=_json(payload),
                governance_json=_json(governance),
                status="DRAFT",
                confirmed_from_json=(
                    _json(command.confirmed_spec.model_dump(mode="json"))
                    if artifact_type == "TECHNICAL_CONTRACT"
                    else None
                ),
                created_at=_instant(self.now()),
            )
        )
        self._advance_revision(connection, command.case_id, prior_revision)
        return c.ArtifactSynthesisAdmissionReceipt(
            receipt_type="ARTIFACT_SYNTHESIS_ADMISSION",
            command=self._base_receipt(command, prior_revision),
            analyzer_run_id=command.candidate.analyzer_run_id,
            artifact_type=artifact_type,
            artifact_id=command.target.foundation_artifact_id,
            artifact_key=command.target.artifact_key,
            artifact_version=command.target.next_artifact_version,
            record_revision=1,
            payload_hash=digest,
        )

    def _validate_confirmed_decision_projection(self, command, payload):
        """Reject drafts that omit or rewrite Foundation-confirmed decisions."""

        canonical = self.confirmed_decision_synthesis_bindings(command.case_id)
        supplied = command.confirmed_decision_bindings
        if supplied != canonical:
            raise FoundationProtocolError(
                c.FoundationRejectionCode.CONFIRMATION_BINDING_FAILED
            )

        decisions = payload["decisions"]
        decision_ids = [item["id"] for item in decisions]
        if len(decision_ids) != len(set(decision_ids)):
            raise FoundationProtocolError(c.FoundationRejectionCode.IDENTITY_PLAN_FAILED)
        confirmed = {
            item["id"]: item for item in decisions if item["status"] == "confirmed"
        }
        expected_ids = {str(item.decision_id) for item in canonical}
        if set(confirmed) != expected_ids:
            raise FoundationProtocolError(
                c.FoundationRejectionCode.CONFIRMATION_BINDING_FAILED
            )

        for binding in canonical:
            decision = confirmed[str(binding.decision_id)]
            exact_confirmation = {
                "confirmation_id": str(binding.confirmation_id),
                "confirmed_decision_id": str(binding.decision_id),
                "confirmed_decision_version": binding.decision_version,
                "decision_batch_view_id": str(binding.decision_batch_view_id),
                "decision_batch_view_hash": binding.decision_batch_view_hash,
                "review_item_id": str(binding.review_item_id),
                "confirmed_case_revision": binding.confirmed_case_revision,
                "actor_ref": str(binding.actor_ref),
                "authority_validation_id": str(binding.authority_validation_id),
                "transcript_event_id": str(binding.transcript_event_id),
                "confirmed_at": binding.confirmed_at.astimezone(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
            }
            if (
                decision["decision_version"] != binding.decision_version
                or decision["decision"] != binding.statement
                or decision["rationale"] != binding.rationale
                or decision["alternatives_considered"]
                != list(binding.alternatives_considered)
                or decision["evidence_refs"] != [str(item) for item in binding.evidence_ids]
                or decision["confirmation_binding"] != exact_confirmation
                or decision["authority"]["actor_ref"] != str(binding.actor_ref)
                or decision["authority"]["foundation_validation_id"]
                != str(binding.authority_validation_id)
            ):
                raise FoundationProtocolError(
                    c.FoundationRejectionCode.CONFIRMATION_BINDING_FAILED
                )

    def _validate_confirmed_spec_lineage(self, connection, command, prior_revision):
        binding = command.confirmed_spec
        confirmations = WORKSHOP_PROTOCOL_TABLES["workshop_artifact_confirmations"]
        confirmation = connection.execute(
            select(confirmations).where(
                confirmations.c.case_id == str(command.case_id),
                confirmations.c.confirmation_id == str(binding.confirmation_id),
                confirmations.c.artifact_id == str(binding.foundation_artifact_id),
                confirmations.c.artifact_version == binding.artifact_version,
                confirmations.c.record_revision == binding.record_revision,
                confirmations.c.payload_hash == binding.payload_hash,
                confirmations.c.confirmed_case_revision == binding.confirmed_case_revision,
                confirmations.c.revoked_at.is_(None),
                confirmations.c.superseded_by_confirmation_id.is_(None),
            )
        ).mappings().one_or_none()
        if confirmation is None or prior_revision < binding.confirmed_case_revision:
            raise FoundationProtocolError(c.FoundationRejectionCode.CONFIRMATION_BINDING_FAILED)
        records = WORKSHOP_PROTOCOL_TABLES["workshop_artifact_records"]
        record = connection.execute(
            select(records).where(
                records.c.case_id == str(command.case_id),
                records.c.artifact_id == str(binding.foundation_artifact_id),
                records.c.artifact_key == binding.artifact_key,
                records.c.artifact_version == binding.artifact_version,
                records.c.record_revision == binding.record_revision,
                records.c.payload_hash == binding.payload_hash,
                records.c.artifact_type == "SPEC_PACKAGE",
                records.c.status == "CONFIRMED",
            )
        ).mappings().one_or_none()
        if record is None or _json(json.loads(binding.canonical_payload_json)) != record["payload_json"]:
            raise FoundationProtocolError(c.FoundationRejectionCode.CONFIRMATION_BINDING_FAILED)

    @staticmethod
    def _reject_duplicate_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    @staticmethod
    def _artifact_identities(value: Any) -> set[str]:
        result = set()
        if isinstance(value, dict):
            for key, item in value.items():
                # Canonical payload entities use the field `id`. Other UUID
                # fields are references or provenance bindings and must resolve
                # to Foundation state, not consume new identity-plan slots.
                if key == "id" and isinstance(item, str):
                    try:
                        UUID(item)
                    except ValueError:
                        pass
                    else:
                        result.add(item)
                result.update(WorkshopFoundationService._artifact_identities(item))
        elif isinstance(value, list):
            for item in value:
                result.update(WorkshopFoundationService._artifact_identities(item))
        return result

    @staticmethod
    def _artifact_identity_kinds(payload: dict[str, Any]) -> dict[str, str]:
        """Return every payload-owned identity with its canonical entity kind.

        Plain ``id`` is identity-bearing only at the paths defined by the two
        normative payload schemas.  Reference UUIDs never consume plan slots,
        and a planned UUID cannot be moved to an entity of another kind.
        """

        path_kinds = {
            # Spec Package payload.
            ("package_items", "*", "id"): "PACKAGE_ITEM",
            ("evidence_catalog", "*", "id"): "EVIDENCE",
            ("semantic_evidence_findings", "*", "finding_id"): "SEMANTIC_EVIDENCE_FINDING",
            ("actors", "*", "id"): "ACTOR",
            ("glossary", "*", "id"): "GLOSSARY_TERM",
            ("outcomes", "*", "id"): "OUTCOME",
            ("scope", "in_scope", "*", "id"): "SCOPE_ITEM",
            ("scope", "non_goals", "*", "id"): "SCOPE_ITEM",
            ("scope", "boundaries", "*", "id"): "SCOPE_BOUNDARY",
            ("journeys", "*", "id"): "JOURNEY",
            ("behaviour_contract", "always", "*", "id"): "BEHAVIOUR_RULE",
            ("behaviour_contract", "ask_first", "*", "id"): "BEHAVIOUR_RULE",
            ("behaviour_contract", "never", "*", "id"): "BEHAVIOUR_RULE",
            ("requirements", "*", "id"): "REQUIREMENT",
            ("data_rules", "*", "id"): "DATA_RULE",
            ("experience_states", "*", "id"): "EXPERIENCE_STATE",
            ("scenarios", "*", "id"): "SCENARIO",
            ("quality_attributes", "*", "id"): "QUALITY_ATTRIBUTE",
            ("constraints", "*", "id"): "CONSTRAINT",
            ("dependencies", "*", "id"): "DEPENDENCY",
            ("risks", "*", "id"): "RISK",
            ("decisions", "*", "id"): "DECISION",
            ("open_items", "*", "id"): "OPEN_ITEM",
            ("acceptance_checks", "*", "id"): "ACCEPTANCE_CHECK",
            # Technical Contract payload.
            ("architecture_context", "nodes", "*", "id"): "ARCHITECTURE_NODE",
            ("components", "*", "id"): "COMPONENT",
            ("interfaces", "*", "id"): "INTERFACE",
            ("data_contracts", "*", "id"): "DATA_CONTRACT",
            ("workflows", "*", "id"): "WORKFLOW",
            ("failure_contracts", "*", "id"): "FAILURE_CONTRACT",
            ("quality_budgets", "*", "id"): "QUALITY_BUDGET",
            ("security_privacy_contract", "controls", "*", "id"): "SECURITY_CONTROL",
            ("observability_audit", "events", "*", "id"): "OBSERVABILITY_EVENT",
            ("observability_audit", "metrics", "*", "id"): "OBSERVABILITY_METRIC",
            ("observability_audit", "traces", "*", "id"): "OBSERVABILITY_TRACE",
            ("observability_audit", "alerts", "*", "id"): "OBSERVABILITY_ALERT",
            ("observability_audit", "audit_records", "*", "id"): "AUDIT_RECORD",
            ("substrate_dependencies", "*", "id"): "SUBSTRATE_DEPENDENCY",
            ("rollout_migration_recovery", "rollout_steps", "*", "id"): "ROLLOUT_STEP",
            ("build_units", "*", "id"): "BUILD_UNIT",
            ("verification_plan", "*", "id"): "VERIFICATION_ITEM",
            ("engineering_decisions", "*", "id"): "ENGINEERING_DECISION",
            ("review_obligations", "*", "id"): "REVIEW_OBLIGATION",
        }
        result: dict[str, str] = {}

        def walk(value: Any, path: tuple[str, ...]) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    item_path = (*path, key)
                    if key in {"id", "finding_id"} and isinstance(item, str):
                        try:
                            UUID(item)
                        except ValueError:
                            continue
                        kind = path_kinds.get(item_path)
                        if kind is None or item in result:
                            raise FoundationProtocolError(
                                c.FoundationRejectionCode.IDENTITY_PLAN_FAILED
                            )
                        result[item] = kind
                    else:
                        walk(item, item_path)
            elif isinstance(value, list):
                for item in value:
                    walk(item, (*path, "*"))

        walk(payload, ())
        return result

    def _materialize_artifact_review(self, connection, command, prior_revision):
        records = WORKSHOP_PROTOCOL_TABLES["workshop_artifact_records"]
        record = connection.execute(
            select(records).where(
                records.c.case_id == str(command.case_id),
                records.c.artifact_type == command.subject.artifact_type,
                records.c.artifact_id == str(command.subject.artifact_id),
                records.c.artifact_key == command.subject.artifact_key,
                records.c.artifact_version == command.subject.artifact_version,
                records.c.record_revision == command.subject.record_revision,
                records.c.payload_hash == command.subject.payload_hash,
            )
        ).mappings().one_or_none()
        if record is None:
            raise FoundationProtocolError(c.FoundationRejectionCode.STALE_ENTITY)
        audit = self.quality_audit_for_record(connection, dict(record), require_pass=False)
        view_id = self.new_id()
        confirmation_id = self.new_id()
        generated_at = self.now()
        active_context = connection.execute(
            select(WORKSHOP_PROTOCOL_TABLES["workshop_analyzer_contexts"])
            .where(
                WORKSHOP_PROTOCOL_TABLES["workshop_analyzer_contexts"].c.case_id
                == str(command.case_id),
                WORKSHOP_PROTOCOL_TABLES["workshop_analyzer_contexts"].c.status
                == c.ContextStatus.ACTIVE.value,
            )
        ).mappings().one()
        context = c.AnalyzerContextBinding.model_validate_json(active_context["binding_json"])
        next_record = dict(record)
        next_record["record_revision"] = record["record_revision"] + 1
        governance = json.loads(record["governance_json"])
        governance = self.governance_with_projection_pass(
            record=next_record,
            governance=governance,
            view_id=view_id,
            view_hash="sha256:" + "0" * 64,
            view_mode=command.view_mode,
            generated_at=generated_at,
        )
        try:
            provisional = build_review_view(
                artifact_type=command.subject.artifact_type,
                record=next_record,
                payload=json.loads(record["payload_json"]),
                governance=governance,
                confirmed_from=(
                    None
                    if record["confirmed_from_json"] is None
                    else json.loads(record["confirmed_from_json"])
                ),
                view_id=view_id,
                view_mode=command.view_mode,
                case_revision=prior_revision + 1,
                context=context,
                snapshot=self.semantic_snapshot(command.case_id),
                now=generated_at,
            )
            provisional_hash = provisional["projection_integrity"]["view_hash"]
            governance = self.governance_with_projection_pass(
                record=next_record,
                governance=governance,
                view_id=view_id,
                view_hash=provisional_hash,
                view_mode=command.view_mode,
                generated_at=generated_at,
            )
            view = build_review_view(
                artifact_type=command.subject.artifact_type,
                record=next_record,
                payload=json.loads(record["payload_json"]),
                governance=governance,
                confirmed_from=(
                    None
                    if record["confirmed_from_json"] is None
                    else json.loads(record["confirmed_from_json"])
                ),
                view_id=view_id,
                view_mode=command.view_mode,
                case_revision=prior_revision + 1,
                context=context,
                snapshot=self.semantic_snapshot(command.case_id),
                now=generated_at,
            )
            if view["projection_integrity"]["view_hash"] != provisional_hash:
                raise ValueError("review binding changed the materialized projection")
        except ValueError as exc:
            raise FoundationProtocolError(c.FoundationRejectionCode.PAYLOAD_SCHEMA_FAILED) from exc
        view_hash = view["projection_integrity"]["view_hash"]
        connection.execute(
            update(records)
            .where(
                records.c.artifact_id == record["artifact_id"],
                records.c.artifact_version == record["artifact_version"],
                records.c.record_revision == record["record_revision"],
            )
            .values(
                record_revision=next_record["record_revision"],
                governance_json=_json(governance),
            )
        )
        table = WORKSHOP_PROTOCOL_TABLES["workshop_artifact_reviews"]
        connection.execute(
            update(table)
            .where(table.c.case_id == str(command.case_id))
            .values(current=0)
        )
        connection.execute(
            insert(table).values(
                view_id=str(view_id),
                confirmation_id=str(confirmation_id),
                case_id=str(command.case_id),
                artifact_id=str(command.subject.artifact_id),
                artifact_version=command.subject.artifact_version,
                record_revision=next_record["record_revision"],
                payload_hash=command.subject.payload_hash,
                view_hash=view_hash,
                view_mode=command.view_mode,
                view_json=_json(view),
                current=1,
                confirmed=0,
                generated_at=_instant(generated_at),
            )
        )
        self.record_projection_quality_phase(
            connection,
            audit=audit,
            record=next_record,
            view_id=view_id,
            view_hash=view_hash,
        )
        self._advance_revision(connection, command.case_id, prior_revision)
        return c.ArtifactReviewReceipt(
            receipt_type="ARTIFACT_REVIEW",
            command=self._base_receipt(command, prior_revision),
            confirmation_id=confirmation_id,
            view_id=view_id,
            view_hash=view_hash,
        )

    def _confirm_artifact(self, connection, command, prior_revision):
        views = WORKSHOP_PROTOCOL_TABLES["workshop_artifact_reviews"]
        row = connection.execute(
            select(views).where(
                views.c.view_id == str(command.binding.view_id),
                views.c.confirmation_id == str(command.binding.confirmation_id),
                views.c.case_id == str(command.case_id),
                views.c.artifact_id == str(command.binding.artifact_id),
                views.c.artifact_version == command.binding.artifact_version,
                views.c.record_revision == command.binding.record_revision,
                views.c.payload_hash == command.binding.payload_hash,
                views.c.view_hash == command.binding.view_hash,
                views.c.current == 1,
                views.c.confirmed == 0,
            )
        ).mappings().one_or_none()
        if row is None:
            raise FoundationProtocolError(c.FoundationRejectionCode.CONFIRMATION_BINDING_FAILED)
        self._require_participant(connection, command.case_id, command.acting_actor_id)
        if self._participant_transcript(
            connection,
            command.case_id,
            command.confirmation_transcript_event_id,
            command.acting_actor_id,
        ) is None:
            raise FoundationProtocolError(c.FoundationRejectionCode.TRANSCRIPT_BINDING_FAILED)
        if command.approved_exception_ids:
            raise FoundationProtocolError(c.FoundationRejectionCode.UNKNOWN_REFERENCE)
        records = WORKSHOP_PROTOCOL_TABLES["workshop_artifact_records"]
        artifact = connection.execute(
            select(records).where(
                records.c.case_id == str(command.case_id),
                records.c.artifact_id == str(command.binding.artifact_id),
                records.c.artifact_version == command.binding.artifact_version,
                records.c.record_revision == command.binding.record_revision,
            )
        ).mappings().one()
        audit, governance = self.require_quality_confirmation_gate(
            connection, dict(artifact), dict(row)
        )
        required_domain = "BUSINESS" if artifact["artifact_type"] == "SPEC_PACKAGE" else "TECHNICAL"
        if not self._has_domain_authority(
            connection, command.case_id, command.acting_actor_id, required_domain
        ):
            raise FoundationProtocolError(c.FoundationRejectionCode.AUTHORITY_FAILED)
        governance = self.finalize_confirmation_quality(
            connection,
            audit=audit,
            governance=governance,
            artifact=dict(artifact),
            view=dict(row),
            command=command,
            confirmed_case_revision=prior_revision + 1,
        )
        try:
            validate_governance(artifact["artifact_type"], governance)
        except ValueError as exc:
            raise FoundationProtocolError(c.FoundationRejectionCode.PAYLOAD_SCHEMA_FAILED) from exc
        confirmations = WORKSHOP_PROTOCOL_TABLES["workshop_artifact_confirmations"]
        connection.execute(
            insert(confirmations).values(
                confirmation_id=str(command.binding.confirmation_id),
                case_id=str(command.case_id),
                artifact_id=str(command.binding.artifact_id),
                artifact_version=command.binding.artifact_version,
                record_revision=command.binding.record_revision,
                payload_hash=command.binding.payload_hash,
                view_id=str(command.binding.view_id),
                view_hash=command.binding.view_hash,
                actor_id=str(command.acting_actor_id),
                confirmation_transcript_event_id=str(command.confirmation_transcript_event_id),
                authority_snapshot_json=command.actor_authentication.model_dump_json(),
                confirmed_case_revision=prior_revision + 1,
                confirmed_at=_instant(self.now()),
                revoked_at=None,
                superseded_by_confirmation_id=None,
            )
        )
        connection.execute(update(views).where(views.c.view_id == str(command.binding.view_id)).values(confirmed=1))
        connection.execute(
            update(records)
            .where(
                records.c.artifact_id == str(command.binding.artifact_id),
                records.c.artifact_version == command.binding.artifact_version,
                records.c.record_revision == command.binding.record_revision,
            )
            .values(status="CONFIRMED")
            .values(governance_json=_json(governance))
        )
        self._advance_revision(connection, command.case_id, prior_revision)
        return c.ArtifactConfirmationReceipt(
            receipt_type="ARTIFACT_CONFIRMATION",
            command=self._base_receipt(command, prior_revision),
            binding=command.binding,
        )

    def current_decision_view(self, case_id: UUID) -> c.DecisionBatchReviewView | None:
        table = WORKSHOP_PROTOCOL_TABLES["workshop_decision_views"]
        with self.engine.connect() as connection:
            row = connection.execute(
                select(table.c.view_json).where(
                    table.c.case_id == str(case_id),
                    table.c.current == 1,
                )
            ).scalar_one_or_none()
        return None if row is None else c.DecisionBatchReviewView.model_validate_json(row)

    def decision_view_by_binding(
        self,
        case_id: UUID,
        view_id: UUID,
        view_hash: str,
    ) -> c.DecisionBatchReviewView | None:
        """Return an exact historical view so Foundation can decide replay vs stale."""

        table = WORKSHOP_PROTOCOL_TABLES["workshop_decision_views"]
        with self.engine.connect() as connection:
            row = connection.execute(
                select(table.c.view_json).where(
                    table.c.case_id == str(case_id),
                    table.c.view_id == str(view_id),
                    table.c.view_hash == view_hash,
                )
            ).scalar_one_or_none()
        return None if row is None else c.DecisionBatchReviewView.model_validate_json(row)

    def decision_response_replay_context(
        self,
        case_id: UUID,
        idempotency_key: str,
    ) -> tuple[c.DecisionBatchReviewView, int] | None:
        """Recover the view and revision bound to a ledgered decision response."""

        ledger = WORKSHOP_PROTOCOL_TABLES["workshop_command_ledger"]
        views = WORKSHOP_PROTOCOL_TABLES["workshop_decision_views"]
        with self.engine.connect() as connection:
            recorded = connection.execute(
                select(ledger.c.receipt_json).where(
                    ledger.c.case_id == str(case_id),
                    ledger.c.idempotency_key == idempotency_key,
                    ledger.c.command_type == "APPLY_DECISION_BATCH_RESPONSE",
                )
            ).scalar_one_or_none()
            if recorded is None:
                return None
            receipt = c.DecisionBatchResponseReceipt.model_validate_json(recorded)
            row = connection.execute(
                select(views.c.view_json).where(
                    views.c.case_id == str(case_id),
                    views.c.view_id == str(receipt.decision_batch_view_id),
                )
            ).scalar_one_or_none()
        if row is None:
            return None
        return (
            c.DecisionBatchReviewView.model_validate_json(row),
            receipt.command.prior_case_revision,
        )

    def current_artifact_review(self, case_id: UUID) -> dict[str, Any] | None:
        table = WORKSHOP_PROTOCOL_TABLES["workshop_artifact_reviews"]
        with self.engine.connect() as connection:
            row = connection.execute(
                select(table).where(table.c.case_id == str(case_id), table.c.current == 1)
                .order_by(table.c.generated_at.desc())
                .limit(1)
            ).mappings().one_or_none()
        if row is None:
            return None
        return {
            "confirmation_id": UUID(row["confirmation_id"]),
            "view_id": UUID(row["view_id"]),
            "view_hash": row["view_hash"],
            "view": json.loads(row["view_json"]),
            "confirmed": bool(row["confirmed"]),
        }

    def confirmed_artifact(self, case_id: UUID, artifact_type: str) -> dict[str, Any] | None:
        table = WORKSHOP_PROTOCOL_TABLES["workshop_artifact_records"]
        with self.engine.connect() as connection:
            row = connection.execute(
                select(table).where(
                    table.c.case_id == str(case_id),
                    table.c.artifact_type == artifact_type,
                    table.c.status == "CONFIRMED",
                )
                .order_by(table.c.artifact_version.desc())
                .limit(1)
            ).mappings().one_or_none()
        if row is None:
            return None
        return {
            "artifact_id": UUID(row["artifact_id"]),
            "artifact_key": row["artifact_key"],
            "artifact_version": row["artifact_version"],
            "record_revision": row["record_revision"],
            "payload_hash": row["payload_hash"],
            "payload": json.loads(row["payload_json"]),
        }
