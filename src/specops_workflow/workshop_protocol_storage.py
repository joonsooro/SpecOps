"""Relational tables owned by the Foundation for Workshop Protocol 1.0.0."""

from __future__ import annotations

from typing import Any

from sqlalchemy import (
    Column,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
)


V4_TABLE_NAMES = (
    "workshop_protocol_cases",
    "workshop_command_ledger",
    "workshop_analyzer_contexts",
    "workshop_final_transcripts",
    "workshop_candidate_mappings",
    "workshop_semantic_records",
    "workshop_guidance",
    "workshop_review_narrations",
    "workshop_decision_views",
    "workshop_identity_plans",
    "workshop_artifact_records",
    "workshop_artifact_reviews",
    "workshop_artifact_confirmations",
    "workshop_protocol_events",
)

V0_RUNTIME_TABLE_NAMES = (
    "workshop_preparations",
    "workshop_preparation_resources",
    "workshop_provider_responses",
    "workshop_runway_items",
    "workshop_analyzer_jobs",
)


def define_workshop_protocol_tables(metadata: MetaData) -> dict[str, Table]:
    """Register the V4 tables on the one canonical Foundation metadata graph."""

    all_names = (*V4_TABLE_NAMES, *V0_RUNTIME_TABLE_NAMES)
    if all(name in metadata.tables for name in all_names):
        return {name: metadata.tables[name] for name in V4_TABLE_NAMES}

    tables: dict[str, Table] = {}

    def table(name: str, *items: Any) -> Table:
        value = Table(name, metadata, *items)
        tables[name] = value
        return value

    table(
        "workshop_protocol_cases",
        Column("case_id", Text, ForeignKey("cases.id", ondelete="RESTRICT"), primary_key=True),
        Column("session_id", Text, nullable=False, unique=True),
        Column("source_set_hash", Text, nullable=False),
        Column("active_context_id", Text),
        Column("readiness", Text, nullable=False),
        Column("review_obligation", Text, nullable=False),
        Column("created_at", Text, nullable=False),
        Column("updated_at", Text, nullable=False),
    )
    table(
        "workshop_command_ledger",
        Column("case_id", Text, ForeignKey("cases.id", ondelete="RESTRICT"), primary_key=True),
        Column("idempotency_key", Text, primary_key=True),
        Column("command_id", Text, nullable=False, unique=True),
        Column("command_type", Text, nullable=False),
        Column("command_fingerprint", Text, nullable=False),
        Column("receipt_json", Text, nullable=False),
        Column("recorded_at", Text, nullable=False),
    )
    table(
        "workshop_analyzer_contexts",
        Column("context_id", Text, primary_key=True),
        Column("case_id", Text, ForeignKey("cases.id", ondelete="RESTRICT"), nullable=False),
        Column("session_id", Text, nullable=False),
        Column("source_set_hash", Text, nullable=False),
        Column("provider_conversation_id", Text, nullable=False),
        Column("status", Text, nullable=False),
        Column("binding_json", Text, nullable=False),
        Column("created_at", Text, nullable=False),
        Column("invalidated_at", Text),
        UniqueConstraint("case_id", "context_id"),
    )
    table(
        "workshop_final_transcripts",
        Column("event_id", Text, primary_key=True),
        Column("case_id", Text, ForeignKey("cases.id", ondelete="RESTRICT"), nullable=False),
        Column("session_id", Text, nullable=False),
        Column("sequence_number", Integer, nullable=False),
        Column("transcript_hash", Text, nullable=False),
        Column("event_json", Text, nullable=False),
        Column("recorded_at", Text, nullable=False),
        UniqueConstraint("case_id", "session_id", "sequence_number"),
    )
    table(
        "workshop_candidate_mappings",
        Column("analyzer_run_id", Text, primary_key=True),
        Column("candidate_key", Text, primary_key=True),
        Column("case_id", Text, ForeignKey("cases.id", ondelete="RESTRICT"), nullable=False),
        Column("entity_kind", Text, nullable=False),
        Column("foundation_id", Text, nullable=False),
        Column("record_version", Integer, nullable=False),
        UniqueConstraint("case_id", "foundation_id", "record_version"),
    )
    table(
        "workshop_semantic_records",
        Column("foundation_id", Text, primary_key=True),
        Column("record_version", Integer, primary_key=True),
        Column("case_id", Text, ForeignKey("cases.id", ondelete="RESTRICT"), nullable=False),
        Column("entity_kind", Text, nullable=False),
        Column("status", Text, nullable=False),
        Column("content_hash", Text, nullable=False),
        Column("payload_json", Text, nullable=False),
        Column("analyzer_run_id", Text, nullable=False),
        Column("candidate_key", Text, nullable=False),
        Column("created_at", Text, nullable=False),
    )
    table(
        "workshop_guidance",
        Column("guidance_id", Text, primary_key=True),
        Column("guidance_version", Integer, primary_key=True),
        Column("case_id", Text, ForeignKey("cases.id", ondelete="RESTRICT"), nullable=False),
        Column("payload_json", Text, nullable=False),
        Column("valid", Integer, nullable=False),
        Column("admitted_at", Text, nullable=False),
    )
    table(
        "workshop_review_narrations",
        Column("narration_id", Text, primary_key=True),
        Column("narration_version", Integer, primary_key=True),
        Column("case_id", Text, ForeignKey("cases.id", ondelete="RESTRICT"), nullable=False),
        Column("decision_batch_view_id", Text, nullable=False),
        Column("payload_json", Text, nullable=False),
        Column("valid", Integer, nullable=False),
        Column("admitted_at", Text, nullable=False),
    )
    table(
        "workshop_decision_views",
        Column("view_id", Text, primary_key=True),
        Column("case_id", Text, ForeignKey("cases.id", ondelete="RESTRICT"), nullable=False),
        Column("view_hash", Text, nullable=False),
        Column("based_on_case_revision", Integer, nullable=False),
        Column("view_json", Text, nullable=False),
        Column("current", Integer, nullable=False),
        Column("generated_at", Text, nullable=False),
    )
    table(
        "workshop_identity_plans",
        Column("identity_plan_id", Text, primary_key=True),
        Column("identity_plan_version", Integer, primary_key=True),
        Column("case_id", Text, ForeignKey("cases.id", ondelete="RESTRICT"), nullable=False),
        Column("artifact_id", Text, nullable=False),
        Column("artifact_version", Integer, nullable=False),
        Column("semantic_state_hash", Text, nullable=False),
        Column("plan_json", Text, nullable=False),
        Column("status", Text, nullable=False),
        Column("created_at", Text, nullable=False),
    )
    table(
        "workshop_artifact_records",
        Column("artifact_id", Text, primary_key=True),
        Column("artifact_version", Integer, primary_key=True),
        Column("case_id", Text, ForeignKey("cases.id", ondelete="RESTRICT"), nullable=False),
        Column("artifact_type", Text, nullable=False),
        Column("artifact_key", Text, nullable=False),
        Column("record_revision", Integer, nullable=False),
        Column("payload_hash", Text, nullable=False),
        Column("payload_json", Text, nullable=False),
        Column("governance_json", Text, nullable=False),
        Column("status", Text, nullable=False),
        Column("confirmed_from_json", Text),
        Column("created_at", Text, nullable=False),
        UniqueConstraint("case_id", "artifact_key", "artifact_version"),
        UniqueConstraint("artifact_id", "artifact_version", "record_revision"),
    )
    table(
        "workshop_artifact_reviews",
        Column("view_id", Text, primary_key=True),
        Column("confirmation_id", Text, nullable=False, unique=True),
        Column("case_id", Text, ForeignKey("cases.id", ondelete="RESTRICT"), nullable=False),
        Column("artifact_id", Text, nullable=False),
        Column("artifact_version", Integer, nullable=False),
        Column("record_revision", Integer, nullable=False),
        Column("payload_hash", Text, nullable=False),
        Column("view_hash", Text, nullable=False),
        Column("view_mode", Text, nullable=False),
        Column("view_json", Text, nullable=False),
        Column("current", Integer, nullable=False),
        Column("confirmed", Integer, nullable=False),
        Column("generated_at", Text, nullable=False),
        ForeignKeyConstraint(
            ("artifact_id", "artifact_version", "record_revision"),
            (
                "workshop_artifact_records.artifact_id",
                "workshop_artifact_records.artifact_version",
                "workshop_artifact_records.record_revision",
            ),
            ondelete="RESTRICT",
        ),
    )
    table(
        "workshop_artifact_confirmations",
        Column("confirmation_id", Text, primary_key=True),
        Column("case_id", Text, ForeignKey("cases.id", ondelete="RESTRICT"), nullable=False),
        Column("artifact_id", Text, nullable=False),
        Column("artifact_version", Integer, nullable=False),
        Column("record_revision", Integer, nullable=False),
        Column("payload_hash", Text, nullable=False),
        Column("view_id", Text, nullable=False, unique=True),
        Column("view_hash", Text, nullable=False),
        Column("actor_id", Text, nullable=False),
        Column("confirmation_transcript_event_id", Text, nullable=False),
        Column("authority_snapshot_json", Text, nullable=False),
        Column("confirmed_case_revision", Integer, nullable=False),
        Column("confirmed_at", Text, nullable=False),
        Column("revoked_at", Text),
        Column("superseded_by_confirmation_id", Text),
        ForeignKeyConstraint(
            ("artifact_id", "artifact_version", "record_revision"),
            (
                "workshop_artifact_records.artifact_id",
                "workshop_artifact_records.artifact_version",
                "workshop_artifact_records.record_revision",
            ),
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ("view_id",),
            ("workshop_artifact_reviews.view_id",),
            ondelete="RESTRICT",
        ),
    )
    table(
        "workshop_protocol_events",
        Column("event_id", Text, primary_key=True),
        Column("case_id", Text, ForeignKey("cases.id", ondelete="RESTRICT"), nullable=False),
        Column("event_sequence", Integer, nullable=False),
        Column("event_type", Text, nullable=False),
        Column("event_json", Text, nullable=False),
        Column("occurred_at", Text, nullable=False),
        UniqueConstraint("case_id", "event_sequence"),
    )
    table(
        "workshop_preparations",
        Column("case_id", Text, ForeignKey("cases.id", ondelete="RESTRICT"), primary_key=True),
        Column("preparation_id", Text, nullable=False, unique=True),
        Column("phase", Text, nullable=False),
        Column("started_at", Text, nullable=False),
        Column("updated_at", Text, nullable=False),
        Column("ready_at", Text),
        Column("failure_code", Text),
        Column("cleanup_state", Text, nullable=False),
        Column("cleanup_reason", Text),
        Column("last_client_disconnected_at", Text),
        Column("restart_grace_until", Text),
        Column("workshop_complete_at", Text),
        Column("cleanup_available_at", Text),
        Column("cleanup_last_error_code", Text),
    )
    table(
        "workshop_preparation_resources",
        Column("case_id", Text, ForeignKey("cases.id", ondelete="RESTRICT"), primary_key=True),
        Column("source_set_hash", Text, nullable=False),
        Column("pm_file_id", Text),
        Column("technical_file_id", Text),
        Column("provider_conversation_id", Text),
        Column("bootstrap_request_id", Text, nullable=False),
        Column("bootstrap_response_id", Text),
        Column("bootstrap_candidate_json", Text),
        Column("context_json", Text),
        Column("updated_at", Text, nullable=False),
    )
    table(
        "workshop_provider_responses",
        Column(
            "case_id",
            Text,
            ForeignKey("cases.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        Column("client_request_id", Text, primary_key=True),
        Column("operation", Text, nullable=False),
        Column("provider_response_id", Text, unique=True),
        Column("recorded_at", Text, nullable=False),
        Column("cleared_at", Text),
    )
    table(
        "workshop_runway_items",
        Column("case_id", Text, ForeignKey("cases.id", ondelete="RESTRICT"), primary_key=True),
        Column("guidance_id", Text, primary_key=True),
        Column("question_id", Text, primary_key=True),
        Column("question_version", Integer, nullable=False),
        Column("position", Integer, nullable=False),
        Column("exact_text", Text, nullable=False),
        Column("reason", Text, nullable=False),
        Column("status", Text, nullable=False),
        Column("admitted_at", Text, nullable=False),
        Column("consumed_at", Text),
        UniqueConstraint("case_id", "guidance_id", "position"),
    )
    table(
        "workshop_analyzer_jobs",
        Column("job_id", Text, primary_key=True),
        Column("case_id", Text, ForeignKey("cases.id", ondelete="RESTRICT"), nullable=False),
        Column("session_id", Text, nullable=False),
        Column("operation", Text, nullable=False),
        Column("subject_id", Text, nullable=False),
        Column("dedupe_key", Text, nullable=False, unique=True),
        Column("priority", Integer, nullable=False),
        Column("state", Text, nullable=False),
        Column("provider_request_id", Text, nullable=False),
        Column("request_json", Text),
        Column("candidate_json", Text),
        Column("admission_receipt_json", Text),
        Column("attempt_count", Integer, nullable=False),
        Column("lease_owner", Text),
        Column("lease_expires_at", Text),
        Column("available_at", Text, nullable=False),
        Column("last_error_code", Text),
        Column("created_at", Text, nullable=False),
        Column("updated_at", Text, nullable=False),
        UniqueConstraint("case_id", "operation", "subject_id"),
    )

    Index(
        "ix_workshop_semantic_records_current",
        tables["workshop_semantic_records"].c.case_id,
        tables["workshop_semantic_records"].c.foundation_id,
        tables["workshop_semantic_records"].c.record_version,
    )
    Index(
        "ix_workshop_artifact_records_current",
        tables["workshop_artifact_records"].c.case_id,
        tables["workshop_artifact_records"].c.artifact_type,
        tables["workshop_artifact_records"].c.artifact_version,
    )
    Index(
        "ix_workshop_analyzer_jobs_eligible",
        tables["workshop_analyzer_jobs"].c.state,
        tables["workshop_analyzer_jobs"].c.priority,
        tables["workshop_analyzer_jobs"].c.available_at,
    )
    return {name: tables[name] for name in V4_TABLE_NAMES}
