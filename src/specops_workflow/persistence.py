from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from alembic import command
from alembic.config import Config
from sqlalchemy import (
    CHAR, CheckConstraint, Column, ForeignKey, ForeignKeyConstraint, Index, Integer, MetaData, Table, Text,
    UniqueConstraint, create_engine, event, insert, select, update,
)
from sqlalchemy.engine import Engine
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.types import TypeDecorator
from pydantic import TypeAdapter

from .artifacts import ArtifactRoot, ArtifactVersion
from .canonical import SchemaRegistry, canonical_json, default_registry, sha256
from .enums import *  # noqa: F403
from .models import *  # noqa: F403
from .state import AmbiguityState, ApprovalState, BindingState, CaseState, DelegationState, OperationState


class UTCText(TypeDecorator):
    impl = Text
    cache_ok = True
    def process_bind_param(self, value: datetime | None, dialect):
        if value is None: return None
        if value.tzinfo is None: raise ValueError("UTCText rejects naive datetime")
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    def process_result_value(self, value: str | None, dialect):
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc) if value else None


UUIDText = CHAR(36); HashText = CHAR(64); JSONText = Text; EnumText = Text
metadata = MetaData()


def owned(name: str, *columns: Column, constraints: tuple[Any, ...] = ()) -> Table:
    return Table(name, metadata, *columns, *constraints)


cases = owned("cases", Column("id", UUIDText, primary_key=True), Column("revision", Integer, nullable=False), Column("pm_actor_id", UUIDText, nullable=False), Column("dev_lead_actor_id", UUIDText, nullable=False), Column("created_at", UTCText(), nullable=False), CheckConstraint("pm_actor_id <> dev_lead_actor_id"), UniqueConstraint("id", "revision"))
case_participants = owned("case_participants", Column("case_id", UUIDText, ForeignKey("cases.id", ondelete="RESTRICT"), primary_key=True), Column("actor_id", UUIDText, primary_key=True), Column("created_at", UTCText(), nullable=False))
delegations = owned("delegations", Column("id", UUIDText, primary_key=True), Column("case_id", UUIDText, ForeignKey("cases.id", ondelete="RESTRICT"), nullable=False), Column("delegator_id", UUIDText, nullable=False), Column("delegate_id", UUIDText, nullable=False), Column("domain", EnumText, nullable=False), Column("command_names", JSONText, nullable=False), Column("artifact_kind", EnumText), Column("artifact_id", UUIDText), Column("valid_from", UTCText(), nullable=False), Column("valid_until", UTCText(), nullable=False), Column("later_review_required", Integer, nullable=False), Column("revoked_at", UTCText()), Column("created_at", UTCText(), nullable=False), UniqueConstraint("id", "case_id"), CheckConstraint("valid_from <= valid_until"))
source_artifacts = owned("source_artifacts", Column("artifact_id", UUIDText, primary_key=True), Column("version", Integer, primary_key=True), Column("case_id", UUIDText, ForeignKey("cases.id", ondelete="RESTRICT"), nullable=False), Column("type", EnumText, nullable=False), Column("media_type", Text, nullable=False), Column("canonical_locator", Text, nullable=False), Column("content_hash", HashText, nullable=False), Column("registered_at", UTCText(), nullable=False), UniqueConstraint("artifact_id", "version", "case_id"))
ambiguity_findings = owned("ambiguity_findings", Column("id", UUIDText, primary_key=True), Column("case_id", UUIDText, ForeignKey("cases.id", ondelete="RESTRICT"), nullable=False), Column("category", EnumText, nullable=False), Column("domain", EnumText, nullable=False), Column("severity", EnumText, nullable=False), Column("evidence_refs", JSONText, nullable=False), Column("clarification_question", Text, nullable=False), Column("status", EnumText, nullable=False), Column("resolutions", JSONText, nullable=False), Column("item_binding", JSONText), Column("created_at", UTCText(), nullable=False), Column("resolved_at", UTCText()), UniqueConstraint("id", "case_id"))
spec_packages = owned("spec_packages", Column("id", UUIDText, primary_key=True), Column("case_id", UUIDText, ForeignKey("cases.id", ondelete="RESTRICT"), nullable=False, unique=True), Column("current_version", Integer, nullable=False), Column("created_at", UTCText(), nullable=False), UniqueConstraint("id", "case_id"))
spec_package_versions = owned("spec_package_versions", Column("package_id", UUIDText, primary_key=True), Column("version", Integer, primary_key=True), Column("case_id", UUIDText, ForeignKey("cases.id", ondelete="RESTRICT"), nullable=False), Column("semantic_hash", HashText, nullable=False), Column("content_schema_version", Integer, nullable=False), Column("hash_schema_version", Integer, nullable=False), Column("state", EnumText, nullable=False), Column("items", JSONText), Column("item_governance", JSONText), Column("created_at", UTCText(), nullable=False), UniqueConstraint("package_id", "version", "case_id"))
spec_requirements = owned("spec_requirements", Column("package_id", UUIDText, primary_key=True), Column("package_version", Integer, primary_key=True), Column("unit_id", UUIDText, primary_key=True), Column("case_id", UUIDText, nullable=False), Column("statement", Text, nullable=False), Column("domain", EnumText, nullable=False), Column("delivery_required", Integer, nullable=False), Column("source_refs", JSONText, nullable=False))
technical_decisions = owned("technical_decisions", Column("package_id", UUIDText, primary_key=True), Column("package_version", Integer, primary_key=True), Column("unit_id", UUIDText, primary_key=True), Column("case_id", UUIDText, nullable=False), Column("statement", Text, nullable=False), Column("domain", EnumText, nullable=False), Column("delivery_required", Integer, nullable=False), Column("provisional", Integer, nullable=False), Column("provisional_delegation_id", UUIDText), Column("source_refs", JSONText, nullable=False))
acceptance_checks = owned("acceptance_checks", Column("package_id", UUIDText, primary_key=True), Column("package_version", Integer, primary_key=True), Column("check_id", UUIDText, primary_key=True), Column("case_id", UUIDText, nullable=False), Column("statement", Text, nullable=False), Column("domain", EnumText, nullable=False), Column("related_unit_ids", JSONText, nullable=False), Column("source_refs", JSONText, nullable=False))
projection_plans = owned("projection_plans", Column("id", UUIDText, primary_key=True), Column("case_id", UUIDText, ForeignKey("cases.id", ondelete="RESTRICT"), nullable=False), Column("target", EnumText, nullable=False), Column("current_version", Integer, nullable=False), Column("created_at", UTCText(), nullable=False), UniqueConstraint("case_id", "target"), UniqueConstraint("id", "case_id"))
projection_plan_versions = owned("projection_plan_versions", Column("plan_id", UUIDText, primary_key=True), Column("version", Integer, primary_key=True), Column("case_id", UUIDText, nullable=False), Column("target", EnumText, nullable=False), Column("project_key", Text), Column("semantic_hash", HashText, nullable=False), Column("content_schema_version", Integer, nullable=False), Column("hash_schema_version", Integer, nullable=False), Column("package_id", UUIDText, nullable=False), Column("package_version", Integer, nullable=False), Column("package_hash", HashText, nullable=False), Column("jira_plan_id", UUIDText), Column("jira_plan_version", Integer), Column("jira_plan_hash", HashText), Column("source_item_bindings", JSONText), Column("retire_tombstones", JSONText, nullable=False), Column("state", EnumText, nullable=False), Column("created_at", UTCText(), nullable=False))
projection_items = owned("projection_items", Column("plan_id", UUIDText, primary_key=True), Column("plan_version", Integer, primary_key=True), Column("item_id", UUIDText, primary_key=True), Column("case_id", UUIDText, nullable=False), Column("generation_key", Text, nullable=False), Column("kind", EnumText, nullable=False), Column("domain", EnumText, nullable=False), Column("title", Text, nullable=False), Column("body", JSONText, nullable=False), Column("parent_item_id", UUIDText), Column("implementation_required", Integer, nullable=False), Column("repository", Text), Column("primary_jira_item_id", UUIDText), Column("source_item_bindings", JSONText), Column("item_semantic_hash", HashText, nullable=False), UniqueConstraint("plan_id", "plan_version", "generation_key"))
projection_item_sources = owned("projection_item_sources", Column("plan_id", UUIDText, primary_key=True), Column("plan_version", Integer, primary_key=True), Column("item_id", UUIDText, primary_key=True), Column("source_unit_id", UUIDText, primary_key=True), Column("case_id", UUIDText, nullable=False))
projection_dependencies = owned("projection_dependencies", Column("plan_id", UUIDText, primary_key=True), Column("plan_version", Integer, primary_key=True), Column("item_id", UUIDText, primary_key=True), Column("depends_on_item_id", UUIDText, primary_key=True), Column("case_id", UUIDText, nullable=False), CheckConstraint("item_id <> depends_on_item_id"))
status_policies = owned("status_policies", Column("id", UUIDText, primary_key=True), Column("case_id", UUIDText, ForeignKey("cases.id", ondelete="RESTRICT"), nullable=False, unique=True), Column("current_version", Integer, nullable=False), Column("created_at", UTCText(), nullable=False), UniqueConstraint("id", "case_id"))
status_policy_versions = owned("status_policy_versions", Column("policy_id", UUIDText, primary_key=True), Column("version", Integer, primary_key=True), Column("case_id", UUIDText, nullable=False), Column("semantic_hash", HashText, nullable=False), Column("content_schema_version", Integer, nullable=False), Column("hash_schema_version", Integer, nullable=False), Column("state", EnumText, nullable=False), Column("mappings", JSONText, nullable=False), Column("rules", JSONText, nullable=False), Column("created_at", UTCText(), nullable=False))
approvals = owned("approvals", Column("id", UUIDText, primary_key=True), Column("case_id", UUIDText, ForeignKey("cases.id", ondelete="RESTRICT"), nullable=False), Column("artifact_kind", EnumText, nullable=False), Column("artifact_id", UUIDText, nullable=False), Column("artifact_version", Integer, nullable=False), Column("artifact_hash", HashText, nullable=False), Column("scope", EnumText, nullable=False), Column("actor_id", UUIDText, nullable=False), Column("delegation_id", UUIDText), Column("later_review_required", Integer, nullable=False), Column("approved_at", UTCText(), nullable=False), UniqueConstraint("case_id", "artifact_kind", "artifact_id", "artifact_version", "artifact_hash", "scope", "actor_id"))
external_operations = owned("external_operations", Column("id", UUIDText, primary_key=True), Column("intent_id", UUIDText, nullable=False), Column("case_id", UUIDText, nullable=False), Column("system", EnumText, nullable=False), Column("plan_id", UUIDText, nullable=False), Column("plan_version", Integer, nullable=False), Column("status_policy_id", UUIDText, nullable=False), Column("status_policy_version", Integer, nullable=False), Column("status_policy_hash", HashText, nullable=False), Column("item_ref", JSONText, nullable=False), Column("action", EnumText, nullable=False), Column("idempotency_key", Text, nullable=False), Column("request_owned_content_hash", HashText, nullable=False), Column("fingerprint", HashText, nullable=False), Column("target_normalized_status", EnumText), Column("contributing_rule_ids", JSONText, nullable=False), Column("status", EnumText, nullable=False), Column("current_attempt", Integer, nullable=False), Column("confirmed_result", JSONText), Column("confirmed_snapshot_sequence", Integer), Column("created_at", UTCText(), nullable=False), Column("updated_at", UTCText(), nullable=False), UniqueConstraint("idempotency_key"), UniqueConstraint("case_id", "intent_id"), UniqueConstraint("id", "case_id"))
external_operation_attempts = owned("external_operation_attempts", Column("operation_id", UUIDText, primary_key=True), Column("attempt", Integer, primary_key=True), Column("case_id", UUIDText, nullable=False), Column("request", JSONText, nullable=False), Column("expected_remote_revision", Text), Column("status", EnumText, nullable=False), Column("failure_code", Text), Column("result", JSONText), Column("started_at", UTCText(), nullable=False), Column("completed_at", UTCText()))
external_bindings = owned("external_bindings", Column("id", UUIDText, primary_key=True), Column("case_id", UUIDText, nullable=False), Column("system", EnumText, nullable=False), Column("projection_plan_id", UUIDText, nullable=False), Column("item_id", UUIDText, nullable=False), Column("current_plan_version", Integer, nullable=False), Column("generation_key", Text, nullable=False), Column("external_identity_key", Text, nullable=False), Column("external_identity", JSONText, nullable=False), Column("current_observation_sequence", Integer, nullable=False), Column("confirmed_at", UTCText(), nullable=False), UniqueConstraint("system", "external_identity_key"), UniqueConstraint("case_id", "system", "projection_plan_id", "item_id"), UniqueConstraint("id", "case_id"))
remote_snapshots = owned("remote_snapshots", Column("binding_id", UUIDText, primary_key=True), Column("observation_sequence", Integer, primary_key=True), Column("observation_kind", EnumText, nullable=False), Column("case_id", UUIDText, nullable=False), Column("operation_id", UUIDText), Column("status_policy_id", UUIDText, nullable=False), Column("status_policy_version", Integer, nullable=False), Column("status_policy_hash", HashText, nullable=False), Column("remote_revision", Text), Column("expected_previous_remote_revision", Text), Column("native_status", Text), Column("normalized_status_at_acceptance", EnumText), Column("lifecycle_at_acceptance", EnumText), Column("owned_content", JSONText), Column("owned_content_hash", HashText), Column("observed_at", UTCText(), nullable=False), UniqueConstraint("binding_id", "remote_revision"))
traceability_edges = owned("traceability_edges", Column("id", UUIDText, primary_key=True), Column("case_id", UUIDText, nullable=False), Column("edge_type", EnumText, nullable=False), Column("from_kind", EnumText, nullable=False), Column("from_id", UUIDText, nullable=False), Column("from_version", Integer), Column("to_kind", EnumText, nullable=False), Column("to_id", UUIDText, nullable=False), Column("to_version", Integer), Column("created_at", UTCText(), nullable=False), UniqueConstraint("case_id", "edge_type", "from_kind", "from_id", "from_version", "to_kind", "to_id", "to_version"))
drift_findings = owned("drift_findings", Column("id", UUIDText, primary_key=True), Column("case_id", UUIDText, nullable=False), Column("category", EnumText, nullable=False), Column("affected_binding", JSONText, nullable=False), Column("expected_value", JSONText, nullable=False), Column("observed_value", JSONText, nullable=False), Column("active", Integer, nullable=False), Column("created_at", UTCText(), nullable=False), Column("resolved_at", UTCText()))
audit_events = owned("audit_events", Column("event_id", UUIDText, primary_key=True), Column("case_id", UUIDText, ForeignKey("cases.id", ondelete="RESTRICT"), nullable=False), Column("case_sequence", Integer, nullable=False), Column("command_id", UUIDText, nullable=False), Column("command_name", Text, nullable=False), Column("command_fingerprint", HashText, nullable=False), Column("actor", Text, nullable=False), Column("occurred_at", UTCText(), nullable=False), Column("target_ids", JSONText, nullable=False), Column("before_case_revision", Integer, nullable=False), Column("after_case_revision", Integer, nullable=False), Column("metadata", JSONText, nullable=False), Column("result", JSONText, nullable=False), UniqueConstraint("case_id", "case_sequence"), UniqueConstraint("case_id", "command_id"), CheckConstraint("case_sequence = after_case_revision"))

# Workshop Protocol 1.0.0 is an additive Foundation capability.  It registers
# its tables on this same metadata graph so Alembic, fresh-database creation,
# and the canonical Foundation repository cannot silently diverge.
from .workshop_protocol_storage import define_workshop_protocol_tables
from .artifact_quality_storage import define_artifact_quality_tables

WORKSHOP_PROTOCOL_TABLES = define_workshop_protocol_tables(metadata)
from .workshop_protocol_storage import V0_RUNTIME_TABLE_NAMES
V0_RUNTIME_TABLES = {name: metadata.tables[name] for name in V0_RUNTIME_TABLE_NAMES}
ARTIFACT_QUALITY_TABLES = define_artifact_quality_tables(metadata)

# Relationally expressible references are composite and case-scoped.  Cyclic
# root/current-version constraints are deferred so a root and version can be
# inserted atomically in either statement order within one unit of work.
spec_packages.append_constraint(ForeignKeyConstraint(["id", "current_version"], ["spec_package_versions.package_id", "spec_package_versions.version"], ondelete="RESTRICT", deferrable=True, initially="DEFERRED"))
spec_package_versions.append_constraint(ForeignKeyConstraint(["package_id", "case_id"], ["spec_packages.id", "spec_packages.case_id"], ondelete="RESTRICT", deferrable=True, initially="DEFERRED"))
for table in (spec_requirements, acceptance_checks):
    table.append_constraint(ForeignKeyConstraint(["package_id", "package_version", "case_id"], ["spec_package_versions.package_id", "spec_package_versions.version", "spec_package_versions.case_id"], ondelete="RESTRICT"))
technical_decisions.append_constraint(ForeignKeyConstraint(["package_id", "package_version", "case_id"], ["spec_package_versions.package_id", "spec_package_versions.version", "spec_package_versions.case_id"], ondelete="RESTRICT"))
technical_decisions.append_constraint(ForeignKeyConstraint(["provisional_delegation_id", "case_id"], ["delegations.id", "delegations.case_id"], ondelete="RESTRICT"))

projection_plans.append_constraint(ForeignKeyConstraint(["id", "current_version"], ["projection_plan_versions.plan_id", "projection_plan_versions.version"], ondelete="RESTRICT", deferrable=True, initially="DEFERRED"))
projection_plan_versions.append_constraint(ForeignKeyConstraint(["plan_id", "case_id"], ["projection_plans.id", "projection_plans.case_id"], ondelete="RESTRICT", deferrable=True, initially="DEFERRED"))
projection_plan_versions.append_constraint(UniqueConstraint("plan_id", "version", "case_id"))
projection_plan_versions.append_constraint(ForeignKeyConstraint(["package_id", "package_version", "case_id"], ["spec_package_versions.package_id", "spec_package_versions.version", "spec_package_versions.case_id"], ondelete="RESTRICT"))
projection_plan_versions.append_constraint(ForeignKeyConstraint(["jira_plan_id", "jira_plan_version", "case_id"], ["projection_plan_versions.plan_id", "projection_plan_versions.version", "projection_plan_versions.case_id"], ondelete="RESTRICT"))
projection_items.append_constraint(ForeignKeyConstraint(["plan_id", "plan_version", "case_id"], ["projection_plan_versions.plan_id", "projection_plan_versions.version", "projection_plan_versions.case_id"], ondelete="RESTRICT"))
projection_items.append_constraint(ForeignKeyConstraint(["plan_id", "plan_version", "parent_item_id"], ["projection_items.plan_id", "projection_items.plan_version", "projection_items.item_id"], ondelete="RESTRICT"))
projection_item_sources.append_constraint(ForeignKeyConstraint(["plan_id", "plan_version", "item_id"], ["projection_items.plan_id", "projection_items.plan_version", "projection_items.item_id"], ondelete="RESTRICT"))
projection_dependencies.append_constraint(ForeignKeyConstraint(["plan_id", "plan_version", "item_id"], ["projection_items.plan_id", "projection_items.plan_version", "projection_items.item_id"], ondelete="RESTRICT"))
projection_dependencies.append_constraint(ForeignKeyConstraint(["plan_id", "plan_version", "depends_on_item_id"], ["projection_items.plan_id", "projection_items.plan_version", "projection_items.item_id"], ondelete="RESTRICT"))

status_policies.append_constraint(ForeignKeyConstraint(["id", "current_version"], ["status_policy_versions.policy_id", "status_policy_versions.version"], ondelete="RESTRICT", deferrable=True, initially="DEFERRED"))
status_policy_versions.append_constraint(ForeignKeyConstraint(["policy_id", "case_id"], ["status_policies.id", "status_policies.case_id"], ondelete="RESTRICT", deferrable=True, initially="DEFERRED"))
status_policy_versions.append_constraint(UniqueConstraint("policy_id", "version", "case_id"))
approvals.append_constraint(ForeignKeyConstraint(["delegation_id", "case_id"], ["delegations.id", "delegations.case_id"], ondelete="RESTRICT"))
external_operations.append_constraint(ForeignKeyConstraint(["plan_id", "plan_version", "case_id"], ["projection_plan_versions.plan_id", "projection_plan_versions.version", "projection_plan_versions.case_id"], ondelete="RESTRICT"))
external_operations.append_constraint(ForeignKeyConstraint(["status_policy_id", "status_policy_version", "case_id"], ["status_policy_versions.policy_id", "status_policy_versions.version", "status_policy_versions.case_id"], ondelete="RESTRICT"))
external_operation_attempts.append_constraint(ForeignKeyConstraint(["operation_id", "case_id"], ["external_operations.id", "external_operations.case_id"], ondelete="RESTRICT"))
external_bindings.append_constraint(ForeignKeyConstraint(["projection_plan_id", "case_id"], ["projection_plans.id", "projection_plans.case_id"], ondelete="RESTRICT"))
remote_snapshots.append_constraint(ForeignKeyConstraint(["binding_id", "case_id"], ["external_bindings.id", "external_bindings.case_id"], ondelete="RESTRICT"))
remote_snapshots.append_constraint(ForeignKeyConstraint(["operation_id", "case_id"], ["external_operations.id", "external_operations.case_id"], ondelete="RESTRICT"))
remote_snapshots.append_constraint(ForeignKeyConstraint(["status_policy_id", "status_policy_version", "case_id"], ["status_policy_versions.policy_id", "status_policy_versions.version", "status_policy_versions.case_id"], ondelete="RESTRICT"))
for table in (traceability_edges, drift_findings):
    table.append_constraint(ForeignKeyConstraint(["case_id"], ["cases.id"], ondelete="RESTRICT"))

delegations.append_constraint(CheckConstraint("(artifact_kind IS NULL) = (artifact_id IS NULL)"))
ambiguity_findings.append_constraint(CheckConstraint("(status = 'OPEN' AND resolved_at IS NULL) OR (status = 'RESOLVED' AND resolved_at IS NOT NULL)"))
technical_decisions.append_constraint(CheckConstraint("(provisional = 1 AND provisional_delegation_id IS NOT NULL) OR (provisional = 0 AND provisional_delegation_id IS NULL)"))
projection_plan_versions.append_constraint(CheckConstraint("(target = 'JIRA' AND project_key IS NOT NULL AND jira_plan_id IS NULL AND jira_plan_version IS NULL AND jira_plan_hash IS NULL) OR (target = 'GITHUB' AND project_key IS NULL AND jira_plan_id IS NOT NULL AND jira_plan_version IS NOT NULL AND jira_plan_hash IS NOT NULL)"))
external_operation_attempts.append_constraint(CheckConstraint("(status = 'PENDING' AND failure_code IS NULL AND result IS NULL AND completed_at IS NULL) OR (status = 'FAILED' AND failure_code IS NOT NULL AND result IS NOT NULL AND completed_at IS NOT NULL) OR (status IN ('UNKNOWN', 'SUCCEEDED') AND failure_code IS NULL AND result IS NOT NULL AND completed_at IS NOT NULL)"))
remote_snapshots.append_constraint(CheckConstraint("(observation_kind = 'FOUND' AND remote_revision IS NOT NULL AND native_status IS NOT NULL AND normalized_status_at_acceptance IS NOT NULL AND lifecycle_at_acceptance IS NOT NULL AND owned_content IS NOT NULL AND owned_content_hash IS NOT NULL AND ((observation_sequence = 1 AND expected_previous_remote_revision IS NULL) OR (observation_sequence > 1 AND expected_previous_remote_revision IS NOT NULL))) OR (observation_kind = 'NOT_FOUND' AND expected_previous_remote_revision IS NOT NULL AND remote_revision IS NULL AND native_status IS NULL AND normalized_status_at_acceptance IS NULL AND lifecycle_at_acceptance IS NULL AND owned_content IS NULL AND owned_content_hash IS NULL)"))
drift_findings.append_constraint(CheckConstraint("(active = 1 AND resolved_at IS NULL) OR (active = 0 AND resolved_at IS NOT NULL)"))
audit_events.append_constraint(CheckConstraint("before_case_revision >= 0 AND before_case_revision <= 9223372036854775807"))

BOOLEAN_COLUMNS = {
    ("delegations", "later_review_required"),
    ("spec_requirements", "delivery_required"),
    ("technical_decisions", "delivery_required"),
    ("technical_decisions", "provisional"),
    ("projection_items", "implementation_required"),
    ("approvals", "later_review_required"),
    ("drift_findings", "active"),
    ("workshop_guidance", "valid"),
    ("workshop_review_narrations", "valid"),
    ("workshop_decision_views", "current"),
    ("workshop_artifact_reviews", "current"),
    ("workshop_artifact_reviews", "confirmed"),
}
for table_name, column_name in BOOLEAN_COLUMNS:
    metadata.tables[table_name].append_constraint(CheckConstraint(f"{column_name} IN (0, 1)"))

for table in metadata.tables.values():
    for column in table.columns:
        if not isinstance(column.type, Integer) or (table.name, column.name) in BOOLEAN_COLUMNS:
            continue
        lower = 0 if (table.name, column.name) in {
            ("audit_events", "before_case_revision"),
            ("workshop_analyzer_jobs", "attempt_count"),
        } else 1
        bounds = f"{column.name} >= {lower} AND {column.name} <= 9223372036854775807"
        table.append_constraint(CheckConstraint(f"{column.name} IS NULL OR ({bounds})" if column.nullable else bounds))

def _closed_values(*enum_types: type[Any]) -> tuple[str, ...]:
    return tuple(item.value for enum_type in enum_types for item in enum_type)

ENUM_COLUMNS: dict[tuple[str, str], tuple[str, ...]] = {
    ("delegations", "domain"): _closed_values(Domain),
    ("delegations", "artifact_kind"): _closed_values(ArtifactKind),
    ("source_artifacts", "type"): _closed_values(SourceArtifactType),
    ("source_artifacts", "media_type"): ("application/json", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "text/markdown", "text/plain", "application/octet-stream"),
    ("ambiguity_findings", "category"): _closed_values(AmbiguityCategory),
    ("ambiguity_findings", "domain"): _closed_values(Domain),
    ("ambiguity_findings", "severity"): _closed_values(Severity),
    ("ambiguity_findings", "status"): _closed_values(FindingStatus),
    ("spec_package_versions", "state"): _closed_values(PackageState),
    ("spec_requirements", "domain"): _closed_values(Domain),
    ("technical_decisions", "domain"): _closed_values(Domain),
    ("acceptance_checks", "domain"): _closed_values(Domain),
    ("projection_plans", "target"): _closed_values(PlanTarget),
    ("projection_plan_versions", "target"): _closed_values(PlanTarget),
    ("projection_plan_versions", "state"): _closed_values(PlanState),
    ("projection_items", "kind"): _closed_values(JiraKind, GitHubKind),
    ("projection_items", "domain"): _closed_values(Domain),
    ("status_policy_versions", "state"): _closed_values(PolicyState),
    ("approvals", "artifact_kind"): _closed_values(ArtifactKind),
    ("approvals", "scope"): _closed_values(ApprovalScope),
    ("external_operations", "system"): _closed_values(System),
    ("external_operations", "action"): _closed_values(Action),
    ("external_operations", "target_normalized_status"): _closed_values(JiraStatus, GitHubStatus),
    ("external_operations", "status"): _closed_values(OperationStatus),
    ("external_operation_attempts", "status"): _closed_values(OperationStatus),
    ("external_bindings", "system"): _closed_values(System),
    ("remote_snapshots", "observation_kind"): _closed_values(ObservationKind),
    ("remote_snapshots", "normalized_status_at_acceptance"): _closed_values(JiraStatus, GitHubStatus),
    ("remote_snapshots", "lifecycle_at_acceptance"): _closed_values(Lifecycle),
    ("traceability_edges", "edge_type"): _closed_values(TraceEdgeType),
    ("traceability_edges", "from_kind"): _closed_values(TraceNodeKind),
    ("traceability_edges", "to_kind"): _closed_values(TraceNodeKind),
    ("drift_findings", "category"): _closed_values(FindingCategory),
}
for (table_name, column_name), values in ENUM_COLUMNS.items():
    allowed = ", ".join(repr(value) for value in values)
    metadata.tables[table_name].append_constraint(CheckConstraint(f"{column_name} IN ({allowed})"))

Index("ix_delegations_case_delegate_revoked", delegations.c.case_id, delegations.c.delegate_id, delegations.c.revoked_at)
Index("ix_ambiguity_case_status_severity", ambiguity_findings.c.case_id, ambiguity_findings.c.status, ambiguity_findings.c.severity)
Index("ix_projection_items_case_item", projection_items.c.case_id, projection_items.c.item_id)
Index("ix_approvals_binding", approvals.c.case_id, approvals.c.artifact_kind, approvals.c.artifact_id, approvals.c.artifact_version, approvals.c.artifact_hash)
Index("ix_external_operations_case_status", external_operations.c.case_id, external_operations.c.status)
Index("ix_drift_findings_case_active_category", drift_findings.c.case_id, drift_findings.c.active, drift_findings.c.category)
Index("ix_trace_from", traceability_edges.c.case_id, traceability_edges.c.from_kind, traceability_edges.c.from_id)
Index("ix_trace_to", traceability_edges.c.case_id, traceability_edges.c.to_kind, traceability_edges.c.to_id)
Index("ix_audit_command", audit_events.c.command_id)


def engine_for(database_url: str) -> Engine:
    engine = create_engine(database_url, future=True)
    if database_url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def foreign_keys(dbapi_connection, _):
            cursor = dbapi_connection.cursor(); cursor.execute("PRAGMA foreign_keys=ON"); cursor.close()
    return engine


def migrate(database_url: str) -> None:
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini")); config.set_main_option("sqlalchemy.url", database_url); command.upgrade(config, "head")


class SqlAlchemyStore:
    def __init__(self, database_url: str, *, registry: SchemaRegistry | None = None) -> None:
        self.engine = engine_for(database_url)
        self.registry = registry or default_registry()

    def load_cases(self) -> dict[UUID, CaseState]:
        loaded: dict[UUID, CaseState] = {}
        with self.engine.connect() as connection:
            for row in connection.execute(select(cases)).mappings():
                state = CaseState(UUID(row["id"]), UUID(row["pm_actor_id"]), UUID(row["dev_lead_actor_id"]), row["created_at"], row["revision"])
                actors = connection.execute(select(case_participants.c.actor_id).where(case_participants.c.case_id == row["id"])).scalars()
                state.participants = {UUID(value) for value in actors}; self._hydrate_case(connection, state); loaded[state.id] = state
        return loaded

    def _hydrate_case(self, connection, case: CaseState) -> None:
        """Hydrate persisted rows only through their public Pydantic schemas."""
        cid = str(case.id)
        refs = TypeAdapter(list[SourceRef])
        resolutions = TypeAdapter(list[ResolutionRecord])
        identity_type = TypeAdapter(JiraIdentity | GitHubIdentity)
        finding_value = TypeAdapter(FindingValue)

        def artifact_binding(kind: ArtifactKind, root: ArtifactRoot, version: int) -> ArtifactBinding:
            stored = next(item for item in root.versions if item.version == version)
            return ArtifactBinding(artifact_kind=kind, artifact_id=root.artifact_id, version=version, semantic_hash=stored.semantic_hash)

        for row in connection.execute(select(delegations).where(delegations.c.case_id == cid)).mappings():
            item = DelegationState(
                UUID(row["id"]), UUID(row["delegator_id"]), UUID(row["delegate_id"]), Domain(row["domain"]),
                frozenset(json.loads(row["command_names"])), row["artifact_kind"], UUID(row["artifact_id"]) if row["artifact_id"] else None,
                row["valid_from"], row["valid_until"], bool(row["later_review_required"]), row["created_at"], row["revoked_at"],
            )
            case.delegations[item.id] = item

        for row in connection.execute(select(source_artifacts).where(source_artifacts.c.case_id == cid)).mappings():
            item = SourceArtifactIdentity(
                artifact_id=UUID(row["artifact_id"]), case_id=case.id, type=SourceArtifactType(row["type"]), version=row["version"],
                media_type=row["media_type"], canonical_locator=row["canonical_locator"], content_hash=row["content_hash"], registered_at=row["registered_at"],
            )
            case.sources[(item.artifact_id, item.version)] = item

        for row in connection.execute(select(ambiguity_findings).where(ambiguity_findings.c.case_id == cid)).mappings():
            item = AmbiguityState(
                UUID(row["id"]), row["category"], Domain(row["domain"]), row["severity"], refs.validate_json(row["evidence_refs"]),
                row["clarification_question"], row["created_at"], FindingStatus(row["status"]), resolutions.validate_json(row["resolutions"]), row["resolved_at"],
                ItemBinding.model_validate_json(row["item_binding"]) if row["item_binding"] else None,
            )
            case.ambiguities[item.id] = item

        package = connection.execute(select(spec_packages).where(spec_packages.c.case_id == cid)).mappings().one_or_none()
        if package:
            root = ArtifactRoot(ArtifactKind.SPEC_PACKAGE, UUID(package["id"]))
            versions = connection.execute(
                select(spec_package_versions).where(spec_package_versions.c.package_id == package["id"]).order_by(spec_package_versions.c.version)
            ).mappings()
            for version in versions:
                criteria = (spec_requirements.c.package_id == package["id"], spec_requirements.c.package_version == version["version"])
                reqs = [
                    Requirement(unit_id=UUID(row["unit_id"]), statement=row["statement"], domain=Domain(row["domain"]), delivery_required=bool(row["delivery_required"]), source_refs=refs.validate_json(row["source_refs"]))
                    for row in connection.execute(select(spec_requirements).where(*criteria)).mappings()
                ]
                decisions = [
                    TechnicalDecision(
                        unit_id=UUID(row["unit_id"]), statement=row["statement"], domain=Domain(row["domain"]), delivery_required=bool(row["delivery_required"]),
                        provisional=bool(row["provisional"]), provisional_delegation_id=UUID(row["provisional_delegation_id"]) if row["provisional_delegation_id"] else None,
                        source_refs=refs.validate_json(row["source_refs"]),
                    )
                    for row in connection.execute(select(technical_decisions).where(technical_decisions.c.package_id == package["id"], technical_decisions.c.package_version == version["version"])).mappings()
                ]
                checks = [
                    AcceptanceCheck(
                        check_id=UUID(row["check_id"]), statement=row["statement"], domain=Domain(row["domain"]),
                        related_unit_ids=[UUID(value) for value in json.loads(row["related_unit_ids"])], source_refs=refs.validate_json(row["source_refs"]),
                    )
                    for row in connection.execute(select(acceptance_checks).where(acceptance_checks.c.package_id == package["id"], acceptance_checks.c.package_version == version["version"])).mappings()
                ]
                package_value: dict[str, Any] = {"requirements": reqs, "technical_decisions": decisions, "acceptance_checks": checks}
                if version["content_schema_version"] == 2:
                    if not version["items"]:
                        raise ValueError("persisted v2 package is missing SpecPackageItems")
                    package_value["items"] = TypeAdapter(list[SpecPackageItem]).validate_json(version["items"])
                payload = self.registry.validate(
                    ArtifactKind.SPEC_PACKAGE,
                    version["content_schema_version"],
                    version["hash_schema_version"],
                    package_value,
                )
                from .service import WorkflowService
                helper = object.__new__(WorkflowService)
                computed_hash = self.registry.hash(
                    ArtifactKind.SPEC_PACKAGE,
                    version["content_schema_version"],
                    version["hash_schema_version"],
                    helper._normalized_package_payload(payload),
                )
                if computed_hash != version["semantic_hash"]:
                    raise ValueError("persisted spec-package semantic hash mismatch")
                root.versions.append(ArtifactVersion(version["version"], version["semantic_hash"], version["content_schema_version"], version["hash_schema_version"], payload, version["state"]))
            case.package = root
            from .service import WorkflowService
            item_helper = object.__new__(WorkflowService)
            item_helper._rebuild_package_items(case)
            current_row = connection.execute(
                select(spec_package_versions.c.item_governance).where(
                    spec_package_versions.c.package_id == package["id"],
                    spec_package_versions.c.version == package["current_version"],
                )
            ).scalar_one_or_none()
            if current_row:
                governance = json.loads(current_row)
                marked = TypeAdapter(list[ItemBinding]).validate_json(
                    json.dumps(governance.get("marked_ready_item_bindings", []))
                )
                marked_keys = {(value.item_id, value.item_version, value.item_hash) for value in marked}
                for history in case.package_items.values():
                    for state in history:
                        state.marked_ready = (state.binding.item_id, state.binding.item_version, state.binding.item_hash) in marked_keys
                reviews = TypeAdapter(list[ReviewRequest]).validate_json(
                    json.dumps(governance.get("review_requests", []))
                )
                case.review_requests = {value.review_request_id: value for value in reviews}

        for plan_row in connection.execute(select(projection_plans).where(projection_plans.c.case_id == cid)).mappings():
            target = PlanTarget(plan_row["target"])
            root = ArtifactRoot(ArtifactKind.PROJECTION_PLAN, UUID(plan_row["id"]))
            version_rows = connection.execute(
                select(projection_plan_versions).where(projection_plan_versions.c.plan_id == plan_row["id"]).order_by(projection_plan_versions.c.version)
            ).mappings()
            previous_items: dict[UUID, str] = {}
            for version in version_rows:
                items: list[ProjectionItem] = []
                item_hashes: dict[UUID, str] = {}
                rows = connection.execute(
                    select(projection_items).where(projection_items.c.plan_id == plan_row["id"], projection_items.c.plan_version == version["version"])
                ).mappings()
                for row in rows:
                    item_id = UUID(row["item_id"])
                    sources = [
                        UUID(value) for value in connection.execute(
                            select(projection_item_sources.c.source_unit_id).where(
                                projection_item_sources.c.plan_id == plan_row["id"], projection_item_sources.c.plan_version == version["version"],
                                projection_item_sources.c.item_id == row["item_id"],
                            )
                        ).scalars()
                    ]
                    dependencies = [
                        UUID(value) for value in connection.execute(
                            select(projection_dependencies.c.depends_on_item_id).where(
                                projection_dependencies.c.plan_id == plan_row["id"], projection_dependencies.c.plan_version == version["version"],
                                projection_dependencies.c.item_id == row["item_id"],
                            )
                        ).scalars()
                    ]
                    kind = JiraKind(row["kind"]) if target == PlanTarget.JIRA else GitHubKind(row["kind"])
                    item_type = ProjectionItemV2 if version["content_schema_version"] == 2 else ProjectionItem
                    item_values = dict(
                        item_id=item_id, kind=kind, domain=Domain(row["domain"]), title=row["title"],
                        body=StructuredWorkBody.model_validate_json(row["body"]), source_unit_ids=sources,
                        parent_item_id=UUID(row["parent_item_id"]) if row["parent_item_id"] else None,
                        dependency_item_ids=dependencies, implementation_required=bool(row["implementation_required"]), repository=row["repository"],
                        primary_jira_item_id=UUID(row["primary_jira_item_id"]) if row["primary_jira_item_id"] else None,
                    )
                    if item_type is ProjectionItemV2:
                        if not row["source_item_bindings"]: raise ValueError("persisted v2 projection item is missing ItemBindings")
                        item_values["source_item_bindings"] = TypeAdapter(list[ItemBinding]).validate_json(row["source_item_bindings"])
                    items.append(item_type(**item_values))
                    item_hashes[item_id] = row["item_semantic_hash"]
                package_binding = ArtifactBinding(
                    artifact_kind=ArtifactKind.SPEC_PACKAGE, artifact_id=UUID(version["package_id"]),
                    version=version["package_version"], semantic_hash=version["package_hash"],
                )
                jira_binding = None
                if version["jira_plan_id"]:
                    jira_binding = ArtifactBinding(
                        artifact_kind=ArtifactKind.PROJECTION_PLAN, artifact_id=UUID(version["jira_plan_id"]),
                        version=version["jira_plan_version"], semantic_hash=version["jira_plan_hash"],
                    )
                plan_value: dict[str, Any] = {"package_binding": package_binding, "target": target, "project_key": version["project_key"], "jira_plan_binding": jira_binding, "items": items}
                if version["content_schema_version"] == 2:
                    if not version["source_item_bindings"]: raise ValueError("persisted v2 projection plan is missing ItemBindings")
                    plan_value["source_item_bindings"] = TypeAdapter(list[ItemBinding]).validate_json(version["source_item_bindings"])
                payload = self.registry.validate(
                    ArtifactKind.PROJECTION_PLAN,
                    version["content_schema_version"],
                    version["hash_schema_version"],
                    plan_value,
                )
                from .service import WorkflowService
                computed_hash = self.registry.hash(
                    ArtifactKind.PROJECTION_PLAN,
                    version["content_schema_version"],
                    version["hash_schema_version"],
                    helper._normalized_plan_payload(payload),
                )
                if computed_hash != version["semantic_hash"]:
                    raise ValueError("persisted projection-plan semantic hash mismatch")
                root.versions.append(ArtifactVersion(version["version"], version["semantic_hash"], version["content_schema_version"], version["hash_schema_version"], payload, version["state"]))
                case.metadata.setdefault("item_hashes", {})[(root.artifact_id, version["version"])] = item_hashes
                tombstones = {
                    UUID(entry["item"]["item_id"]): {
                        "item": ProjectionItem.model_validate_json(json.dumps(entry["item"])),
                        "prior_plan_version": entry["prior_plan_version"],
                        "prior_plan_hash": entry["prior_plan_hash"],
                    }
                    for entry in json.loads(version["retire_tombstones"])
                }
                case.metadata.setdefault("tombstones", {})[
                    (root.artifact_id, version["version"])
                ] = tombstones
                current_items = {item.item_id: item_hashes[item.item_id] for item in items}
                if previous_items or tombstones:
                    actions: dict[UUID, Action] = {}
                    for item_id, digest in current_items.items():
                        if item_id not in previous_items: actions[item_id] = Action.CREATE
                        elif previous_items[item_id] != digest: actions[item_id] = Action.UPDATE
                    for item_id in tombstones: actions[item_id] = Action.RETIRE
                    case.metadata.setdefault("reconciliation", {})[(root.artifact_id, version["version"])] = actions
                previous_items = current_items
            case.plans[target] = root

        policy_row = connection.execute(select(status_policies).where(status_policies.c.case_id == cid)).mappings().one_or_none()
        if policy_row:
            root = ArtifactRoot(ArtifactKind.STATUS_POLICY, UUID(policy_row["id"]))
            for version in connection.execute(select(status_policy_versions).where(status_policy_versions.c.policy_id == policy_row["id"]).order_by(status_policy_versions.c.version)).mappings():
                payload = self.registry.validate(
                    ArtifactKind.STATUS_POLICY,
                    version["content_schema_version"],
                    version["hash_schema_version"],
                    {
                        "mappings": TypeAdapter(list[NativeStatusMapping]).validate_json(version["mappings"]),
                        "rules": TypeAdapter(list[StatusRule]).validate_json(version["rules"]),
                    },
                )
                from .service import WorkflowService
                computed_hash = self.registry.hash(
                    ArtifactKind.STATUS_POLICY,
                    version["content_schema_version"],
                    version["hash_schema_version"],
                    WorkflowService._normalized_policy_payload(payload),
                )
                if computed_hash != version["semantic_hash"]:
                    raise ValueError("persisted status-policy semantic hash mismatch")
                root.versions.append(ArtifactVersion(version["version"], version["semantic_hash"], version["content_schema_version"], version["hash_schema_version"], payload, version["state"]))
            case.policy = root

        for row in connection.execute(select(approvals).where(approvals.c.case_id == cid)).mappings():
            delegation = case.delegations.get(UUID(row["delegation_id"])) if row["delegation_id"] else None
            case.approvals.append(ApprovalState(
                UUID(row["id"]), row["artifact_kind"], UUID(row["artifact_id"]), row["artifact_version"], row["artifact_hash"], ApprovalScope(row["scope"]),
                UUID(row["actor_id"]), delegation.id if delegation else None, delegation.delegator_id if delegation else None,
                bool(row["later_review_required"]), row["approved_at"],
            ))

        for row in connection.execute(select(external_bindings).where(external_bindings.c.case_id == cid)).mappings():
            identity = identity_type.validate_json(row["external_identity"])
            item = BindingState(
                UUID(row["id"]), case.id, System(row["system"]), UUID(row["projection_plan_id"]), UUID(row["item_id"]), row["generation_key"],
                identity, row["current_plan_version"], row["current_observation_sequence"], row["confirmed_at"],
            )
            case.bindings[item.id] = item
            case.metadata.setdefault("external_bindings", {})[item.item_id] = {"binding_id": item.id, "key": identity.key} if isinstance(identity, JiraIdentity) else {"binding_id": item.id, "identity": identity}

        for binding in case.bindings.values():
            rows = connection.execute(
                select(remote_snapshots).where(remote_snapshots.c.binding_id == str(binding.id)).order_by(remote_snapshots.c.observation_sequence)
            ).mappings()
            for row in rows:
                plan_root = next(root for root in case.plans.values() if root.artifact_id == binding.plan_id)
                plan_binding = artifact_binding(ArtifactKind.PROJECTION_PLAN, plan_root, binding.current_plan_version)
                policy_binding = artifact_binding(ArtifactKind.STATUS_POLICY, case.policy, row["status_policy_version"])
                jira_binding = case.plans[PlanTarget.JIRA].binding if binding.system == System.GITHUB and PlanTarget.JIRA in case.plans else None
                observation = RemoteObservation(
                    observation_kind=ObservationKind(row["observation_kind"]), system=binding.system, external_identity=binding.external_identity,
                    generation_key=binding.generation_key, package_binding=case.package.binding, plan_binding=plan_binding,
                    jira_plan_binding=jira_binding, status_policy_binding=policy_binding, remote_revision=row["remote_revision"],
                    expected_previous_remote_revision=row["expected_previous_remote_revision"], native_status=row["native_status"],
                    owned_content=json.loads(row["owned_content"]) if row["owned_content"] else None,
                )
                binding.snapshots.append(observation)

        for row in connection.execute(select(external_operations).where(external_operations.c.case_id == cid)).mappings():
            attempt = connection.execute(
                select(external_operation_attempts).where(external_operation_attempts.c.operation_id == row["id"]).order_by(external_operation_attempts.c.attempt.desc())
            ).mappings().first()
            request = json.loads(attempt["request"])
            plan_root = next(root for root in case.plans.values() if root.artifact_id == UUID(row["plan_id"]))
            plan_binding = artifact_binding(ArtifactKind.PROJECTION_PLAN, plan_root, row["plan_version"])
            package_binding = case.package.binding
            policy_binding = artifact_binding(ArtifactKind.STATUS_POLICY, case.policy, row["status_policy_version"])
            jira_binding = case.plans[PlanTarget.JIRA].binding if row["system"] == System.GITHUB.value and PlanTarget.JIRA in case.plans else None
            target_status = None
            if row["target_normalized_status"]:
                target_status = JiraStatus(row["target_normalized_status"]) if row["system"] == System.JIRA.value else GitHubStatus(row["target_normalized_status"])
            intent = OperationIntent(
                intent_id=UUID(row["intent_id"]), system=System(row["system"]), item_ref=json.loads(row["item_ref"]), action=Action(row["action"]), request=request,
                package_binding=package_binding, plan_binding=plan_binding, jira_plan_binding=jira_binding, status_policy_binding=policy_binding,
                request_owned_content_hash=row["request_owned_content_hash"], fingerprint=row["fingerprint"], expected=attempt["expected_remote_revision"],
                target_normalized_status=target_status, contributing_rule_ids=[UUID(value) for value in json.loads(row["contributing_rule_ids"])],
            )
            confirmation = RemoteObservation.model_validate_json(row["confirmed_result"]) if row["confirmed_result"] else None
            operation = OperationState(
                UUID(row["id"]), intent, row["idempotency_key"], OperationStatus(row["status"]), row["current_attempt"],
                row["created_at"], row["updated_at"], attempt["failure_code"], confirmation, row["confirmed_snapshot_sequence"], None,
                attempt["started_at"],
            )
            case.operations[operation.id] = operation

        for row in connection.execute(select(drift_findings).where(drift_findings.c.case_id == cid)).mappings():
            case.metadata.setdefault("findings", {})[UUID(row["id"])] = {
                "category": FindingCategory(row["category"]), "affected": finding_value.validate_json(row["affected_binding"]),
                "expected": finding_value.validate_json(row["expected_value"]), "observed": finding_value.validate_json(row["observed_value"]),
                "active": bool(row["active"]), "created_at": row["created_at"], "resolved_at": row["resolved_at"],
            }

        for row in connection.execute(select(traceability_edges).where(traceability_edges.c.case_id == cid)).mappings():
            edge = TraceEdge(
                id=UUID(row["id"]), edge_type=TraceEdgeType(row["edge_type"]),
                from_endpoint=TraceEndpoint(kind=TraceNodeKind(row["from_kind"]), id=UUID(row["from_id"]), version=row["from_version"]),
                to_endpoint=TraceEndpoint(kind=TraceNodeKind(row["to_kind"]), id=UUID(row["to_id"]), version=row["to_version"]),
            )
            case.metadata.setdefault("trace_edges", {})[edge.id] = edge

        result_models = {
            "create_case": CaseResult, "add_participant": ParticipantResult, "grant_delegation": DelegationResult,
            "revoke_delegation": DelegationResult, "register_source_artifact": SourceArtifactResult,
            "record_ambiguity_finding": AmbiguityFindingResult, "resolve_ambiguity_finding": AmbiguityFindingResult,
            "create_spec_package": SpecPackageResult, "revise_spec_package": SpecPackageResult, "mark_spec_package_ready": SpecPackageResult,
            "mark_spec_package_item_ready": ItemGovernanceResult, "approve_spec_package_item": ItemGovernanceResult,
            "create_review_request": ReviewRequestResult, "resolve_review_request": ReviewRequestResult,
            "approve_spec_package": ApprovalResult, "create_projection_plan": ProjectionPlanResult,
            "revise_projection_plan": ProjectionPlanResult, "approve_projection_plan": ApprovalResult,
            "create_status_policy": StatusPolicyResult, "revise_status_policy": StatusPolicyResult, "approve_status_policy": ApprovalResult,
            "start_external_operation": OperationResult, "record_operation_result": OperationResult,
            "reconcile_operation": OperationResult, "submit_remote_snapshot": SnapshotResult,
        }
        for row in connection.execute(select(audit_events).where(audit_events.c.case_id == cid).order_by(audit_events.c.case_sequence)).mappings():
            # Workshop Protocol 1.0.0 commands use their own generated receipt
            # union and replay ledger.  They still share this canonical audit
            # sequence, but must not be coerced into the legacy command-result
            # models during restart hydration.
            if row["command_name"].startswith("workshop."):
                continue
            result = result_models[row["command_name"]].model_validate_json(row["result"])
            case.command_results[UUID(row["command_id"])] = (row["command_fingerprint"], result)
            if isinstance(result, OperationResult) and result.operation_id in case.operations:
                case.operations[result.operation_id].last_result = result

        # Intents are deterministic derivatives; reconstruct them from current approved state.
        if case.policy and case.policy.current.state == PolicyState.APPROVED.value:
            from .service import WorkflowService
            helper = object.__new__(WorkflowService)
            helper.clock = type("HydrationClock", (), {"now": lambda _: case.created_at})()
            helper._derive_content_intents(case)

    def save_case_and_audit(self, case: CaseState, *, event_id: UUID, command_name: str, command_id: UUID, fingerprint: str, actor: Any, occurred_at: datetime, result: Any) -> None:
        result_json = result.model_dump(mode="json", exclude_none=False)
        target_kinds = {
            "create_case": "CASE", "add_participant": "PARTICIPANT", "grant_delegation": "DELEGATION", "revoke_delegation": "DELEGATION",
            "register_source_artifact": "SOURCE_ARTIFACT", "record_ambiguity_finding": "AMBIGUITY_FINDING", "resolve_ambiguity_finding": "AMBIGUITY_FINDING",
            "create_spec_package": "SPEC_PACKAGE", "revise_spec_package": "SPEC_PACKAGE", "mark_spec_package_ready": "SPEC_PACKAGE",
            "mark_spec_package_item_ready": "SPEC_PACKAGE", "approve_spec_package_item": "APPROVAL",
            "create_review_request": "SPEC_PACKAGE", "resolve_review_request": "SPEC_PACKAGE",
            "create_projection_plan": "PROJECTION_PLAN", "revise_projection_plan": "PROJECTION_PLAN",
            "create_status_policy": "STATUS_POLICY", "revise_status_policy": "STATUS_POLICY",
            "approve_spec_package": "APPROVAL", "approve_projection_plan": "APPROVAL", "approve_status_policy": "APPROVAL",
            "start_external_operation": "EXTERNAL_OPERATION", "record_operation_result": "EXTERNAL_OPERATION", "reconcile_operation": "EXTERNAL_OPERATION",
            "submit_remote_snapshot": "REMOTE_SNAPSHOT",
        }
        if command_name == "create_case":
            target = result_json["case_id"]
        elif command_name == "register_source_artifact":
            target = result_json["identity"]["artifact_id"]
        elif "binding" in result_json:
            target = result_json["binding"]["artifact_id"]
        elif "item_binding" in result_json:
            target = result_json["item_binding"]["item_id"]
        elif "review_request" in result_json:
            target = result_json["review_request"]["review_request_id"]
        elif "artifact_binding" in result_json and "approval_id" not in result_json:
            target = result_json["artifact_binding"]["artifact_id"]
        else:
            target = next((value for key, value in result_json.items() if key.endswith("_id") and key not in {"case_id", "command_id", "intent_id"}), str(case.id))
        binding = result_json.get("binding") or result_json.get("artifact_binding")
        item_binding = result_json.get("item_binding") or (result_json.get("review_request") or {}).get("item_binding")
        audit_metadata = {
            "target_kind": target_kinds[command_name],
            "target_version": binding.get("version") if binding else item_binding.get("item_version") if item_binding else None,
            "target_hash": binding.get("semantic_hash") if binding else item_binding.get("item_hash") if item_binding else None,
            "operation_attempt": result_json.get("attempt"),
            "finding_categories": sorted({value["category"].value for value in case.metadata.get("findings", {}).values() if value.get("active")}),
        }
        with self.engine.begin() as connection:
            existing = connection.execute(select(cases.c.id).where(cases.c.id == str(case.id))).scalar_one_or_none()
            values = {"revision": case.revision, "pm_actor_id": str(case.pm_actor_id), "dev_lead_actor_id": str(case.dev_lead_actor_id), "created_at": case.created_at}
            if existing is None: connection.execute(insert(cases).values(id=str(case.id), **values))
            else: connection.execute(update(cases).where(cases.c.id == str(case.id)).values(**values))
            for participant in case.participants:
                present = connection.execute(select(case_participants.c.actor_id).where(case_participants.c.case_id == str(case.id), case_participants.c.actor_id == str(participant))).scalar_one_or_none()
                if present is None: connection.execute(insert(case_participants).values(case_id=str(case.id), actor_id=str(participant), created_at=case.created_at))
            self._save_domain(connection, case)
            connection.execute(insert(audit_events).values(event_id=str(event_id), case_id=str(case.id), case_sequence=case.revision, command_id=str(command_id), command_name=command_name, command_fingerprint=fingerprint, actor=str(actor), occurred_at=occurred_at, target_ids=canonical_json([target]).decode(), before_case_revision=case.revision - 1, after_case_revision=case.revision, metadata=canonical_json(audit_metadata).decode(), result=canonical_json(result_json).decode()))

    @staticmethod
    def _replace(connection, table: Table, **values: Any) -> None:
        statement = sqlite_insert(table).values(**values)
        primary_keys = [column.name for column in table.primary_key.columns]
        updates = {key: statement.excluded[key] for key in values if key not in primary_keys}
        connection.execute(statement.on_conflict_do_update(index_elements=primary_keys, set_=updates))

    @staticmethod
    def _insert_once(connection, table: Table, **values: Any) -> None:
        primary_keys = [column.name for column in table.primary_key.columns]
        criteria = [table.c[key] == values[key] for key in primary_keys]
        if connection.execute(select(*[table.c[key] for key in primary_keys]).where(*criteria)).first() is None:
            connection.execute(insert(table).values(**values))

    @staticmethod
    def _insert_version_or_update_state(connection, table: Table, **values: Any) -> None:
        primary_keys = [column.name for column in table.primary_key.columns]
        criteria = [table.c[key] == values[key] for key in primary_keys]
        existing = connection.execute(select(table).where(*criteria)).mappings().one_or_none()
        if existing is None:
            connection.execute(insert(table).values(**values))
        else:
            mutable = {key: values[key] for key in ("state", "item_governance") if key in values and existing[key] != values[key]}
            if mutable: connection.execute(update(table).where(*criteria).values(**mutable))

    def _save_domain(self, connection, case: CaseState) -> None:
        cid = str(case.id)
        dump = lambda value: canonical_json(value).decode("utf-8")
        for item in case.delegations.values():
            self._replace(connection, delegations, id=str(item.id), case_id=cid, delegator_id=str(item.delegator_id), delegate_id=str(item.delegate_id), domain=item.domain.value, command_names=dump(sorted(item.command_names)), artifact_kind=item.artifact_kind, artifact_id=str(item.artifact_id) if item.artifact_id else None, valid_from=item.valid_from, valid_until=item.valid_until, later_review_required=item.later_review_required, revoked_at=item.revoked_at, created_at=item.created_at)
        for item in case.sources.values():
            self._replace(connection, source_artifacts, artifact_id=str(item.artifact_id), version=item.version, case_id=cid, type=item.type.value, media_type=item.media_type, canonical_locator=item.canonical_locator, content_hash=item.content_hash, registered_at=item.registered_at)
        for item in case.ambiguities.values():
            self._replace(connection, ambiguity_findings, id=str(item.id), case_id=cid, category=item.category, domain=item.domain.value, severity=item.severity, evidence_refs=dump([ref.model_dump(mode="python") for ref in item.evidence_refs]), clarification_question=item.clarification_question, status=item.status.value, resolutions=dump([record.model_dump(mode="python") for record in item.resolutions]), item_binding=dump(item.item_binding.model_dump(mode="python")) if item.item_binding else None, created_at=item.created_at, resolved_at=item.resolved_at)
        if case.package:
            governance = dump({
                "marked_ready_item_bindings": [
                    state.binding.model_dump(mode="python")
                    for _, history in sorted(case.package_items.items(), key=lambda pair: pair[0].bytes)
                    for state in history if state.marked_ready
                ],
                "review_requests": [
                    request.model_dump(mode="python")
                    for request in sorted(case.review_requests.values(), key=lambda value: value.review_request_id.bytes)
                ],
            })
            self._replace(connection, spec_packages, id=str(case.package.artifact_id), case_id=cid, current_version=case.package.current.version, created_at=case.created_at)
            for version in case.package.versions:
                item_rows = [item.model_dump(mode="python") for item in version.payload.items] if isinstance(version.payload, SpecPackagePayloadV2) else None
                self._insert_version_or_update_state(connection, spec_package_versions, package_id=str(case.package.artifact_id), version=version.version, case_id=cid, semantic_hash=version.semantic_hash, content_schema_version=version.content_schema_version, hash_schema_version=version.hash_schema_version, state=version.state, items=dump(item_rows) if item_rows is not None else None, item_governance=governance, created_at=case.created_at)
                for unit in version.payload.requirements:
                    self._insert_once(connection, spec_requirements, package_id=str(case.package.artifact_id), package_version=version.version, unit_id=str(unit.unit_id), case_id=cid, statement=unit.statement, domain=unit.domain.value, delivery_required=unit.delivery_required, source_refs=dump([ref.model_dump(mode="python") for ref in unit.source_refs]))
                for unit in version.payload.technical_decisions:
                    self._insert_once(connection, technical_decisions, package_id=str(case.package.artifact_id), package_version=version.version, unit_id=str(unit.unit_id), case_id=cid, statement=unit.statement, domain=unit.domain.value, delivery_required=unit.delivery_required, provisional=unit.provisional, provisional_delegation_id=str(unit.provisional_delegation_id) if unit.provisional_delegation_id else None, source_refs=dump([ref.model_dump(mode="python") for ref in unit.source_refs]))
                for check in version.payload.acceptance_checks:
                    self._insert_once(connection, acceptance_checks, package_id=str(case.package.artifact_id), package_version=version.version, check_id=str(check.check_id), case_id=cid, statement=check.statement, domain=check.domain.value, related_unit_ids=dump(check.related_unit_ids), source_refs=dump([ref.model_dump(mode="python") for ref in check.source_refs]))
        for target, root in case.plans.items():
            self._replace(connection, projection_plans, id=str(root.artifact_id), case_id=cid, target=target.value, current_version=root.current.version, created_at=case.created_at)
            for version in root.versions:
                payload = version.payload; jira = payload.jira_plan_binding
                tombstones = case.metadata.get("tombstones", {}).get(
                    (root.artifact_id, version.version), {}
                )
                serialized_tombstones = [
                    {
                        "item": entry["item"].model_dump(mode="python"),
                        "prior_plan_version": entry["prior_plan_version"],
                        "prior_plan_hash": entry["prior_plan_hash"],
                    }
                    for _, entry in sorted(tombstones.items(), key=lambda pair: pair[0].bytes)
                ]
                plan_item_bindings = payload.source_item_bindings if isinstance(payload, ProjectionPlanPayloadV2) else None
                self._insert_version_or_update_state(connection, projection_plan_versions, plan_id=str(root.artifact_id), version=version.version, case_id=cid, target=target.value, project_key=payload.project_key, semantic_hash=version.semantic_hash, content_schema_version=version.content_schema_version, hash_schema_version=version.hash_schema_version, package_id=str(payload.package_binding.artifact_id), package_version=payload.package_binding.version, package_hash=payload.package_binding.semantic_hash, jira_plan_id=str(jira.artifact_id) if jira else None, jira_plan_version=jira.version if jira else None, jira_plan_hash=jira.semantic_hash if jira else None, source_item_bindings=dump([value.model_dump(mode="python") for value in plan_item_bindings]) if plan_item_bindings is not None else None, retire_tombstones=dump(serialized_tombstones), state=version.state, created_at=case.created_at)
                hashes = case.metadata.get("item_hashes", {}).get((root.artifact_id, version.version), {})
                remaining = {item.item_id: item for item in payload.items}
                ordered_items = []
                emitted: set[UUID] = set()
                while remaining:
                    ready = sorted(
                        (
                            item
                            for item in remaining.values()
                            if item.parent_item_id is None or item.parent_item_id in emitted
                        ),
                        key=lambda item: item.item_id.bytes,
                    )
                    if not ready:
                        # Domain validation rejects hierarchy cycles; this keeps storage
                        # deterministic if corrupted state reaches the persistence boundary.
                        ready = sorted(remaining.values(), key=lambda item: item.item_id.bytes)
                    for plan_item in ready:
                        ordered_items.append(plan_item)
                        emitted.add(plan_item.item_id)
                        remaining.pop(plan_item.item_id)
                for plan_item in ordered_items:
                    source_item_bindings = plan_item.source_item_bindings if isinstance(plan_item, ProjectionItemV2) else None
                    self._insert_once(connection, projection_items, plan_id=str(root.artifact_id), plan_version=version.version, item_id=str(plan_item.item_id), case_id=cid, generation_key=plan_item.body.generation_key, kind=plan_item.kind.value, domain=plan_item.domain.value, title=plan_item.title, body=dump(plan_item.body.model_dump(mode="python")), parent_item_id=str(plan_item.parent_item_id) if plan_item.parent_item_id else None, implementation_required=plan_item.implementation_required, repository=plan_item.repository, primary_jira_item_id=str(plan_item.primary_jira_item_id) if plan_item.primary_jira_item_id else None, source_item_bindings=dump([value.model_dump(mode="python") for value in source_item_bindings]) if source_item_bindings is not None else None, item_semantic_hash=hashes.get(plan_item.item_id, "0" * 64))
                for plan_item in ordered_items:
                    for source in plan_item.source_unit_ids: self._insert_once(connection, projection_item_sources, plan_id=str(root.artifact_id), plan_version=version.version, item_id=str(plan_item.item_id), source_unit_id=str(source), case_id=cid)
                    for dependency in plan_item.dependency_item_ids: self._insert_once(connection, projection_dependencies, plan_id=str(root.artifact_id), plan_version=version.version, item_id=str(plan_item.item_id), depends_on_item_id=str(dependency), case_id=cid)
        if case.policy:
            self._replace(connection, status_policies, id=str(case.policy.artifact_id), case_id=cid, current_version=case.policy.current.version, created_at=case.created_at)
            for version in case.policy.versions:
                self._insert_version_or_update_state(connection, status_policy_versions, policy_id=str(case.policy.artifact_id), version=version.version, case_id=cid, semantic_hash=version.semantic_hash, content_schema_version=version.content_schema_version, hash_schema_version=version.hash_schema_version, state=version.state, mappings=dump([item.model_dump(mode="python") for item in version.payload.mappings]), rules=dump([item.model_dump(mode="python") for item in version.payload.rules]), created_at=case.created_at)
        for item in case.approvals:
            self._replace(connection, approvals, id=str(item.id), case_id=cid, artifact_kind=item.artifact_kind, artifact_id=str(item.artifact_id), artifact_version=item.artifact_version, artifact_hash=item.artifact_hash, scope=item.scope.value, actor_id=str(item.actor_id), delegation_id=str(item.delegation_id) if item.delegation_id else None, later_review_required=item.later_review_required, approved_at=item.approved_at)
        for item in case.operations.values():
            intent = item.intent; policy = intent.status_policy_binding
            self._replace(connection, external_operations, id=str(item.id), intent_id=str(intent.intent_id), case_id=cid, system=intent.system.value, plan_id=str(intent.plan_binding.artifact_id), plan_version=intent.plan_binding.version, status_policy_id=str(policy.artifact_id), status_policy_version=policy.version, status_policy_hash=policy.semantic_hash, item_ref=dump(intent.item_ref), action=intent.action.value, idempotency_key=item.idempotency_key, request_owned_content_hash=intent.request_owned_content_hash, fingerprint=intent.fingerprint, target_normalized_status=intent.target_normalized_status.value if intent.target_normalized_status else None, contributing_rule_ids=dump(intent.contributing_rule_ids), status=item.status.value, current_attempt=item.attempt, confirmed_result=dump(item.confirmation.model_dump(mode="python")) if item.confirmation else None, confirmed_snapshot_sequence=item.confirmed_snapshot_sequence, created_at=item.created_at, updated_at=item.updated_at)
            self._replace(connection, external_operation_attempts, operation_id=str(item.id), attempt=item.attempt, case_id=cid, request=dump(intent.request), expected_remote_revision=intent.expected, status=item.status.value, failure_code=item.failure_code, result=dump(item.last_result.model_dump(mode="python")) if item.last_result and item.status != OperationStatus.PENDING else None, started_at=item.attempt_started_at or item.created_at, completed_at=item.updated_at if item.status.value != "PENDING" else None)
        for item in case.bindings.values():
            identity = item.external_identity.model_dump(mode="python"); identity_key = identity.get("key") or identity.get("node_id")
            self._replace(connection, external_bindings, id=str(item.id), case_id=cid, system=item.system.value, projection_plan_id=str(item.plan_id), item_id=str(item.item_id), current_plan_version=item.current_plan_version, generation_key=item.generation_key, external_identity_key=identity_key, external_identity=dump(identity), current_observation_sequence=item.current_observation_sequence, confirmed_at=item.confirmed_at)
            for sequence, snapshot in enumerate(item.snapshots, 1):
                policy = snapshot.status_policy_binding
                normalized = None
                lifecycle = None
                owned_hash = None
                if snapshot.observation_kind == ObservationKind.FOUND:
                    normalized = next(
                        (mapping.normalized_status for mapping in case.policy.current.payload.mappings if mapping.system == snapshot.system and mapping.native_status == snapshot.native_status),
                        JiraStatus.UNKNOWN if snapshot.system == System.JIRA else GitHubStatus.UNKNOWN,
                    )
                    from .service import WorkflowService
                    lifecycle = WorkflowService._lifecycle(snapshot.system, normalized, snapshot.owned_content)
                    owned_hash = sha256(snapshot.owned_content)
                self._replace(connection, remote_snapshots, binding_id=str(item.id), observation_sequence=sequence, observation_kind=snapshot.observation_kind.value, case_id=cid, operation_id=None, status_policy_id=str(policy.artifact_id), status_policy_version=policy.version, status_policy_hash=policy.semantic_hash, remote_revision=snapshot.remote_revision, expected_previous_remote_revision=snapshot.expected_previous_remote_revision, native_status=snapshot.native_status, normalized_status_at_acceptance=normalized.value if normalized else None, lifecycle_at_acceptance=lifecycle.value if lifecycle else None, owned_content=dump(snapshot.owned_content) if snapshot.owned_content else None, owned_content_hash=owned_hash, observed_at=item.confirmed_at)
        for edge in case.metadata.get("trace_edges", {}).values():
            self._replace(
                connection,
                traceability_edges,
                id=str(edge.id),
                case_id=cid,
                edge_type=edge.edge_type,
                from_kind=edge.from_endpoint.kind,
                from_id=str(edge.from_endpoint.id),
                from_version=edge.from_endpoint.version,
                to_kind=edge.to_endpoint.kind,
                to_id=str(edge.to_endpoint.id),
                to_version=edge.to_endpoint.version,
                created_at=case.created_at,
            )
        for finding_id, item in case.metadata.get("findings", {}).items():
            self._replace(connection, drift_findings, id=str(finding_id), case_id=cid, category=item["category"].value, affected_binding=dump(item.get("affected", {})), expected_value=dump(item["expected"]), observed_value=dump(item["observed"]), active=item["active"], created_at=item["created_at"], resolved_at=item.get("resolved_at"))

    def list_audit(self, case_id: UUID) -> list[AuditEvent]:
        with self.engine.connect() as connection:
            rows = connection.execute(select(audit_events).where(audit_events.c.case_id == str(case_id)).order_by(audit_events.c.case_sequence)).mappings().all()
        return [AuditEvent(event_id=UUID(row["event_id"]), case_id=case_id, case_sequence=row["case_sequence"], command_id=UUID(row["command_id"]), command_name=row["command_name"], command_fingerprint=row["command_fingerprint"], actor=row["actor"] if row["actor"] == "SYSTEM" else UUID(row["actor"]), occurred_at=row["occurred_at"], target_ids=[UUID(value) for value in json.loads(row["target_ids"])], before_case_revision=row["before_case_revision"], after_case_revision=row["after_case_revision"], metadata=json.loads(row["metadata"]), result=json.loads(row["result"])) for row in rows]

    def list_operation_attempts(self, case_id: UUID) -> list[OperationAttemptRecord]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(
                    external_operation_attempts,
                    external_operations.c.intent_id,
                    external_operations.c.system,
                    external_operations.c.action,
                    external_operations.c.idempotency_key,
                )
                .join(
                    external_operations,
                    (external_operations.c.id == external_operation_attempts.c.operation_id)
                    & (external_operations.c.case_id == external_operation_attempts.c.case_id),
                )
                .where(external_operation_attempts.c.case_id == str(case_id))
                .order_by(external_operation_attempts.c.operation_id, external_operation_attempts.c.attempt)
            ).mappings().all()
        return [
            OperationAttemptRecord(
                operation_id=UUID(row["operation_id"]),
                attempt=row["attempt"],
                intent_id=UUID(row["intent_id"]),
                system=System(row["system"]),
                action=Action(row["action"]),
                idempotency_key=row["idempotency_key"],
                request=json.loads(row["request"]),
                expected_remote_revision=row["expected_remote_revision"],
                status=OperationStatus(row["status"]),
                failure_code=row["failure_code"],
                result=OperationResult.model_validate_json(row["result"]) if row["result"] else None,
                started_at=row["started_at"],
                completed_at=row["completed_at"],
            )
            for row in rows
        ]
