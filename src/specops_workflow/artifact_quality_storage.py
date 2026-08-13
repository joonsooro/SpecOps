"""Foundation tables for Artifact Quality Audit Protocol 1.0.0."""

from __future__ import annotations

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


ARTIFACT_QUALITY_TABLE_NAMES = (
    "workshop_artifact_quality_audits",
    "workshop_artifact_quality_findings",
    "workshop_artifact_quality_gate_phases",
)


def define_artifact_quality_tables(metadata: MetaData) -> dict[str, Table]:
    if ARTIFACT_QUALITY_TABLE_NAMES[0] in metadata.tables:
        return {name: metadata.tables[name] for name in ARTIFACT_QUALITY_TABLE_NAMES}

    audits = Table(
        "workshop_artifact_quality_audits",
        metadata,
        Column("audit_id", Text, primary_key=True),
        Column("evaluator_run_id", Text, nullable=False, unique=True),
        Column("case_id", Text, ForeignKey("cases.id", ondelete="RESTRICT"), nullable=False),
        Column("artifact_id", Text, nullable=False),
        Column("artifact_version", Integer, nullable=False),
        Column("audited_record_revision", Integer, nullable=False),
        Column("resulting_record_revision", Integer),
        Column("payload_hash", Text, nullable=False),
        Column("request_hash", Text, nullable=False, unique=True),
        Column("audit_scope_manifest_hash", Text, nullable=False),
        Column("quality_contract_hash", Text, nullable=False),
        Column("source_set_hash", Text, nullable=False),
        Column("transcript_manifest_hash", Text, nullable=False),
        Column("semantic_state_hash", Text, nullable=False),
        Column("state", Text, nullable=False),
        Column("outcome", Text, nullable=False),
        Column("provider", Text),
        Column("model", Text),
        Column("reasoning_effort", Text),
        Column("provider_conversation_id", Text),
        Column("provider_response_id", Text),
        Column("client_request_id", Text),
        Column("provider_context_json", Text),
        Column("bundle_json", Text, nullable=False),
        Column("execution_json", Text),
        Column("candidate_json", Text),
        Column("combined_results_json", Text),
        Column("receipt_json", Text),
        Column("created_at", Text, nullable=False),
        Column("updated_at", Text, nullable=False),
        ForeignKeyConstraint(
            ("artifact_id", "artifact_version"),
            (
                "workshop_artifact_records.artifact_id",
                "workshop_artifact_records.artifact_version",
            ),
            ondelete="RESTRICT",
        ),
        UniqueConstraint("artifact_id", "artifact_version", "payload_hash"),
    )
    findings = Table(
        "workshop_artifact_quality_findings",
        metadata,
        Column("finding_id", Text, primary_key=True),
        Column("finding_version", Integer, primary_key=True),
        Column(
            "audit_id",
            Text,
            ForeignKey("workshop_artifact_quality_audits.audit_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        Column("rule_id", Text, nullable=False),
        Column("candidate_key", Text, nullable=False),
        Column("finding_json", Text, nullable=False),
        Column("created_at", Text, nullable=False),
        UniqueConstraint("audit_id", "rule_id", "candidate_key"),
    )
    phases = Table(
        "workshop_artifact_quality_gate_phases",
        metadata,
        Column("phase_id", Text, primary_key=True),
        Column(
            "audit_id",
            Text,
            ForeignKey("workshop_artifact_quality_audits.audit_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        Column("case_id", Text, ForeignKey("cases.id", ondelete="RESTRICT"), nullable=False),
        Column("phase", Text, nullable=False),
        Column("rule_id", Text, nullable=False),
        Column("artifact_id", Text, nullable=False),
        Column("artifact_version", Integer, nullable=False),
        Column("record_revision", Integer, nullable=False),
        Column("payload_hash", Text, nullable=False),
        Column("view_id", Text),
        Column("view_hash", Text),
        Column("result", Text, nullable=False),
        Column("evidence_json", Text, nullable=False),
        Column("created_at", Text, nullable=False),
        UniqueConstraint("audit_id", "phase", "view_id"),
    )
    Index(
        "ix_workshop_artifact_quality_subject",
        audits.c.case_id,
        audits.c.artifact_id,
        audits.c.artifact_version,
        audits.c.state,
    )
    return {audits.name: audits, findings.name: findings, phases.name: phases}
