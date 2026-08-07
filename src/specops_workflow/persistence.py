from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import (
    Boolean, CheckConstraint, Column, ForeignKey, Index, Integer, MetaData, String, Table, Text,
    UniqueConstraint, create_engine, event, insert, select, update,
)
from sqlalchemy.engine import Engine
from sqlalchemy.types import TypeDecorator
from pydantic import TypeAdapter

from .artifacts import ArtifactRoot, ArtifactVersion
from .canonical import canonical_json
from .enums import *  # noqa: F403
from .models import *  # noqa: F403
from .state import AmbiguityState, ApprovalState, BindingState, CaseState, DelegationState


class UTCText(TypeDecorator):
    impl = Text
    cache_ok = True
    def process_bind_param(self, value: datetime | None, dialect):
        if value is None: return None
        if value.tzinfo is None: raise ValueError("UTCText rejects naive datetime")
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    def process_result_value(self, value: str | None, dialect):
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc) if value else None


UUIDText = String(36); HashText = String(64); JSONText = Text; EnumText = String(64)
metadata = MetaData()


def owned(name: str, *columns: Column, constraints: tuple[Any, ...] = ()) -> Table:
    return Table(name, metadata, *columns, *constraints)


cases = owned("cases", Column("id", UUIDText, primary_key=True), Column("revision", Integer, nullable=False), Column("pm_actor_id", UUIDText, nullable=False), Column("dev_lead_actor_id", UUIDText, nullable=False), Column("created_at", UTCText(), nullable=False), CheckConstraint("pm_actor_id <> dev_lead_actor_id"), UniqueConstraint("id", "revision"))
case_participants = owned("case_participants", Column("case_id", UUIDText, ForeignKey("cases.id", ondelete="RESTRICT"), primary_key=True), Column("actor_id", UUIDText, primary_key=True), Column("created_at", UTCText(), nullable=False))
delegations = owned("delegations", Column("id", UUIDText, primary_key=True), Column("case_id", UUIDText, ForeignKey("cases.id", ondelete="RESTRICT"), nullable=False), Column("delegator_id", UUIDText, nullable=False), Column("delegate_id", UUIDText, nullable=False), Column("domain", EnumText, nullable=False), Column("command_names", JSONText, nullable=False), Column("artifact_kind", EnumText), Column("artifact_id", UUIDText), Column("valid_from", UTCText(), nullable=False), Column("valid_until", UTCText(), nullable=False), Column("later_review_required", Boolean, nullable=False), Column("revoked_at", UTCText()), Column("created_at", UTCText(), nullable=False), UniqueConstraint("id", "case_id"), CheckConstraint("valid_from <= valid_until"))
source_artifacts = owned("source_artifacts", Column("artifact_id", UUIDText, primary_key=True), Column("version", Integer, primary_key=True), Column("case_id", UUIDText, ForeignKey("cases.id", ondelete="RESTRICT"), nullable=False), Column("type", EnumText, nullable=False), Column("media_type", String(128), nullable=False), Column("canonical_locator", Text, nullable=False), Column("content_hash", HashText, nullable=False), Column("registered_at", UTCText(), nullable=False), UniqueConstraint("artifact_id", "version", "case_id"))
ambiguity_findings = owned("ambiguity_findings", Column("id", UUIDText, primary_key=True), Column("case_id", UUIDText, ForeignKey("cases.id", ondelete="RESTRICT"), nullable=False), Column("category", EnumText, nullable=False), Column("domain", EnumText, nullable=False), Column("severity", EnumText, nullable=False), Column("evidence_refs", JSONText, nullable=False), Column("clarification_question", Text, nullable=False), Column("status", EnumText, nullable=False), Column("resolutions", JSONText, nullable=False), Column("created_at", UTCText(), nullable=False), Column("resolved_at", UTCText()), UniqueConstraint("id", "case_id"))
spec_packages = owned("spec_packages", Column("id", UUIDText, primary_key=True), Column("case_id", UUIDText, ForeignKey("cases.id", ondelete="RESTRICT"), nullable=False, unique=True), Column("current_version", Integer, nullable=False), Column("created_at", UTCText(), nullable=False), UniqueConstraint("id", "case_id"))
spec_package_versions = owned("spec_package_versions", Column("package_id", UUIDText, primary_key=True), Column("version", Integer, primary_key=True), Column("case_id", UUIDText, ForeignKey("cases.id", ondelete="RESTRICT"), nullable=False), Column("semantic_hash", HashText, nullable=False), Column("content_schema_version", Integer, nullable=False), Column("hash_schema_version", Integer, nullable=False), Column("state", EnumText, nullable=False), Column("created_at", UTCText(), nullable=False), UniqueConstraint("package_id", "version", "case_id"))
spec_requirements = owned("spec_requirements", Column("package_id", UUIDText, primary_key=True), Column("package_version", Integer, primary_key=True), Column("unit_id", UUIDText, primary_key=True), Column("case_id", UUIDText, nullable=False), Column("statement", Text, nullable=False), Column("domain", EnumText, nullable=False), Column("delivery_required", Boolean, nullable=False), Column("source_refs", JSONText, nullable=False))
technical_decisions = owned("technical_decisions", Column("package_id", UUIDText, primary_key=True), Column("package_version", Integer, primary_key=True), Column("unit_id", UUIDText, primary_key=True), Column("case_id", UUIDText, nullable=False), Column("statement", Text, nullable=False), Column("domain", EnumText, nullable=False), Column("delivery_required", Boolean, nullable=False), Column("provisional", Boolean, nullable=False), Column("provisional_delegation_id", UUIDText), Column("source_refs", JSONText, nullable=False))
acceptance_checks = owned("acceptance_checks", Column("package_id", UUIDText, primary_key=True), Column("package_version", Integer, primary_key=True), Column("check_id", UUIDText, primary_key=True), Column("case_id", UUIDText, nullable=False), Column("statement", Text, nullable=False), Column("domain", EnumText, nullable=False), Column("related_unit_ids", JSONText, nullable=False), Column("source_refs", JSONText, nullable=False))
projection_plans = owned("projection_plans", Column("id", UUIDText, primary_key=True), Column("case_id", UUIDText, ForeignKey("cases.id", ondelete="RESTRICT"), nullable=False), Column("target", EnumText, nullable=False), Column("current_version", Integer, nullable=False), Column("created_at", UTCText(), nullable=False), UniqueConstraint("case_id", "target"), UniqueConstraint("id", "case_id"))
projection_plan_versions = owned("projection_plan_versions", Column("plan_id", UUIDText, primary_key=True), Column("version", Integer, primary_key=True), Column("case_id", UUIDText, nullable=False), Column("target", EnumText, nullable=False), Column("project_key", String(16)), Column("semantic_hash", HashText, nullable=False), Column("content_schema_version", Integer, nullable=False), Column("hash_schema_version", Integer, nullable=False), Column("package_id", UUIDText, nullable=False), Column("package_version", Integer, nullable=False), Column("package_hash", HashText, nullable=False), Column("jira_plan_id", UUIDText), Column("jira_plan_version", Integer), Column("jira_plan_hash", HashText), Column("retire_tombstones", JSONText, nullable=False), Column("state", EnumText, nullable=False), Column("created_at", UTCText(), nullable=False))
projection_items = owned("projection_items", Column("plan_id", UUIDText, primary_key=True), Column("plan_version", Integer, primary_key=True), Column("item_id", UUIDText, primary_key=True), Column("case_id", UUIDText, nullable=False), Column("generation_key", Text, nullable=False), Column("kind", EnumText, nullable=False), Column("domain", EnumText, nullable=False), Column("title", Text, nullable=False), Column("body", JSONText, nullable=False), Column("parent_item_id", UUIDText), Column("implementation_required", Boolean, nullable=False), Column("repository", Text), Column("primary_jira_item_id", UUIDText), Column("item_semantic_hash", HashText, nullable=False), UniqueConstraint("plan_id", "plan_version", "generation_key"))
projection_item_sources = owned("projection_item_sources", Column("plan_id", UUIDText, primary_key=True), Column("plan_version", Integer, primary_key=True), Column("item_id", UUIDText, primary_key=True), Column("source_unit_id", UUIDText, primary_key=True), Column("case_id", UUIDText, nullable=False))
projection_dependencies = owned("projection_dependencies", Column("plan_id", UUIDText, primary_key=True), Column("plan_version", Integer, primary_key=True), Column("item_id", UUIDText, primary_key=True), Column("depends_on_item_id", UUIDText, primary_key=True), Column("case_id", UUIDText, nullable=False), CheckConstraint("item_id <> depends_on_item_id"))
status_policies = owned("status_policies", Column("id", UUIDText, primary_key=True), Column("case_id", UUIDText, ForeignKey("cases.id", ondelete="RESTRICT"), nullable=False, unique=True), Column("current_version", Integer, nullable=False), Column("created_at", UTCText(), nullable=False), UniqueConstraint("id", "case_id"))
status_policy_versions = owned("status_policy_versions", Column("policy_id", UUIDText, primary_key=True), Column("version", Integer, primary_key=True), Column("case_id", UUIDText, nullable=False), Column("semantic_hash", HashText, nullable=False), Column("content_schema_version", Integer, nullable=False), Column("hash_schema_version", Integer, nullable=False), Column("state", EnumText, nullable=False), Column("mappings", JSONText, nullable=False), Column("rules", JSONText, nullable=False), Column("created_at", UTCText(), nullable=False))
approvals = owned("approvals", Column("id", UUIDText, primary_key=True), Column("case_id", UUIDText, ForeignKey("cases.id", ondelete="RESTRICT"), nullable=False), Column("artifact_kind", EnumText, nullable=False), Column("artifact_id", UUIDText, nullable=False), Column("artifact_version", Integer, nullable=False), Column("artifact_hash", HashText, nullable=False), Column("scope", EnumText, nullable=False), Column("actor_id", UUIDText, nullable=False), Column("delegation_id", UUIDText), Column("later_review_required", Boolean, nullable=False), Column("approved_at", UTCText(), nullable=False), UniqueConstraint("case_id", "artifact_kind", "artifact_id", "artifact_version", "artifact_hash", "scope", "actor_id"))
external_operations = owned("external_operations", Column("id", UUIDText, primary_key=True), Column("intent_id", UUIDText, nullable=False), Column("case_id", UUIDText, nullable=False), Column("system", EnumText, nullable=False), Column("plan_id", UUIDText, nullable=False), Column("plan_version", Integer, nullable=False), Column("status_policy_id", UUIDText, nullable=False), Column("status_policy_version", Integer, nullable=False), Column("status_policy_hash", HashText, nullable=False), Column("item_ref", JSONText, nullable=False), Column("action", EnumText, nullable=False), Column("idempotency_key", Text, nullable=False), Column("request_owned_content_hash", HashText, nullable=False), Column("fingerprint", HashText, nullable=False), Column("target_normalized_status", EnumText), Column("contributing_rule_ids", JSONText, nullable=False), Column("status", EnumText, nullable=False), Column("current_attempt", Integer, nullable=False), Column("confirmed_result", JSONText), Column("confirmed_snapshot_sequence", Integer), Column("created_at", UTCText(), nullable=False), Column("updated_at", UTCText(), nullable=False), UniqueConstraint("case_id", "idempotency_key"), UniqueConstraint("case_id", "intent_id"), UniqueConstraint("id", "case_id"))
external_operation_attempts = owned("external_operation_attempts", Column("operation_id", UUIDText, primary_key=True), Column("attempt", Integer, primary_key=True), Column("case_id", UUIDText, nullable=False), Column("request", JSONText, nullable=False), Column("expected_remote_revision", Text), Column("status", EnumText, nullable=False), Column("failure_code", Text), Column("result", JSONText), Column("started_at", UTCText(), nullable=False), Column("completed_at", UTCText()))
external_bindings = owned("external_bindings", Column("id", UUIDText, primary_key=True), Column("case_id", UUIDText, nullable=False), Column("system", EnumText, nullable=False), Column("projection_plan_id", UUIDText, nullable=False), Column("item_id", UUIDText, nullable=False), Column("current_plan_version", Integer, nullable=False), Column("generation_key", Text, nullable=False), Column("external_identity_key", Text, nullable=False), Column("external_identity", JSONText, nullable=False), Column("current_observation_sequence", Integer, nullable=False), Column("confirmed_at", UTCText(), nullable=False), UniqueConstraint("system", "external_identity_key"), UniqueConstraint("case_id", "system", "projection_plan_id", "item_id"), UniqueConstraint("id", "case_id"))
remote_snapshots = owned("remote_snapshots", Column("binding_id", UUIDText, primary_key=True), Column("observation_sequence", Integer, primary_key=True), Column("observation_kind", EnumText, nullable=False), Column("case_id", UUIDText, nullable=False), Column("operation_id", UUIDText), Column("status_policy_id", UUIDText, nullable=False), Column("status_policy_version", Integer, nullable=False), Column("status_policy_hash", HashText, nullable=False), Column("remote_revision", Text), Column("expected_previous_remote_revision", Text), Column("native_status", Text), Column("normalized_status_at_acceptance", EnumText), Column("lifecycle_at_acceptance", EnumText), Column("owned_content", JSONText), Column("owned_content_hash", HashText), Column("observed_at", UTCText(), nullable=False), UniqueConstraint("binding_id", "remote_revision"))
traceability_edges = owned("traceability_edges", Column("id", UUIDText, primary_key=True), Column("case_id", UUIDText, nullable=False), Column("edge_type", EnumText, nullable=False), Column("from_kind", EnumText, nullable=False), Column("from_id", UUIDText, nullable=False), Column("from_version", Integer), Column("to_kind", EnumText, nullable=False), Column("to_id", UUIDText, nullable=False), Column("to_version", Integer), Column("created_at", UTCText(), nullable=False), UniqueConstraint("case_id", "edge_type", "from_kind", "from_id", "from_version", "to_kind", "to_id", "to_version"))
drift_findings = owned("drift_findings", Column("id", UUIDText, primary_key=True), Column("case_id", UUIDText, nullable=False), Column("category", EnumText, nullable=False), Column("affected_binding", JSONText, nullable=False), Column("expected_value", JSONText, nullable=False), Column("observed_value", JSONText, nullable=False), Column("active", Boolean, nullable=False), Column("created_at", UTCText(), nullable=False), Column("resolved_at", UTCText()))
audit_events = owned("audit_events", Column("event_id", UUIDText, primary_key=True), Column("case_id", UUIDText, ForeignKey("cases.id", ondelete="RESTRICT"), nullable=False), Column("case_sequence", Integer, nullable=False), Column("command_id", UUIDText, nullable=False), Column("command_name", Text, nullable=False), Column("command_fingerprint", HashText, nullable=False), Column("actor", Text, nullable=False), Column("occurred_at", UTCText(), nullable=False), Column("target_ids", JSONText, nullable=False), Column("before_case_revision", Integer, nullable=False), Column("after_case_revision", Integer, nullable=False), Column("metadata", JSONText, nullable=False), Column("result", JSONText, nullable=False), UniqueConstraint("case_id", "case_sequence"), UniqueConstraint("case_id", "command_id"), CheckConstraint("case_sequence = after_case_revision"))

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
    def __init__(self, database_url: str) -> None: self.engine = engine_for(database_url)

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
        cid = str(case.id); refs = TypeAdapter(list[SourceRef]); resolutions = TypeAdapter(list[ResolutionRecord])
        for row in connection.execute(select(delegations).where(delegations.c.case_id == cid)).mappings():
            item = DelegationState(UUID(row["id"]), UUID(row["delegator_id"]), UUID(row["delegate_id"]), Domain(row["domain"]), frozenset(json.loads(row["command_names"])), row["artifact_kind"], UUID(row["artifact_id"]) if row["artifact_id"] else None, row["valid_from"], row["valid_until"], bool(row["later_review_required"]), row["created_at"], row["revoked_at"]); case.delegations[item.id] = item
        for row in connection.execute(select(source_artifacts).where(source_artifacts.c.case_id == cid)).mappings():
            item = SourceArtifactIdentity(artifact_id=UUID(row["artifact_id"]), case_id=case.id, type=SourceArtifactType(row["type"]), version=row["version"], media_type=row["media_type"], canonical_locator=row["canonical_locator"], content_hash=row["content_hash"], registered_at=row["registered_at"]); case.sources[(item.artifact_id, item.version)] = item
        for row in connection.execute(select(ambiguity_findings).where(ambiguity_findings.c.case_id == cid)).mappings():
            item = AmbiguityState(UUID(row["id"]), row["category"], Domain(row["domain"]), row["severity"], refs.validate_json(row["evidence_refs"]), row["clarification_question"], row["created_at"], FindingStatus(row["status"]), resolutions.validate_json(row["resolutions"]), row["resolved_at"]); case.ambiguities[item.id] = item
        package = connection.execute(select(spec_packages).where(spec_packages.c.case_id == cid)).mappings().one_or_none()
        if package:
            root = ArtifactRoot(ArtifactKind.SPEC_PACKAGE, UUID(package["id"]))
            for version in connection.execute(select(spec_package_versions).where(spec_package_versions.c.package_id == package["id"]).order_by(spec_package_versions.c.version)).mappings():
                reqs = [Requirement(unit_id=UUID(r["unit_id"]), statement=r["statement"], domain=Domain(r["domain"]), delivery_required=bool(r["delivery_required"]), source_refs=refs.validate_json(r["source_refs"])) for r in connection.execute(select(spec_requirements).where(spec_requirements.c.package_id == package["id"], spec_requirements.c.package_version == version["version"])).mappings()]
                decisions = [TechnicalDecision(unit_id=UUID(r["unit_id"]), statement=r["statement"], domain=Domain(r["domain"]), delivery_required=bool(r["delivery_required"]), provisional=bool(r["provisional"]), provisional_delegation_id=UUID(r["provisional_delegation_id"]) if r["provisional_delegation_id"] else None, source_refs=refs.validate_json(r["source_refs"])) for r in connection.execute(select(technical_decisions).where(technical_decisions.c.package_id == package["id"], technical_decisions.c.package_version == version["version"])).mappings()]
                checks = [AcceptanceCheck(check_id=UUID(r["check_id"]), statement=r["statement"], domain=Domain(r["domain"]), related_unit_ids=[UUID(value) for value in json.loads(r["related_unit_ids"])], source_refs=refs.validate_json(r["source_refs"])) for r in connection.execute(select(acceptance_checks).where(acceptance_checks.c.package_id == package["id"], acceptance_checks.c.package_version == version["version"])).mappings()]
                root.versions.append(ArtifactVersion(version["version"], version["semantic_hash"], version["content_schema_version"], version["hash_schema_version"], SpecPackagePayload(requirements=reqs, technical_decisions=decisions, acceptance_checks=checks), version["state"]))
            case.package = root
        for row in connection.execute(select(approvals).where(approvals.c.case_id == cid)).mappings():
            case.approvals.append(ApprovalState(UUID(row["id"]), row["artifact_kind"], UUID(row["artifact_id"]), row["artifact_version"], row["artifact_hash"], ApprovalScope(row["scope"]), UUID(row["actor_id"]), UUID(row["delegation_id"]) if row["delegation_id"] else None, None, bool(row["later_review_required"]), row["approved_at"]))
        identity_type = TypeAdapter(JiraIdentity | GitHubIdentity)
        for row in connection.execute(select(external_bindings).where(external_bindings.c.case_id == cid)).mappings():
            identity = identity_type.validate_json(row["external_identity"]); item = BindingState(UUID(row["id"]), case.id, System(row["system"]), UUID(row["projection_plan_id"]), UUID(row["item_id"]), row["generation_key"], identity, row["current_plan_version"], row["current_observation_sequence"], row["confirmed_at"]); case.bindings[item.id] = item; case.metadata.setdefault("external_bindings", {})[item.item_id] = {"binding_id": item.id, "key": identity.key} if isinstance(identity, JiraIdentity) else {"binding_id": item.id, "identity": identity}
        for row in connection.execute(select(drift_findings).where(drift_findings.c.case_id == cid)).mappings():
            case.metadata.setdefault("findings", {})[UUID(row["id"])] = {"category": FindingCategory(row["category"]), "affected": json.loads(row["affected_binding"]), "expected": json.loads(row["expected_value"]), "observed": json.loads(row["observed_value"]), "active": bool(row["active"]), "created_at": row["created_at"], "resolved_at": row["resolved_at"]}

    def save_case_and_audit(self, case: CaseState, *, command_name: str, command_id: UUID, fingerprint: str, actor: Any, occurred_at: datetime, result: Any) -> None:
        result_json = result.model_dump(mode="json", exclude_none=False)
        target = next((value for key, value in result_json.items() if key.endswith("_id") and key not in {"case_id", "command_id"}), case.id)
        with self.engine.begin() as connection:
            existing = connection.execute(select(cases.c.id).where(cases.c.id == str(case.id))).scalar_one_or_none()
            values = {"revision": case.revision, "pm_actor_id": str(case.pm_actor_id), "dev_lead_actor_id": str(case.dev_lead_actor_id), "created_at": case.created_at}
            if existing is None: connection.execute(insert(cases).values(id=str(case.id), **values))
            else: connection.execute(update(cases).where(cases.c.id == str(case.id)).values(**values))
            for participant in case.participants:
                present = connection.execute(select(case_participants.c.actor_id).where(case_participants.c.case_id == str(case.id), case_participants.c.actor_id == str(participant))).scalar_one_or_none()
                if present is None: connection.execute(insert(case_participants).values(case_id=str(case.id), actor_id=str(participant), created_at=case.created_at))
            self._save_domain(connection, case)
            connection.execute(insert(audit_events).values(event_id=str(uuid4()), case_id=str(case.id), case_sequence=case.revision, command_id=str(command_id), command_name=command_name, command_fingerprint=fingerprint, actor=str(actor), occurred_at=occurred_at, target_ids=canonical_json([target]).decode(), before_case_revision=case.revision - 1, after_case_revision=case.revision, metadata="{}", result=canonical_json(result_json).decode()))

    @staticmethod
    def _replace(connection, table: Table, **values: Any) -> None:
        connection.execute(insert(table).prefix_with("OR REPLACE").values(**values))

    def _save_domain(self, connection, case: CaseState) -> None:
        cid = str(case.id)
        dump = lambda value: canonical_json(value).decode("utf-8")
        for item in case.delegations.values():
            self._replace(connection, delegations, id=str(item.id), case_id=cid, delegator_id=str(item.delegator_id), delegate_id=str(item.delegate_id), domain=item.domain.value, command_names=dump(sorted(item.command_names)), artifact_kind=item.artifact_kind, artifact_id=str(item.artifact_id) if item.artifact_id else None, valid_from=item.valid_from, valid_until=item.valid_until, later_review_required=item.later_review_required, revoked_at=item.revoked_at, created_at=item.created_at)
        for item in case.sources.values():
            self._replace(connection, source_artifacts, artifact_id=str(item.artifact_id), version=item.version, case_id=cid, type=item.type.value, media_type=item.media_type, canonical_locator=item.canonical_locator, content_hash=item.content_hash, registered_at=item.registered_at)
        for item in case.ambiguities.values():
            self._replace(connection, ambiguity_findings, id=str(item.id), case_id=cid, category=item.category, domain=item.domain.value, severity=item.severity, evidence_refs=dump([ref.model_dump(mode="python") for ref in item.evidence_refs]), clarification_question=item.clarification_question, status=item.status.value, resolutions=dump([record.model_dump(mode="python") for record in item.resolutions]), created_at=item.created_at, resolved_at=item.resolved_at)
        if case.package:
            self._replace(connection, spec_packages, id=str(case.package.artifact_id), case_id=cid, current_version=case.package.current.version, created_at=case.created_at)
            for version in case.package.versions:
                self._replace(connection, spec_package_versions, package_id=str(case.package.artifact_id), version=version.version, case_id=cid, semantic_hash=version.semantic_hash, content_schema_version=version.content_schema_version, hash_schema_version=version.hash_schema_version, state=version.state, created_at=case.created_at)
                for unit in version.payload.requirements:
                    self._replace(connection, spec_requirements, package_id=str(case.package.artifact_id), package_version=version.version, unit_id=str(unit.unit_id), case_id=cid, statement=unit.statement, domain=unit.domain.value, delivery_required=unit.delivery_required, source_refs=dump([ref.model_dump(mode="python") for ref in unit.source_refs]))
                for unit in version.payload.technical_decisions:
                    self._replace(connection, technical_decisions, package_id=str(case.package.artifact_id), package_version=version.version, unit_id=str(unit.unit_id), case_id=cid, statement=unit.statement, domain=unit.domain.value, delivery_required=unit.delivery_required, provisional=unit.provisional, provisional_delegation_id=str(unit.provisional_delegation_id) if unit.provisional_delegation_id else None, source_refs=dump([ref.model_dump(mode="python") for ref in unit.source_refs]))
                for check in version.payload.acceptance_checks:
                    self._replace(connection, acceptance_checks, package_id=str(case.package.artifact_id), package_version=version.version, check_id=str(check.check_id), case_id=cid, statement=check.statement, domain=check.domain.value, related_unit_ids=dump(check.related_unit_ids), source_refs=dump([ref.model_dump(mode="python") for ref in check.source_refs]))
        for target, root in case.plans.items():
            self._replace(connection, projection_plans, id=str(root.artifact_id), case_id=cid, target=target.value, current_version=root.current.version, created_at=case.created_at)
            for version in root.versions:
                payload = version.payload; jira = payload.jira_plan_binding
                self._replace(connection, projection_plan_versions, plan_id=str(root.artifact_id), version=version.version, case_id=cid, target=target.value, project_key=payload.project_key, semantic_hash=version.semantic_hash, content_schema_version=version.content_schema_version, hash_schema_version=version.hash_schema_version, package_id=str(payload.package_binding.artifact_id), package_version=payload.package_binding.version, package_hash=payload.package_binding.semantic_hash, jira_plan_id=str(jira.artifact_id) if jira else None, jira_plan_version=jira.version if jira else None, jira_plan_hash=jira.semantic_hash if jira else None, retire_tombstones="[]", state=version.state, created_at=case.created_at)
                hashes = case.metadata.get("item_hashes", {}).get((root.artifact_id, version.version), {})
                for plan_item in payload.items:
                    self._replace(connection, projection_items, plan_id=str(root.artifact_id), plan_version=version.version, item_id=str(plan_item.item_id), case_id=cid, generation_key=plan_item.body.generation_key, kind=plan_item.kind.value, domain=plan_item.domain.value, title=plan_item.title, body=dump(plan_item.body.model_dump(mode="python")), parent_item_id=str(plan_item.parent_item_id) if plan_item.parent_item_id else None, implementation_required=plan_item.implementation_required, repository=plan_item.repository, primary_jira_item_id=str(plan_item.primary_jira_item_id) if plan_item.primary_jira_item_id else None, item_semantic_hash=hashes.get(plan_item.item_id, "0" * 64))
                    for source in plan_item.source_unit_ids: self._replace(connection, projection_item_sources, plan_id=str(root.artifact_id), plan_version=version.version, item_id=str(plan_item.item_id), source_unit_id=str(source), case_id=cid)
                    for dependency in plan_item.dependency_item_ids: self._replace(connection, projection_dependencies, plan_id=str(root.artifact_id), plan_version=version.version, item_id=str(plan_item.item_id), depends_on_item_id=str(dependency), case_id=cid)
        if case.policy:
            self._replace(connection, status_policies, id=str(case.policy.artifact_id), case_id=cid, current_version=case.policy.current.version, created_at=case.created_at)
            for version in case.policy.versions:
                self._replace(connection, status_policy_versions, policy_id=str(case.policy.artifact_id), version=version.version, case_id=cid, semantic_hash=version.semantic_hash, content_schema_version=version.content_schema_version, hash_schema_version=version.hash_schema_version, state=version.state, mappings=dump([item.model_dump(mode="python") for item in version.payload.mappings]), rules=dump([item.model_dump(mode="python") for item in version.payload.rules]), created_at=case.created_at)
        for item in case.approvals:
            self._replace(connection, approvals, id=str(item.id), case_id=cid, artifact_kind=item.artifact_kind, artifact_id=str(item.artifact_id), artifact_version=item.artifact_version, artifact_hash=item.artifact_hash, scope=item.scope.value, actor_id=str(item.actor_id), delegation_id=str(item.delegation_id) if item.delegation_id else None, later_review_required=item.later_review_required, approved_at=item.approved_at)
        for item in case.operations.values():
            intent = item.intent; policy = intent.status_policy_binding
            self._replace(connection, external_operations, id=str(item.id), intent_id=str(intent.intent_id), case_id=cid, system=intent.system.value, plan_id=str(intent.plan_binding.artifact_id), plan_version=intent.plan_binding.version, status_policy_id=str(policy.artifact_id), status_policy_version=policy.version, status_policy_hash=policy.semantic_hash, item_ref=dump(intent.item_ref), action=intent.action.value, idempotency_key=item.idempotency_key, request_owned_content_hash=intent.request_owned_content_hash, fingerprint=intent.fingerprint, target_normalized_status=intent.target_normalized_status.value if intent.target_normalized_status else None, contributing_rule_ids=dump(intent.contributing_rule_ids), status=item.status.value, current_attempt=item.attempt, confirmed_result=dump(item.confirmation.model_dump(mode="python")) if item.confirmation else None, confirmed_snapshot_sequence=item.confirmed_snapshot_sequence, created_at=item.created_at, updated_at=item.updated_at)
            self._replace(connection, external_operation_attempts, operation_id=str(item.id), attempt=item.attempt, case_id=cid, request=dump(intent.request), expected_remote_revision=intent.expected, status=item.status.value, failure_code=item.failure_code, result=dump(item.last_result.model_dump(mode="python")) if item.last_result else None, started_at=item.created_at, completed_at=item.updated_at if item.status.value != "PENDING" else None)
        for item in case.bindings.values():
            identity = item.external_identity.model_dump(mode="python"); identity_key = identity.get("key") or identity.get("node_id")
            self._replace(connection, external_bindings, id=str(item.id), case_id=cid, system=item.system.value, projection_plan_id=str(item.plan_id), item_id=str(item.item_id), current_plan_version=item.current_plan_version, generation_key=item.generation_key, external_identity_key=identity_key, external_identity=dump(identity), current_observation_sequence=item.current_observation_sequence, confirmed_at=item.confirmed_at)
            for sequence, snapshot in enumerate(item.snapshots, 1):
                policy = snapshot.status_policy_binding
                self._replace(connection, remote_snapshots, binding_id=str(item.id), observation_sequence=sequence, observation_kind=snapshot.observation_kind.value, case_id=cid, operation_id=None, status_policy_id=str(policy.artifact_id), status_policy_version=policy.version, status_policy_hash=policy.semantic_hash, remote_revision=snapshot.remote_revision, expected_previous_remote_revision=snapshot.expected_previous_remote_revision, native_status=snapshot.native_status, normalized_status_at_acceptance=None, lifecycle_at_acceptance=None, owned_content=dump(snapshot.owned_content) if snapshot.owned_content else None, owned_content_hash=None, observed_at=item.confirmed_at)
        for finding_id, item in case.metadata.get("findings", {}).items():
            self._replace(connection, drift_findings, id=str(finding_id), case_id=cid, category=item["category"].value, affected_binding=dump(item.get("affected", {})), expected_value=dump(item["expected"]), observed_value=dump(item["observed"]), active=item["active"], created_at=item["created_at"], resolved_at=item.get("resolved_at"))

    def list_audit(self, case_id: UUID) -> list[AuditEvent]:
        with self.engine.connect() as connection:
            rows = connection.execute(select(audit_events).where(audit_events.c.case_id == str(case_id)).order_by(audit_events.c.case_sequence)).mappings().all()
        return [AuditEvent(event_id=UUID(row["event_id"]), case_id=case_id, case_sequence=row["case_sequence"], command_id=UUID(row["command_id"]), command_name=row["command_name"], command_fingerprint=row["command_fingerprint"], actor=row["actor"] if row["actor"] == "SYSTEM" else UUID(row["actor"]), occurred_at=row["occurred_at"], target_ids=[UUID(value) for value in json.loads(row["target_ids"])], before_case_revision=row["before_case_revision"], after_case_revision=row["after_case_revision"], metadata=json.loads(row["metadata"]), result=json.loads(row["result"])) for row in rows]
