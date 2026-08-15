"""Foundation-owned Artifact Quality Audit Protocol 1.0.0 reducer."""

from __future__ import annotations

import json
import re
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid5

from sqlalchemy import and_, func, insert, select, update
from sqlalchemy.exc import IntegrityError

from specops_contracts import artifact_quality_v1 as q
from specops_contracts import workshop_v1 as c
from specops_contracts.canonical import canonical_bytes, domain_hash, payload_hash
from specops_workshop.v4.artifact_quality import (
    quality_contract_hash,
    resolve_payload_pointer,
    validate_audit_bundle,
)

from .artifact_projection import draft_governance, validate_exact
from .artifact_reference_graph import validate_artifact_reference_graph
from .artifact_evidence_support import (
    evidence_assessment_disposition,
    materialize_supported_evidence,
)
from .artifact_quality_revision import (
    apply_quality_revision_candidate,
    prepare_quality_revision_request,
    quality_revision_pointer_closure,
)
from .persistence import (
    ARTIFACT_QUALITY_TABLES,
    EVIDENCE_ASSESSMENT_TABLES,
    WORKSHOP_PROTOCOL_TABLES,
    case_participants,
    source_artifacts,
)


_SECRET_PATTERNS = tuple(
    re.compile(value)
    for value in (
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
        r"\bsk-[A-Za-z0-9_-]{20,}\b",
        r"\bAIza[0-9A-Za-z_-]{30,}\b",
        r"\bAKIA[0-9A-Z]{16}\b",
        r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b",
        r"https?://[^\s/:]+:[^\s/@]+@",
    )
)


def _json(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json", exclude_none=False)
    return canonical_bytes(value).decode("utf-8")


def _instant(value) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _source_ids_from_context_json(binding_json: str) -> set[str]:
    """Read stable source identities without upgrading a historical provider contract."""

    value = json.loads(binding_json)
    try:
        return {
            item["source"]["source_id"]
            for item in value["source_set"]["ordered_sources"]
        }
    except (KeyError, TypeError) as exc:
        raise ValueError("stored Analyzer context has no exact source identities") from exc


def _confirmed_spec_payload_from_record(
    artifact_type: str, confirmed_from_json: str | None
) -> dict[str, Any] | None:
    """Recover the exact Spec lineage already bound to a Technical record."""

    if artifact_type != "TECHNICAL_CONTRACT":
        return None
    if not isinstance(confirmed_from_json, str):
        raise ValueError("Technical record has no confirmed Spec lineage")
    try:
        binding = json.loads(confirmed_from_json)
        canonical_payload_json = binding["canonical_payload_json"]
        expected_hash = binding["payload_hash"]
        payload = json.loads(canonical_payload_json)
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Technical record has invalid confirmed Spec lineage") from exc
    if not isinstance(payload, dict) or payload_hash(payload) != expected_hash:
        raise ValueError("Technical record confirmed Spec lineage hash changed")
    return payload


class ArtifactQualityFoundationMixin:
    """Mixed into ``WorkshopFoundationService`` to keep one Foundation state owner."""

    def latest_artifact_record(
        self, case_id: UUID, artifact_type: str
    ) -> dict[str, Any] | None:
        records = WORKSHOP_PROTOCOL_TABLES["workshop_artifact_records"]
        with self.engine.connect() as connection:
            row = connection.execute(
                select(records)
                .where(
                    records.c.case_id == str(case_id),
                    records.c.artifact_type == artifact_type,
                )
                .order_by(records.c.artifact_version.desc())
                .limit(1)
            ).mappings().one_or_none()
        return None if row is None else dict(row)

    def materialize_artifact_evidence_support(
        self,
        case_id: UUID,
        request: q.ArtifactEvidenceSupportRequest,
        candidate: q.ArtifactEvidenceSupportCandidate,
        execution: q.StandaloneEvaluatorExecutionBinding,
    ) -> dict[str, Any]:
        """Commit canonical evidence and retain every exact assessment atomically."""

        from .workshop_protocol import FoundationProtocolError

        records = WORKSHOP_PROTOCOL_TABLES["workshop_artifact_records"]
        plans = WORKSHOP_PROTOCOL_TABLES["workshop_identity_plans"]
        semantic_records = WORKSHOP_PROTOCOL_TABLES["workshop_semantic_records"]
        runs = EVIDENCE_ASSESSMENT_TABLES[
            "workshop_artifact_evidence_assessment_runs"
        ]
        assessment_rows = EVIDENCE_ASSESSMENT_TABLES[
            "workshop_artifact_evidence_assessments"
        ]
        request_json = _json(request)
        candidate_json = _json(candidate)
        execution_json = _json(execution)
        with self.engine.begin() as connection:
            replay = connection.execute(
                select(runs).where(runs.c.request_hash == request.request_hash)
            ).mappings().one_or_none()
            if replay is not None:
                if (
                    replay["case_id"] != str(case_id)
                    or replay["request_json"] != request_json
                    or replay["candidate_json"] != candidate_json
                    or replay["execution_json"] != execution_json
                    or connection.execute(
                        select(func.count()).select_from(assessment_rows).where(
                            assessment_rows.c.request_hash == request.request_hash
                        )
                    ).scalar_one()
                    != len(request.pairs)
                ):
                    raise FoundationProtocolError(
                        c.FoundationRejectionCode.EVIDENCE_BINDING_FAILED
                    )
                replayed_record = connection.execute(
                    select(records).where(
                        records.c.case_id == str(case_id),
                        records.c.artifact_type == "SPEC_PACKAGE",
                        records.c.artifact_id == replay["artifact_id"],
                        records.c.artifact_version == replay["artifact_version"],
                        records.c.record_revision == replay["resulting_record_revision"],
                        records.c.payload_hash == replay["resulting_payload_hash"],
                    )
                ).mappings().one_or_none()
                if replayed_record is None:
                    raise FoundationProtocolError(c.FoundationRejectionCode.STALE_ENTITY)
                return dict(replayed_record)

            prior_revision = self._case_revision(connection, case_id)
            row = connection.execute(
                select(records).where(
                    records.c.case_id == str(case_id),
                    records.c.artifact_type == "SPEC_PACKAGE",
                    records.c.artifact_id == str(request.artifact_id),
                    records.c.artifact_version == request.artifact_version,
                    records.c.record_revision == request.record_revision,
                    records.c.payload_hash == request.payload_hash,
                )
            ).mappings().one_or_none()
            if row is None:
                raise FoundationProtocolError(c.FoundationRejectionCode.STALE_ENTITY)
            payload = json.loads(row["payload_json"])
            try:
                revised = materialize_supported_evidence(
                    payload=payload,
                    request=request,
                    candidate=candidate,
                    new_id=self.new_id,
                )
                validate_exact("spec-package-payload.schema.json", revised)
            except ValueError as exc:
                raise FoundationProtocolError(
                    c.FoundationRejectionCode.EVIDENCE_BINDING_FAILED
                ) from exc
            plan_json = connection.execute(
                select(plans.c.plan_json).where(
                    plans.c.case_id == str(case_id),
                    plans.c.artifact_id == str(request.artifact_id),
                    plans.c.artifact_version == request.artifact_version,
                    plans.c.status == "CONSUMED",
                )
            ).scalar_one_or_none()
            if plan_json is None:
                raise FoundationProtocolError(c.FoundationRejectionCode.IDENTITY_PLAN_FAILED)
            stored_plan = json.loads(plan_json)
            if isinstance(stored_plan, dict) and "identity_plan" in stored_plan:
                stored_plan = stored_plan["identity_plan"]
            plan = c.ArtifactSynthesisIdentityPlan.model_validate_json(
                json.dumps(stored_plan, sort_keys=True, separators=(",", ":"))
            )
            planned = {
                str(item.foundation_id): item.entity_kind
                for item in plan.planned_identities
            }
            identity_kinds = self._artifact_identity_kinds(revised)
            if not set(identity_kinds).issubset(planned) or any(
                planned[identity] != kind for identity, kind in identity_kinds.items()
            ):
                raise FoundationProtocolError(c.FoundationRejectionCode.IDENTITY_PLAN_FAILED)

            allowed = set(identity_kinds)
            allowed.update(
                connection.execute(
                    select(semantic_records.c.foundation_id).where(
                        semantic_records.c.case_id == str(case_id),
                        semantic_records.c.status != c.SemanticRecordStatus.STALE.value,
                    )
                ).scalars()
            )
            active = connection.execute(
                select(WORKSHOP_PROTOCOL_TABLES["workshop_analyzer_contexts"]).where(
                    WORKSHOP_PROTOCOL_TABLES["workshop_analyzer_contexts"].c.case_id
                    == str(case_id),
                    WORKSHOP_PROTOCOL_TABLES["workshop_analyzer_contexts"].c.status
                    == c.ContextStatus.ACTIVE.value,
                )
            ).mappings().one()
            allowed.update(_source_ids_from_context_json(active["binding_json"]))
            allowed.update(
                connection.execute(
                    select(case_participants.c.actor_id).where(
                        case_participants.c.case_id == str(case_id)
                    )
                ).scalars()
            )
            issues = validate_artifact_reference_graph(
                revised, allowed_reference_ids=allowed
            )
            if issues:
                raise FoundationProtocolError(
                    c.FoundationRejectionCode.UNKNOWN_REFERENCE,
                    safe_diagnostic_pointers=tuple(item.pointer for item in issues),
                )
            revised_hash = payload_hash(revised)
            next_record_revision = request.record_revision + 1
            updated = connection.execute(
                update(records)
                .where(
                    records.c.case_id == str(case_id),
                    records.c.artifact_id == str(request.artifact_id),
                    records.c.artifact_version == request.artifact_version,
                    records.c.record_revision == request.record_revision,
                    records.c.payload_hash == request.payload_hash,
                )
                .values(
                    record_revision=next_record_revision,
                    payload_hash=revised_hash,
                    payload_json=_json(revised),
                )
            )
            if updated.rowcount != 1:
                raise FoundationProtocolError(c.FoundationRejectionCode.STALE_ENTITY)
            expected_client_request_id = f"aqa-support-{request.request_hash[7:39]}"
            if execution.client_request_id != expected_client_request_id:
                raise FoundationProtocolError(
                    c.FoundationRejectionCode.EVIDENCE_BINDING_FAILED
                )
            assessments = {item.pair_id: item for item in candidate.assessments}
            dispositions = {
                pair.pair_id: evidence_assessment_disposition(
                    assessments[pair.pair_id].assessment
                )
                for pair in request.pairs
            }
            canonical_count = sum(
                disposition == "CANONICAL" for disposition in dispositions.values()
            )
            created_at = _instant(execution.completed_at)
            connection.execute(
                insert(runs).values(
                    request_hash=request.request_hash,
                    request_id=str(request.request_id),
                    evaluator_run_id=str(request.evaluator_run_id),
                    case_id=str(case_id),
                    artifact_id=str(request.artifact_id),
                    artifact_version=request.artifact_version,
                    assessed_record_revision=request.record_revision,
                    assessed_payload_hash=request.payload_hash,
                    resulting_record_revision=next_record_revision,
                    resulting_payload_hash=revised_hash,
                    policy_version="1.0.0",
                    provider=execution.provider,
                    model=execution.model,
                    reasoning_effort=execution.reasoning_effort,
                    provider_response_id=execution.provider_response_id,
                    client_request_id=execution.client_request_id,
                    canonical_count=canonical_count,
                    quarantined_count=len(request.pairs) - canonical_count,
                    request_json=request_json,
                    candidate_json=candidate_json,
                    execution_json=execution_json,
                    created_at=created_at,
                )
            )
            connection.execute(
                insert(assessment_rows),
                [
                    {
                        "request_hash": request.request_hash,
                        "pair_id": str(pair.pair_id),
                        "claim_ref": str(pair.claim_ref),
                        "claim_pointer": pair.claim_pointer,
                        "claim_hash": pair.claim_hash,
                        "evidence_ref": str(pair.evidence_ref),
                        "source_id": str(pair.source_id),
                        "source_hash": pair.source_hash,
                        "excerpt_hash": pair.excerpt_hash,
                        "assessment": assessments[pair.pair_id].assessment.value,
                        "confidence": assessments[pair.pair_id].confidence,
                        "disposition": dispositions[pair.pair_id],
                        "pair_json": _json(pair),
                        "assessment_json": _json(assessments[pair.pair_id]),
                        "created_at": created_at,
                    }
                    for pair in request.pairs
                ],
            )
            self._advance_revision(connection, case_id, prior_revision)
        result = self.latest_artifact_record(case_id, "SPEC_PACKAGE")
        assert result is not None
        return result

    def prepare_artifact_quality_revision(
        self,
        case_id: UUID,
        artifact_type: str,
        *,
        allocated_identity_kinds: tuple[str, ...],
    ) -> q.ArtifactQualityRevisionRequest:
        """Create the one deterministic revision request for the latest failed audit."""

        from .workshop_protocol import FoundationProtocolError

        record = self.latest_artifact_record(case_id, artifact_type)
        if record is None:
            raise FoundationProtocolError(c.FoundationRejectionCode.UNKNOWN_REFERENCE)
        audits = ARTIFACT_QUALITY_TABLES["workshop_artifact_quality_audits"]
        with self.engine.connect() as connection:
            row = connection.execute(
                select(audits).where(
                    audits.c.case_id == str(case_id),
                    audits.c.artifact_id == record["artifact_id"],
                    audits.c.artifact_version == record["artifact_version"],
                    audits.c.resulting_record_revision == record["record_revision"],
                    audits.c.payload_hash == record["payload_hash"],
                    audits.c.state == q.AuditState.ADMITTED.value,
                    audits.c.outcome != q.AuditOutcome.PASS.value,
                )
            ).mappings().one_or_none()
            based_on_case_revision = self._case_revision(connection, case_id)
        if row is None or row["receipt_json"] is None:
            raise FoundationProtocolError(c.FoundationRejectionCode.INVALID_TRANSITION)
        receipt = q.ArtifactQualityAuditReceipt.model_validate_json(row["receipt_json"])
        finding_ids = tuple(item.finding_id for item in receipt.findings)
        failed_rule_ids = tuple(
            item.rule_id
            for item in receipt.combined_rule_results
            if item.result is q.ComponentResult.FAIL
        )
        finding_pointers = tuple(
            sorted(
                {
                    pointer
                    for finding in receipt.findings
                    for pointer in finding.artifact_pointers
                    if pointer
                }
            )
        )
        payload = json.loads(record["payload_json"])
        pointers = quality_revision_pointer_closure(
            payload=payload,
            finding_pointers=finding_pointers,
            allocated_identity_kinds=allocated_identity_kinds,
        )
        if not finding_ids or not failed_rule_ids or not pointers:
            raise FoundationProtocolError(c.FoundationRejectionCode.INVALID_TRANSITION)
        request_id = uuid5(receipt.audit_id, "artifact-quality-revision-request-v1")
        allocated_ids = tuple(
            uuid5(request_id, f"allocated:{index}:{kind}")
            for index, kind in enumerate(allocated_identity_kinds)
        )
        generated_ids = iter((request_id, *allocated_ids))
        return prepare_quality_revision_request(
            artifact_id=UUID(record["artifact_id"]),
            artifact_version=record["artifact_version"],
            record_revision=record["record_revision"],
            based_on_case_revision=based_on_case_revision,
            payload=payload,
            audit_id=receipt.audit_id,
            finding_ids=finding_ids,
            failed_rule_ids=failed_rule_ids,
            canonical_artifact_pointers=pointers,
            allocated_identity_kinds=allocated_identity_kinds,
            new_id=lambda: next(generated_ids),
        )

    def apply_artifact_quality_revision(
        self,
        case_id: UUID,
        request: q.ArtifactQualityRevisionRequest,
        candidate: q.ArtifactQualityRevisionCandidate,
    ) -> q.ArtifactQualityRevisionReceipt:
        """Atomically apply or replay the one exact pointer-bounded revision."""

        from .workshop_protocol import FoundationProtocolError

        records = WORKSHOP_PROTOCOL_TABLES["workshop_artifact_records"]
        audits = ARTIFACT_QUALITY_TABLES["workshop_artifact_quality_audits"]
        semantic_records = WORKSHOP_PROTOCOL_TABLES["workshop_semantic_records"]
        with self.engine.begin() as connection:
            audit = connection.execute(
                select(audits).where(
                    audits.c.audit_id == str(request.audit_id),
                    audits.c.case_id == str(case_id),
                    audits.c.artifact_id == str(request.artifact_id),
                    audits.c.artifact_version == request.artifact_version,
                    audits.c.resulting_record_revision == request.record_revision,
                    audits.c.payload_hash == request.payload_hash,
                    audits.c.state == q.AuditState.ADMITTED.value,
                    audits.c.outcome != q.AuditOutcome.PASS.value,
                )
            ).mappings().one_or_none()
            if audit is None or audit["receipt_json"] is None:
                raise FoundationProtocolError(c.FoundationRejectionCode.INVALID_TRANSITION)
            prior_receipt = q.ArtifactQualityAuditReceipt.model_validate_json(
                audit["receipt_json"]
            )
            expected_findings = tuple(item.finding_id for item in prior_receipt.findings)
            expected_rules = tuple(
                item.rule_id
                for item in prior_receipt.combined_rule_results
                if item.result is q.ComponentResult.FAIL
            )
            finding_pointers = tuple(
                sorted(
                    {
                        pointer
                        for finding in prior_receipt.findings
                        for pointer in finding.artifact_pointers
                        if pointer
                    }
                )
            )
            try:
                base_payload = json.loads(request.canonical_payload_json)
            except json.JSONDecodeError as exc:
                raise FoundationProtocolError(
                    c.FoundationRejectionCode.PAYLOAD_SCHEMA_FAILED
                ) from exc
            expected_pointers = quality_revision_pointer_closure(
                payload=base_payload,
                finding_pointers=finding_pointers,
                allocated_identity_kinds=tuple(
                    item.entity_kind for item in request.allocated_identities
                ),
            )
            if (
                request.finding_ids != expected_findings
                or request.failed_rule_ids != expected_rules
                or request.canonical_artifact_pointers != expected_pointers
            ):
                raise FoundationProtocolError(
                    c.FoundationRejectionCode.PROVIDER_REQUEST_BINDING_FAILED
                )
            expected_request_id = uuid5(
                request.audit_id, "artifact-quality-revision-request-v1"
            )
            expected_allocated = tuple(
                uuid5(expected_request_id, f"allocated:{index}:{item.entity_kind}")
                for index, item in enumerate(request.allocated_identities)
            )
            if request.revision_request_id != expected_request_id or tuple(
                item.foundation_id for item in request.allocated_identities
            ) != expected_allocated:
                raise FoundationProtocolError(c.FoundationRejectionCode.IDENTITY_PLAN_FAILED)
            try:
                revised = apply_quality_revision_candidate(
                    payload=base_payload,
                    request=request,
                    candidate=candidate,
                    identity_kinds=self._artifact_identity_kinds,
                )
                artifact_record = connection.execute(
                    select(records.c.artifact_type, records.c.confirmed_from_json).where(
                        records.c.case_id == str(case_id),
                        records.c.artifact_id == str(request.artifact_id),
                        records.c.artifact_version == request.artifact_version,
                    )
                ).mappings().one()
                artifact_type = artifact_record["artifact_type"]
                validate_exact(
                    "spec-package-payload.schema.json"
                    if artifact_type == "SPEC_PACKAGE"
                    else "technical-contract-payload.schema.json",
                    revised,
                )
            except (ValueError, json.JSONDecodeError) as exc:
                raise FoundationProtocolError(
                    c.FoundationRejectionCode.PAYLOAD_SCHEMA_FAILED
                ) from exc

            allowed = set(self._artifact_identity_kinds(revised))
            allowed.update(
                connection.execute(
                    select(semantic_records.c.foundation_id).where(
                        semantic_records.c.case_id == str(case_id),
                        semantic_records.c.status != c.SemanticRecordStatus.STALE.value,
                    )
                ).scalars()
            )
            active = connection.execute(
                select(WORKSHOP_PROTOCOL_TABLES["workshop_analyzer_contexts"]).where(
                    WORKSHOP_PROTOCOL_TABLES["workshop_analyzer_contexts"].c.case_id
                    == str(case_id),
                    WORKSHOP_PROTOCOL_TABLES["workshop_analyzer_contexts"].c.status
                    == c.ContextStatus.ACTIVE.value,
                )
            ).mappings().one()
            allowed.update(_source_ids_from_context_json(active["binding_json"]))
            allowed.update(
                connection.execute(
                    select(case_participants.c.actor_id).where(
                        case_participants.c.case_id == str(case_id)
                    )
                ).scalars()
            )
            try:
                confirmed_spec_payload = _confirmed_spec_payload_from_record(
                    artifact_type, artifact_record["confirmed_from_json"]
                )
            except ValueError as exc:
                raise FoundationProtocolError(
                    c.FoundationRejectionCode.PROVIDER_REQUEST_BINDING_FAILED
                ) from exc
            if confirmed_spec_payload is not None:
                allowed.update(self._artifact_identity_kinds(confirmed_spec_payload))
            issues = validate_artifact_reference_graph(
                revised, allowed_reference_ids=allowed
            )
            if issues:
                raise FoundationProtocolError(
                    c.FoundationRejectionCode.UNKNOWN_REFERENCE,
                    safe_diagnostic_pointers=tuple(item.pointer for item in issues),
                )
            resulting_hash = payload_hash(revised)
            resulting_record_revision = request.record_revision + 1
            current = connection.execute(
                select(records).where(
                    records.c.case_id == str(case_id),
                    records.c.artifact_id == str(request.artifact_id),
                    records.c.artifact_version == request.artifact_version,
                )
            ).mappings().one()
            if (
                current["record_revision"] == resulting_record_revision
                and current["payload_hash"] == resulting_hash
                and current["payload_json"] == _json(revised)
            ):
                return q.ArtifactQualityRevisionReceipt(
                    protocol_version=q.PROTOCOL_VERSION,
                    revision_request_id=request.revision_request_id,
                    revision_request_version=1,
                    request_hash=request.request_hash,
                    artifact_id=request.artifact_id,
                    artifact_version=request.artifact_version,
                    prior_record_revision=request.record_revision,
                    resulting_record_revision=resulting_record_revision,
                    prior_payload_hash=request.payload_hash,
                    resulting_payload_hash=resulting_hash,
                    audit_id=request.audit_id,
                    resulting_case_revision=request.based_on_case_revision + 1,
                    replayed=True,
                )
            if (
                current["record_revision"] != request.record_revision
                or current["payload_hash"] != request.payload_hash
                or current["payload_json"] != request.canonical_payload_json
                or self._case_revision(connection, case_id)
                != request.based_on_case_revision
            ):
                raise FoundationProtocolError(c.FoundationRejectionCode.STALE_ENTITY)
            quality_hash = json.loads(current["governance_json"])["quality_contract"][
                "content_hash"
            ]
            governance = draft_governance(
                artifact_type=artifact_type,
                payload=revised,
                target=SimpleNamespace(next_artifact_version=request.artifact_version),
                quality_hash=quality_hash,
                now=self.now(),
                new_id=self.new_id,
            )
            result = connection.execute(
                update(records)
                .where(
                    records.c.case_id == str(case_id),
                    records.c.artifact_id == str(request.artifact_id),
                    records.c.artifact_version == request.artifact_version,
                    records.c.record_revision == request.record_revision,
                    records.c.payload_hash == request.payload_hash,
                )
                .values(
                    record_revision=resulting_record_revision,
                    payload_hash=resulting_hash,
                    payload_json=_json(revised),
                    governance_json=_json(governance),
                )
            )
            if result.rowcount != 1:
                raise FoundationProtocolError(c.FoundationRejectionCode.STALE_ENTITY)
            self._set_case_quality_state(
                connection,
                case_id,
                q.AuditOutcome.NEEDS_CLARIFICATION,
                awaiting_confirmation=False,
            )
            self._advance_revision(
                connection, case_id, request.based_on_case_revision
            )
        return q.ArtifactQualityRevisionReceipt(
            protocol_version=q.PROTOCOL_VERSION,
            revision_request_id=request.revision_request_id,
            revision_request_version=1,
            request_hash=request.request_hash,
            artifact_id=request.artifact_id,
            artifact_version=request.artifact_version,
            prior_record_revision=request.record_revision,
            resulting_record_revision=resulting_record_revision,
            prior_payload_hash=request.payload_hash,
            resulting_payload_hash=resulting_hash,
            audit_id=request.audit_id,
            resulting_case_revision=request.based_on_case_revision + 1,
            replayed=False,
        )

    def prepare_artifact_quality_audit(
        self, command: q.PrepareArtifactQualityAuditCommand
    ) -> q.PreparedArtifactQualityAudit:
        audits = ARTIFACT_QUALITY_TABLES["workshop_artifact_quality_audits"]
        with self.engine.begin() as connection:
            existing = connection.execute(
                select(audits).where(audits.c.audit_id == str(command.bundle.audit_id))
            ).mappings().one_or_none()
            if existing is not None:
                if (
                    existing["request_hash"] != command.bundle.request_hash
                    or existing["bundle_json"] != _json(command.bundle)
                ):
                    self._quality_reject(c.FoundationRejectionCode.DUPLICATE_CONFLICT)
                receipt = (
                    None
                    if existing["receipt_json"] is None
                    else q.ArtifactQualityAuditReceipt.model_validate_json(
                        existing["receipt_json"]
                    ).model_copy(update={"replayed": True})
                )
                return q.PreparedArtifactQualityAudit(
                    audit_id=command.bundle.audit_id,
                    request_hash=command.bundle.request_hash,
                    state=q.AuditState(existing["state"]),
                    outcome=q.AuditOutcome(existing["outcome"]),
                    existing_receipt=receipt,
                )
            current_revision = self._case_revision(connection, command.bundle.case_id)
            if current_revision != command.expected_case_revision:
                self._quality_reject(c.FoundationRejectionCode.STALE_STATE)
            self._validate_quality_bundle_against_foundation(connection, command.bundle)
            now = _instant(self.now())
            try:
                connection.execute(
                    insert(audits).values(
                        audit_id=str(command.bundle.audit_id),
                        evaluator_run_id=str(command.bundle.evaluator_run_id),
                        case_id=str(command.bundle.case_id),
                        artifact_id=str(command.bundle.subject.artifact_id),
                        artifact_version=command.bundle.subject.artifact_version,
                        audited_record_revision=command.bundle.subject.record_revision,
                        resulting_record_revision=None,
                        payload_hash=command.bundle.subject.payload_hash,
                        request_hash=command.bundle.request_hash,
                        audit_scope_manifest_hash=command.bundle.audit_scope_manifest_hash,
                        quality_contract_hash=command.bundle.quality_contract.content_hash,
                        source_set_hash=command.bundle.source_set_hash,
                        transcript_manifest_hash=command.bundle.transcript_manifest_hash,
                        semantic_state_hash=command.bundle.semantic_state_hash,
                        state=q.AuditState.PENDING_PROVIDER.value,
                        outcome=q.AuditOutcome.PENDING_AUDIT.value,
                        provider=None,
                        model=None,
                        reasoning_effort=None,
                        provider_conversation_id=None,
                        provider_response_id=None,
                        client_request_id=None,
                        provider_context_json=None,
                        bundle_json=_json(command.bundle),
                        execution_json=None,
                        candidate_json=None,
                        combined_results_json=None,
                        receipt_json=None,
                        created_at=now,
                        updated_at=now,
                    )
                )
            except IntegrityError:
                self._quality_reject(c.FoundationRejectionCode.DUPLICATE_CONFLICT)
        return q.PreparedArtifactQualityAudit(
            audit_id=command.bundle.audit_id,
            request_hash=command.bundle.request_hash,
            state=q.AuditState.PENDING_PROVIDER,
            outcome=q.AuditOutcome.PENDING_AUDIT,
            existing_receipt=None,
        )

    def bind_artifact_quality_evaluator(
        self, command: q.BindArtifactQualityEvaluatorCommand
    ) -> q.PreparedArtifactQualityAudit:
        audits = ARTIFACT_QUALITY_TABLES["workshop_artifact_quality_audits"]
        with self.engine.begin() as connection:
            row = connection.execute(
                select(audits).where(audits.c.audit_id == str(command.audit_id))
            ).mappings().one_or_none()
            if row is None or row["request_hash"] != command.request_hash:
                self._quality_reject(c.FoundationRejectionCode.UNKNOWN_REFERENCE)
            expected = (
                command.provider,
                command.model,
                command.reasoning_effort,
                command.provider_conversation_id,
            )
            if row["state"] != q.AuditState.PENDING_PROVIDER.value:
                actual = (
                    row["provider"], row["model"], row["reasoning_effort"],
                    row["provider_conversation_id"],
                )
                if actual != expected:
                    self._quality_reject(c.FoundationRejectionCode.DUPLICATE_CONFLICT)
            else:
                connection.execute(
                    update(audits)
                    .where(audits.c.audit_id == str(command.audit_id))
                    .values(
                        state=q.AuditState.CONTEXT_READY.value,
                        provider=command.provider,
                        model=command.model,
                        reasoning_effort=command.reasoning_effort,
                        provider_conversation_id=command.provider_conversation_id,
                        client_request_id=command.client_request_id,
                        provider_context_json=_json(
                            {
                                "provider": command.provider,
                                "model": command.model,
                                "reasoning_effort": command.reasoning_effort,
                                "provider_conversation_id": command.provider_conversation_id,
                                "client_request_id": command.client_request_id,
                                "started_at": command.started_at,
                            }
                        ),
                        updated_at=_instant(self.now()),
                    )
                )
            return q.PreparedArtifactQualityAudit(
                audit_id=command.audit_id,
                request_hash=command.request_hash,
                state=q.AuditState(row["state"] if row["state"] != q.AuditState.PENDING_PROVIDER.value else q.AuditState.CONTEXT_READY.value),
                outcome=q.AuditOutcome(row["outcome"]),
                existing_receipt=(
                    None
                    if row["receipt_json"] is None
                    else q.ArtifactQualityAuditReceipt.model_validate_json(
                        row["receipt_json"]
                    ).model_copy(update={"replayed": True})
                ),
            )

    def artifact_quality_provider_context(self, audit_id: UUID) -> dict[str, Any] | None:
        audits = ARTIFACT_QUALITY_TABLES["workshop_artifact_quality_audits"]
        with self.engine.connect() as connection:
            value = connection.execute(
                select(audits.c.provider_context_json).where(
                    audits.c.audit_id == str(audit_id),
                    audits.c.state == q.AuditState.CONTEXT_READY.value,
                )
            ).scalar_one_or_none()
        return None if value is None else json.loads(value)

    def checkpoint_artifact_quality_response(
        self, command: q.CheckpointArtifactQualityResponseCommand
    ) -> None:
        """Durably bind the one accepted provider Response before polling it."""

        audits = ARTIFACT_QUALITY_TABLES["workshop_artifact_quality_audits"]
        with self.engine.begin() as connection:
            row = connection.execute(
                select(audits).where(audits.c.audit_id == str(command.audit_id))
            ).mappings().one_or_none()
            if (
                row is None
                or row["request_hash"] != command.request_hash
                or row["state"] != q.AuditState.CONTEXT_READY.value
            ):
                self._quality_reject(c.FoundationRejectionCode.UNKNOWN_REFERENCE)
            if row["provider_response_id"] is not None:
                if (
                    row["provider_response_id"] != command.provider_response_id
                    or row["client_request_id"] != command.client_request_id
                ):
                    self._quality_reject(c.FoundationRejectionCode.DUPLICATE_CONFLICT)
                return
            provider_context = json.loads(row["provider_context_json"])
            provider_context.update(
                {
                    "provider_response_id": command.provider_response_id,
                    "response_client_request_id": command.client_request_id,
                }
            )
            connection.execute(
                update(audits)
                .where(audits.c.audit_id == str(command.audit_id))
                .values(
                    provider_response_id=command.provider_response_id,
                    client_request_id=command.client_request_id,
                    provider_context_json=_json(provider_context),
                    updated_at=_instant(self.now()),
                )
            )

    def admit_artifact_quality_audit(
        self, command: q.AdmitArtifactQualityAuditCommand
    ) -> q.ArtifactQualityAuditReceipt:
        audits = ARTIFACT_QUALITY_TABLES["workshop_artifact_quality_audits"]
        records = WORKSHOP_PROTOCOL_TABLES["workshop_artifact_records"]
        findings_table = ARTIFACT_QUALITY_TABLES["workshop_artifact_quality_findings"]
        with self.engine.begin() as connection:
            row = connection.execute(
                select(audits).where(audits.c.audit_id == str(command.bundle.audit_id))
            ).mappings().one_or_none()
            if row is None:
                self._quality_reject(c.FoundationRejectionCode.UNKNOWN_REFERENCE)
            if row["state"] == q.AuditState.ADMITTED.value:
                if (
                    row["request_hash"] != command.bundle.request_hash
                    or row["candidate_json"] != _json(command.candidate)
                    or row["execution_json"] != _json(command.execution)
                ):
                    self._quality_reject(c.FoundationRejectionCode.DUPLICATE_CONFLICT)
                return q.ArtifactQualityAuditReceipt.model_validate_json(
                    row["receipt_json"]
                ).model_copy(update={"replayed": True})
            if row["state"] != q.AuditState.CONTEXT_READY.value:
                self._quality_reject(c.FoundationRejectionCode.PROVIDER_REQUEST_BINDING_FAILED)
            current_revision = self._case_revision(connection, command.bundle.case_id)
            if current_revision != command.expected_case_revision:
                self._quality_reject(c.FoundationRejectionCode.STALE_STATE)
            if row["bundle_json"] != _json(command.bundle):
                self._quality_reject(c.FoundationRejectionCode.DUPLICATE_CONFLICT)
            if (
                row["provider"], row["model"], row["reasoning_effort"],
                row["provider_conversation_id"],
            ) != (
                command.execution.provider,
                command.execution.model,
                command.execution.reasoning_effort,
                command.execution.provider_conversation_id,
            ):
                self._quality_reject(c.FoundationRejectionCode.PROVIDER_REQUEST_BINDING_FAILED)
            if (
                row["provider_response_id"] is not None
                and (
                    row["provider_response_id"]
                    != command.execution.provider_response_id
                    or row["client_request_id"]
                    != command.execution.client_request_id
                )
            ):
                self._quality_reject(c.FoundationRejectionCode.PROVIDER_REQUEST_BINDING_FAILED)
            if (
                command.execution.provider != "OPENAI"
                or command.execution.model != "gpt-5.6-terra"
                or command.execution.reasoning_effort != "medium"
                or not command.execution.store_enabled
            ):
                self._quality_reject(c.FoundationRejectionCode.PROVIDER_REQUEST_BINDING_FAILED)
            self._validate_quality_bundle_against_foundation(connection, command.bundle)
            self._validate_quality_candidate(command.bundle, command.candidate)

            admitted_findings, finding_ids = self._admit_quality_findings(
                connection, command.bundle, command.candidate
            )
            combined = self._reduce_payload_quality(
                connection, command.bundle, command.candidate, finding_ids
            )
            outcome = self._quality_outcome(combined)
            artifact = connection.execute(
                select(records).where(
                    records.c.case_id == str(command.bundle.case_id),
                    records.c.artifact_id == str(command.bundle.subject.artifact_id),
                    records.c.artifact_version == command.bundle.subject.artifact_version,
                    records.c.record_revision == command.bundle.subject.record_revision,
                    records.c.payload_hash == command.bundle.subject.payload_hash,
                )
            ).mappings().one()
            governance = self._quality_governance(
                artifact_type=command.bundle.subject.artifact_type,
                governance=json.loads(artifact["governance_json"]),
                combined=combined,
                outcome=outcome,
                admitted_findings=admitted_findings,
            )
            resulting_record_revision = artifact["record_revision"] + 1
            connection.execute(
                update(records)
                .where(
                    records.c.artifact_id == artifact["artifact_id"],
                    records.c.artifact_version == artifact["artifact_version"],
                    records.c.record_revision == artifact["record_revision"],
                )
                .values(
                    record_revision=resulting_record_revision,
                    governance_json=_json(governance),
                )
            )
            self._set_case_quality_state(
                connection, command.bundle.case_id, outcome, awaiting_confirmation=True
            )
            self._advance_revision(connection, command.bundle.case_id, current_revision)
            receipt = q.ArtifactQualityAuditReceipt(
                protocol_version=q.PROTOCOL_VERSION,
                audit_id=command.bundle.audit_id,
                request_hash=command.bundle.request_hash,
                state=q.AuditState.ADMITTED,
                artifact_id=command.bundle.subject.artifact_id,
                artifact_version=command.bundle.subject.artifact_version,
                audited_record_revision=command.bundle.subject.record_revision,
                resulting_record_revision=resulting_record_revision,
                outcome=outcome,
                findings=admitted_findings,
                combined_rule_results=combined,
                resulting_case_revision=current_revision + 1,
                replayed=False,
            )
            connection.execute(
                update(audits)
                .where(audits.c.audit_id == str(command.bundle.audit_id))
                .values(
                    state=q.AuditState.ADMITTED.value,
                    outcome=outcome.value,
                    resulting_record_revision=resulting_record_revision,
                    provider_response_id=command.execution.provider_response_id,
                    client_request_id=command.execution.client_request_id,
                    execution_json=_json(command.execution),
                    candidate_json=_json(command.candidate),
                    combined_results_json=_json(combined),
                    receipt_json=_json(receipt),
                    updated_at=_instant(self.now()),
                )
            )
        return receipt

    def artifact_quality_receipt(
        self, case_id: UUID, artifact_id: UUID, artifact_version: int
    ) -> q.ArtifactQualityAuditReceipt | None:
        audits = ARTIFACT_QUALITY_TABLES["workshop_artifact_quality_audits"]
        with self.engine.connect() as connection:
            value = connection.execute(
                select(audits.c.receipt_json).where(
                    audits.c.case_id == str(case_id),
                    audits.c.artifact_id == str(artifact_id),
                    audits.c.artifact_version == artifact_version,
                    audits.c.state == q.AuditState.ADMITTED.value,
                )
            ).scalar_one_or_none()
        return None if value is None else q.ArtifactQualityAuditReceipt.model_validate_json(value)

    def _validate_quality_bundle_against_foundation(self, connection, bundle) -> None:
        from .workshop_protocol import FoundationProtocolError

        active = connection.execute(
            select(WORKSHOP_PROTOCOL_TABLES["workshop_analyzer_contexts"]).where(
                WORKSHOP_PROTOCOL_TABLES["workshop_analyzer_contexts"].c.case_id == str(bundle.case_id),
                WORKSHOP_PROTOCOL_TABLES["workshop_analyzer_contexts"].c.status == c.ContextStatus.ACTIVE.value,
            )
        ).mappings().one_or_none()
        if active is None:
            raise FoundationProtocolError(c.FoundationRejectionCode.PROVIDER_REQUEST_BINDING_FAILED)
        try:
            validate_audit_bundle(
                bundle,
                expected_quality_hash=quality_contract_hash(),
            )
        except (ValueError, json.JSONDecodeError) as exc:
            raise FoundationProtocolError(c.FoundationRejectionCode.PAYLOAD_SCHEMA_FAILED) from exc
        protocol_case = connection.execute(
            select(WORKSHOP_PROTOCOL_TABLES["workshop_protocol_cases"]).where(
                WORKSHOP_PROTOCOL_TABLES["workshop_protocol_cases"].c.case_id == str(bundle.case_id)
            )
        ).mappings().one()
        if (
            protocol_case["session_id"] != str(bundle.session_id)
            or protocol_case["source_set_hash"] != bundle.source_set_hash
            or self._case_revision(connection, bundle.case_id) != bundle.based_on_case_revision
        ):
            raise FoundationProtocolError(c.FoundationRejectionCode.STALE_STATE)
        records = WORKSHOP_PROTOCOL_TABLES["workshop_artifact_records"]
        artifact = connection.execute(
            select(records).where(
                records.c.case_id == str(bundle.case_id),
                records.c.artifact_type == bundle.subject.artifact_type.value,
                records.c.artifact_id == str(bundle.subject.artifact_id),
                records.c.artifact_key == bundle.subject.artifact_key,
                records.c.artifact_version == bundle.subject.artifact_version,
                records.c.record_revision == bundle.subject.record_revision,
                records.c.payload_hash == bundle.subject.payload_hash,
                records.c.payload_json == bundle.subject.canonical_payload_json,
            )
        ).mappings().one_or_none()
        latest_version = connection.execute(
            select(func.max(records.c.artifact_version)).where(
                records.c.case_id == str(bundle.case_id),
                records.c.artifact_type == bundle.subject.artifact_type.value,
            )
        ).scalar_one()
        if artifact is None or latest_version != bundle.subject.artifact_version:
            raise FoundationProtocolError(c.FoundationRejectionCode.STALE_ENTITY)
        stored_sources = connection.execute(
            select(source_artifacts).where(source_artifacts.c.case_id == str(bundle.case_id))
        ).mappings().all()
        by_source = {(row["artifact_id"], row["version"]): row for row in stored_sources}
        for source in bundle.sources:
            row = by_source.get((str(source.source_id), source.version))
            expected_hash = source.payload_hash.removeprefix("sha256:")
            if row is None or row["content_hash"] != expected_hash or row["canonical_locator"] != source.canonical_locator:
                raise FoundationProtocolError(c.FoundationRejectionCode.SOURCE_BINDING_FAILED)
        transcript_events = self.final_transcripts(bundle.case_id)
        expected_transcripts = tuple(
            q.AuditTranscript(
                event_id=item.event_id,
                sequence_number=item.sequence_number,
                transcript_artifact_id=item.transcript_artifact_id,
                transcript_version=item.transcript_version,
                transcript_hash=item.transcript_hash,
                actor=q.TranscriptActor(item.actor.value),
                speaker_actor_id=item.speaker_actor_id,
                complete_text=item.text,
            )
            for item in transcript_events
        )
        if expected_transcripts != bundle.transcripts:
            raise FoundationProtocolError(c.FoundationRejectionCode.TRANSCRIPT_BINDING_FAILED)
        snapshot = self.semantic_snapshot(bundle.case_id)
        if (
            _json(snapshot) != bundle.canonical_semantic_snapshot_json
            or domain_hash("SPECOPS:SEMANTIC_STATE:v1", snapshot.model_dump(mode="json"))
            != bundle.semantic_state_hash
        ):
            raise FoundationProtocolError(c.FoundationRejectionCode.STALE_ENTITY)
        if bundle.subject.artifact_type is q.ArtifactType.TECHNICAL_CONTRACT:
            confirmed = self.confirmed_spec_binding(bundle.case_id)
            if confirmed is None or bundle.confirmed_spec is None:
                raise FoundationProtocolError(c.FoundationRejectionCode.CONFIRMATION_BINDING_FAILED)
            if bundle.confirmed_spec.model_dump(mode="json") != {
                "artifact_id": str(confirmed.foundation_artifact_id),
                "artifact_key": confirmed.artifact_key,
                "artifact_version": confirmed.artifact_version,
                "record_revision": confirmed.record_revision,
                "payload_hash": confirmed.payload_hash,
                "confirmation_id": str(confirmed.confirmation_id),
                "confirmed_case_revision": confirmed.confirmed_case_revision,
                "canonical_payload_json": confirmed.canonical_payload_json,
            }:
                raise FoundationProtocolError(c.FoundationRejectionCode.CONFIRMATION_BINDING_FAILED)

    def _validate_quality_candidate(self, bundle, candidate) -> None:
        from .workshop_protocol import FoundationProtocolError

        echoes = (
            candidate.audit_id == bundle.audit_id,
            candidate.evaluator_run_id == bundle.evaluator_run_id,
            candidate.request_hash == bundle.request_hash,
            candidate.artifact_id == bundle.subject.artifact_id,
            candidate.artifact_version == bundle.subject.artifact_version,
            candidate.record_revision == bundle.subject.record_revision,
            candidate.payload_hash == bundle.subject.payload_hash,
            candidate.audit_scope_manifest_hash == bundle.audit_scope_manifest_hash,
            candidate.semantic_quality_contract_hash == bundle.quality_contract.content_hash,
            tuple(item.rule_id for item in candidate.assessments) == bundle.semantic_rule_ids,
        )
        if not all(echoes):
            raise FoundationProtocolError(c.FoundationRejectionCode.PROVIDER_REQUEST_BINDING_FAILED)
        payload = json.loads(bundle.subject.canonical_payload_json)
        evidence_ids = {
            str(item.evidence_id)
            for item in c.FoundationSemanticSnapshot.model_validate_json(
                bundle.canonical_semantic_snapshot_json
            ).evidence
        }
        evidence_ids.update(str(item["id"]) for item in payload.get("evidence_catalog", []))
        transcript_ids = {str(item.event_id) for item in bundle.transcripts}
        candidate_keys: set[str] = set()
        try:
            for assessment in candidate.assessments:
                for pointer in assessment.artifact_pointers:
                    resolve_payload_pointer(payload, pointer)
                if not {str(item) for item in assessment.evidence_ids}.issubset(evidence_ids):
                    raise KeyError("unknown evidence")
                if not {str(item) for item in assessment.transcript_event_ids}.issubset(transcript_ids):
                    raise KeyError("unknown transcript")
                for finding in assessment.findings:
                    if finding.candidate_key in candidate_keys:
                        raise KeyError("duplicate finding key")
                    candidate_keys.add(finding.candidate_key)
                    for pointer in finding.artifact_pointers:
                        resolve_payload_pointer(payload, pointer)
                    if not {str(item) for item in finding.evidence_ids}.issubset(evidence_ids):
                        raise KeyError("unknown finding evidence")
                    if not {str(item) for item in finding.transcript_event_ids}.issubset(transcript_ids):
                        raise KeyError("unknown finding transcript")
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            raise FoundationProtocolError(c.FoundationRejectionCode.UNKNOWN_REFERENCE) from exc

    def _admit_quality_findings(self, connection, bundle, candidate):
        table = ARTIFACT_QUALITY_TABLES["workshop_artifact_quality_findings"]
        admitted: list[q.AdmittedQualityFinding] = []
        by_rule: dict[str, tuple[UUID, ...]] = {}
        for assessment in candidate.assessments:
            ids = []
            for finding in assessment.findings:
                finding_id = self.new_id()
                value = q.AdmittedQualityFinding(
                    finding_id=finding_id,
                    finding_version=1,
                    audit_id=bundle.audit_id,
                    rule_id=assessment.rule_id,
                    **finding.model_dump(),
                )
                connection.execute(
                    insert(table).values(
                        finding_id=str(finding_id),
                        finding_version=1,
                        audit_id=str(bundle.audit_id),
                        rule_id=assessment.rule_id,
                        candidate_key=finding.candidate_key,
                        finding_json=_json(value),
                        created_at=_instant(self.now()),
                    )
                )
                admitted.append(value)
                ids.append(finding_id)
            by_rule[assessment.rule_id] = tuple(ids)
        return tuple(admitted), by_rule

    def _reduce_payload_quality(self, connection, bundle, candidate, finding_ids):
        payload = json.loads(bundle.subject.canonical_payload_json)
        semantic = {item.rule_id: item for item in candidate.assessments}
        global_refs_ok = self._payload_references_resolve(connection, bundle, payload)
        results = []
        for rule in bundle.rule_manifest:
            assessment = semantic.get(rule.rule_id)
            subchecks = []
            for check_type in rule.check_types:
                if check_type is q.QualityCheckType.SEMANTIC:
                    continue
                if rule.rule_id.endswith(("Q-024", "Q-025")):
                    result, codes, explanation = (
                        q.ComponentResult.PENDING,
                        ("LATER_GATE_PHASE",),
                        "This component executes only in its exact later Foundation gate phase.",
                    )
                else:
                    passed, codes, explanation = self._foundation_payload_subcheck(
                        connection, bundle, payload, rule.rule_id, check_type, global_refs_ok
                    )
                    result = q.ComponentResult.PASS if passed else q.ComponentResult.FAIL
                subchecks.append(
                    q.FoundationSubcheck(
                        check_type=check_type.value,
                        result=result,
                        evidence_codes=codes,
                        explanation=explanation,
                    )
                )
            components = [item.result for item in subchecks]
            if assessment is not None:
                semantic_pass = assessment.result in {
                    q.SemanticAssessmentResult.PASS,
                    q.SemanticAssessmentResult.NOT_APPLICABLE,
                }
                components.append(
                    q.ComponentResult.PASS if semantic_pass else q.ComponentResult.FAIL
                )
            result = (
                q.ComponentResult.FAIL
                if q.ComponentResult.FAIL in components
                else q.ComponentResult.PENDING
                if q.ComponentResult.PENDING in components
                else q.ComponentResult.PASS
            )
            explanations = [item.explanation for item in subchecks]
            if assessment is not None:
                explanations.insert(0, assessment.explanation)
            results.append(
                q.CombinedQualityRuleResult(
                    rule_id=rule.rule_id,
                    semantic_result=None if assessment is None else assessment.result,
                    foundation_subchecks=tuple(subchecks),
                    result=result,
                    failure_effect=rule.failure_effect,
                    finding_refs=finding_ids.get(rule.rule_id, ()),
                    explanation=" ".join(explanations) or "All applicable components passed.",
                )
            )
        return tuple(results)

    def _foundation_payload_subcheck(
        self, connection, bundle, payload, rule_id, check_type, global_refs_ok
    ):
        if check_type is q.QualityCheckType.STRUCTURAL:
            passed = True
            codes = ["PAYLOAD_SCHEMA_VALID"]
            if rule_id == "SPEC-Q-019":
                passed = not any(
                    item["status"] == "open" and item["blocking"]
                    for item in payload["open_items"]
                )
                codes.append("NO_BLOCKING_OPEN_ITEMS" if passed else "BLOCKING_OPEN_ITEM")
            elif rule_id == "TECH-Q-014":
                passed = not self._contains_secret(payload)
                codes.append("SECRET_SCAN_CLEAN" if passed else "SECRET_SCAN_FAILED")
            elif rule_id == "TECH-Q-021":
                passed = not any(
                    item["status"] == "open" and item["blocking"]
                    for item in payload["review_obligations"]
                )
                codes.append("NO_BLOCKING_OBLIGATIONS" if passed else "BLOCKING_OBLIGATION")
            return passed, tuple(codes), "Foundation executed the rule's deterministic structural policy."
        if check_type is q.QualityCheckType.REFERENTIAL:
            passed = global_refs_ok
            codes = ["REFERENCE_GRAPH_RESOLVED" if passed else "UNRESOLVED_REFERENCE"]
            if passed and rule_id == "SPEC-Q-009":
                passed = all(
                    item["priority"] != "must"
                    or item["source_evidence_refs"]
                    or item["decision_refs"]
                    for item in payload["requirements"]
                )
                codes.append("MUST_PROVENANCE_PRESENT" if passed else "MUST_PROVENANCE_MISSING")
            if passed and rule_id == "SPEC-Q-020":
                passed = self._spec_membership_complete(payload)
                codes.append("PACKAGE_MEMBERSHIP_COMPLETE" if passed else "ORPHAN_PACKAGE_ITEM")
            if passed and rule_id == "TECH-Q-003":
                passed = self._technical_copied_statements_match(bundle, payload)
                codes.append("COPIED_STATEMENTS_EQUAL" if passed else "COPIED_STATEMENT_MISMATCH")
            return passed, tuple(codes), "Foundation resolved identities, versions, and exact reference bindings."
        if check_type is q.QualityCheckType.AUTHORITY:
            passed = self._payload_authority_valid(connection, bundle, payload, rule_id)
            return passed, ("AUTHORITY_VALID" if passed else "AUTHORITY_INVALID",), "Foundation validated applicable actor and delegation authority."
        if check_type is q.QualityCheckType.HUMAN:
            passed = self._payload_human_bindings_valid(connection, bundle, payload, rule_id)
            return passed, ("HUMAN_BINDING_VALID" if passed else "HUMAN_BINDING_INVALID",), "Foundation resolved the exact prior human ceremony required by this rule."
        raise AssertionError("semantic checks are never executed as Foundation subchecks")

    def _payload_references_resolve(self, connection, bundle, payload) -> bool:
        owned = set(self._artifact_identity_kinds(payload))
        snapshot = c.FoundationSemanticSnapshot.model_validate_json(
            bundle.canonical_semantic_snapshot_json
        )
        allowed = set(owned)
        for collection, id_field in (
            (snapshot.evidence, "evidence_id"), (snapshot.problems, "problem_id"),
            (snapshot.questions, "question_id"), (snapshot.facts, "fact_id"),
            (snapshot.decisions, "decision_id"),
            (snapshot.evidence_findings, "finding_id"),
            (snapshot.revision_requests, "revision_request_id"),
        ):
            allowed.update(str(getattr(item, id_field)) for item in collection)
        allowed.update(str(item.source_id) for item in bundle.sources)
        allowed.update(
            connection.execute(
                select(case_participants.c.actor_id).where(
                    case_participants.c.case_id == str(bundle.case_id)
                )
            ).scalars()
        )
        if bundle.confirmed_spec is not None:
            allowed.update(
                self._artifact_identity_kinds(
                    json.loads(bundle.confirmed_spec.canonical_payload_json)
                )
            )
        return not validate_artifact_reference_graph(
            payload, allowed_reference_ids=allowed
        )

    @staticmethod
    def _spec_membership_complete(payload) -> bool:
        members = {key: set() for key in ("requirement_refs", "decision_refs", "acceptance_check_refs")}
        for item in payload["package_items"]:
            for key in members:
                members[key].update(item[key])
        return (
            members["requirement_refs"] == {item["id"] for item in payload["requirements"]}
            and members["decision_refs"] == {item["id"] for item in payload["decisions"]}
            and members["acceptance_check_refs"] == {item["id"] for item in payload["acceptance_checks"]}
        )

    @staticmethod
    def _technical_copied_statements_match(bundle, payload) -> bool:
        if bundle.confirmed_spec is None:
            return False
        spec = json.loads(bundle.confirmed_spec.canonical_payload_json)
        try:
            return all(
                resolve_payload_pointer(spec, value["spec_json_pointer"]) == value["value"]
                for value in payload["contract_intent"].values()
                if isinstance(value, dict) and "spec_json_pointer" in value
            )
        except (KeyError, IndexError, TypeError, ValueError):
            return False

    def _payload_authority_valid(self, connection, bundle, payload, rule_id) -> bool:
        if rule_id.startswith("SPEC-"):
            if rule_id in {"SPEC-Q-004", "SPEC-Q-010", "SPEC-Q-011"}:
                return self._payload_human_bindings_valid(connection, bundle, payload, "SPEC-Q-010")
            return not any(
                item["status"] == "open" and item["blocking"]
                for item in payload.get("open_items", [])
            )
        if rule_id == "TECH-Q-021":
            return not any(
                item["status"] == "open" and item["blocking"]
                for item in payload.get("review_obligations", [])
            )
        if rule_id == "TECH-Q-020":
            return not any(
                item["status"] == "accepted" and not item["authority_domain"].strip()
                for item in payload.get("engineering_decisions", [])
            )
        return True

    def _payload_human_bindings_valid(self, connection, bundle, payload, rule_id) -> bool:
        if rule_id != "SPEC-Q-010":
            return True
        semantic_records = WORKSHOP_PROTOCOL_TABLES["workshop_semantic_records"]
        decision_views = WORKSHOP_PROTOCOL_TABLES["workshop_decision_views"]
        transcript_table = WORKSHOP_PROTOCOL_TABLES["workshop_final_transcripts"]
        for decision in payload.get("decisions", []):
            if decision["status"] != "confirmed":
                continue
            binding = decision["confirmation_binding"]
            semantic = connection.execute(
                select(semantic_records).where(
                    semantic_records.c.case_id == str(bundle.case_id),
                    semantic_records.c.foundation_id == binding["confirmed_decision_id"],
                    semantic_records.c.record_version == binding["confirmed_decision_version"],
                    semantic_records.c.entity_kind == "DECISION",
                    semantic_records.c.status == c.SemanticRecordStatus.CONFIRMED.value,
                )
            ).mappings().one_or_none()
            view = connection.execute(
                select(decision_views).where(
                    decision_views.c.case_id == str(bundle.case_id),
                    decision_views.c.view_id == binding["decision_batch_view_id"],
                    decision_views.c.view_hash == binding["decision_batch_view_hash"],
                )
            ).mappings().one_or_none()
            transcript = connection.execute(
                select(transcript_table).where(
                    transcript_table.c.case_id == str(bundle.case_id),
                    transcript_table.c.event_id == binding["transcript_event_id"],
                )
            ).mappings().one_or_none()
            if semantic is None or view is None or transcript is None:
                return False
            items = json.loads(view["view_json"])["items"]
            if not any(
                item["review_item_id"] == binding["review_item_id"]
                and item["pending_decision_id"] == binding["confirmed_decision_id"]
                and item["pending_decision_version"] == binding["confirmed_decision_version"]
                for item in items
            ):
                return False
            if binding["confirmed_case_revision"] > bundle.based_on_case_revision:
                return False
        return True

    @staticmethod
    def _contains_secret(value: Any) -> bool:
        if isinstance(value, dict):
            return any(ArtifactQualityFoundationMixin._contains_secret(item) for item in value.values())
        if isinstance(value, list):
            return any(ArtifactQualityFoundationMixin._contains_secret(item) for item in value)
        return isinstance(value, str) and any(pattern.search(value) for pattern in _SECRET_PATTERNS)

    @staticmethod
    def _quality_outcome(results) -> q.AuditOutcome:
        failed = [item for item in results if item.result is q.ComponentResult.FAIL]
        if not failed:
            return q.AuditOutcome.PASS
        effects = {item.failure_effect for item in failed}
        if q.FailureEffect.REJECT_MUTATION in effects:
            return q.AuditOutcome.REJECTED
        if q.FailureEffect.BLOCKED in effects:
            return q.AuditOutcome.BLOCKED
        if q.FailureEffect.NEEDS_CLARIFICATION in effects:
            return q.AuditOutcome.NEEDS_CLARIFICATION
        return q.AuditOutcome.CONDITIONAL

    def _quality_governance(
        self, *, artifact_type, governance, combined, outcome, admitted_findings
    ):
        rule_results = []
        for item in combined:
            if item.result is q.ComponentResult.PENDING:
                continue
            result = (
                "fail" if item.result is q.ComponentResult.FAIL
                else "not_applicable"
                if item.semantic_result is q.SemanticAssessmentResult.NOT_APPLICABLE
                else "pass"
            )
            rule_results.append(
                {
                    "rule_id": item.rule_id,
                    "result": result,
                    "finding_refs": [str(value) for value in item.finding_refs],
                    "explanation": item.explanation,
                }
            )
        blockers = [
            str(finding.finding_id)
            for finding in admitted_findings
            if finding.severity in {"HIGH", "CRITICAL"}
        ]
        if artifact_type is q.ArtifactType.SPEC_PACKAGE:
            audit = governance["readiness_audit"]
            audit.update(
                run_at=_instant(self.now()),
                rule_results=rule_results,
                blocker_refs=blockers,
                result=(
                    "needs_clarification"
                    if outcome in {q.AuditOutcome.PASS, q.AuditOutcome.NEEDS_CLARIFICATION}
                    else "blocked"
                ),
            )
            readiness = (
                "READY" if outcome is q.AuditOutcome.PASS
                else "NEEDS_CLARIFICATION" if outcome is q.AuditOutcome.NEEDS_CLARIFICATION
                else "BLOCKED"
            )
            for item in governance["item_governance"]:
                item["readiness"] = readiness
                item["review_obligation"] = (
                    "NONE" if outcome is q.AuditOutcome.PASS else "DECISION_REQUIRED"
                )
        else:
            readiness = governance["contract_readiness"]
            readiness.update(
                run_at=_instant(self.now()),
                rule_results=rule_results,
                blocker_refs=blockers,
                result=(
                    "conditional" if outcome is q.AuditOutcome.CONDITIONAL else "blocked"
                ),
                downstream_handoff_allowed=False,
                handoff_reasons=[
                    "Exact technical approval and handoff authorization are still pending."
                    if outcome is q.AuditOutcome.PASS
                    else "One or more Artifact Quality Audit rules failed."
                ],
            )
        return governance

    def _set_case_quality_state(self, connection, case_id, outcome, *, awaiting_confirmation):
        table = WORKSHOP_PROTOCOL_TABLES["workshop_protocol_cases"]
        if outcome is q.AuditOutcome.PASS and awaiting_confirmation:
            readiness = c.Readiness.NEEDS_CLARIFICATION
            obligation = c.ReviewObligation.DECISION_REQUIRED
        elif outcome is q.AuditOutcome.NEEDS_CLARIFICATION:
            readiness = c.Readiness.NEEDS_CLARIFICATION
            obligation = c.ReviewObligation.DECISION_REQUIRED
        else:
            readiness = c.Readiness.BLOCKED
            obligation = c.ReviewObligation.DECISION_REQUIRED
        connection.execute(
            update(table)
            .where(table.c.case_id == str(case_id))
            .values(
                readiness=readiness.value,
                review_obligation=obligation.value,
                updated_at=_instant(self.now()),
            )
        )

    def quality_audit_for_record(
        self, connection, record: dict[str, Any], *, require_pass: bool = False
    ) -> dict[str, Any]:
        from .workshop_protocol import FoundationProtocolError

        audits = ARTIFACT_QUALITY_TABLES["workshop_artifact_quality_audits"]
        row = connection.execute(
            select(audits).where(
                audits.c.case_id == record["case_id"],
                audits.c.artifact_id == record["artifact_id"],
                audits.c.artifact_version == record["artifact_version"],
                audits.c.payload_hash == record["payload_hash"],
                audits.c.resulting_record_revision <= record["record_revision"],
                audits.c.state == q.AuditState.ADMITTED.value,
            )
        ).mappings().one_or_none()
        if row is None:
            raise FoundationProtocolError(c.FoundationRejectionCode.INVALID_TRANSITION)
        if require_pass and row["outcome"] != q.AuditOutcome.PASS.value:
            raise FoundationProtocolError(c.FoundationRejectionCode.INVALID_TRANSITION)
        return dict(row)

    @staticmethod
    def _review_binding(record, view_id, view_hash, view_mode, generated_at):
        return {
            "profile_id": "artifact-review-view",
            "profile_version": "3.0.0",
            "view_id": str(view_id),
            "mode": view_mode.lower(),
            "artifact_id": record["artifact_id"],
            "artifact_key": record["artifact_key"],
            "artifact_version": record["artifact_version"],
            "record_revision": record["record_revision"],
            "payload_hash": record["payload_hash"],
            "view_hash": view_hash,
            "generated_at": _instant(generated_at),
        }

    def governance_with_projection_pass(
        self,
        *,
        record,
        governance,
        view_id,
        view_hash,
        view_mode,
        generated_at,
    ):
        prefix = "SPEC" if record["artifact_type"] == "SPEC_PACKAGE" else "TECH"
        result = {
            "rule_id": f"{prefix}-Q-025",
            "result": "pass",
            "finding_refs": [],
            "explanation": (
                "Foundation validated the exact immutable projection binding, canonical "
                "pointers, completeness flag, artifact payload hash, and view hash."
            ),
        }
        target = (
            governance["readiness_audit"]
            if record["artifact_type"] == "SPEC_PACKAGE"
            else governance["contract_readiness"]
        )
        target["rule_results"] = [
            item for item in target["rule_results"] if item["rule_id"] != result["rule_id"]
        ] + [result]
        governance["latest_review_view"] = self._review_binding(
            record, view_id, view_hash, view_mode, generated_at
        )
        return governance

    def record_projection_quality_phase(
        self, connection, *, audit, record, view_id, view_hash
    ) -> None:
        phases = ARTIFACT_QUALITY_TABLES["workshop_artifact_quality_gate_phases"]
        prefix = "SPEC" if record["artifact_type"] == "SPEC_PACKAGE" else "TECH"
        connection.execute(
            insert(phases).values(
                phase_id=str(self.new_id()),
                audit_id=audit["audit_id"],
                case_id=record["case_id"],
                phase="REVIEW_PROJECTION",
                rule_id=f"{prefix}-Q-025",
                artifact_id=record["artifact_id"],
                artifact_version=record["artifact_version"],
                record_revision=record["record_revision"],
                payload_hash=record["payload_hash"],
                view_id=str(view_id),
                view_hash=view_hash,
                result="PASS",
                evidence_json=_json(
                    {
                        "view_id": str(view_id),
                        "view_hash": view_hash,
                        "record_revision": record["record_revision"],
                        "payload_hash": record["payload_hash"],
                        "all_material_items_included": True,
                    }
                ),
                created_at=_instant(self.now()),
            )
        )

    def require_quality_confirmation_gate(self, connection, artifact, view, command):
        from .workshop_protocol import FoundationProtocolError

        acceptance = command.residual_quality_risk_acceptance
        audit = self.quality_audit_for_record(
            connection, artifact, require_pass=acceptance is None
        )
        phases = ARTIFACT_QUALITY_TABLES["workshop_artifact_quality_gate_phases"]
        phase = connection.execute(
            select(phases).where(
                phases.c.audit_id == audit["audit_id"],
                phases.c.phase == "REVIEW_PROJECTION",
                phases.c.artifact_id == artifact["artifact_id"],
                phases.c.artifact_version == artifact["artifact_version"],
                phases.c.record_revision == artifact["record_revision"],
                phases.c.payload_hash == artifact["payload_hash"],
                phases.c.view_id == view["view_id"],
                phases.c.view_hash == view["view_hash"],
                phases.c.result == "PASS",
            )
        ).mappings().one_or_none()
        if phase is None:
            raise FoundationProtocolError(c.FoundationRejectionCode.CONFIRMATION_BINDING_FAILED)
        governance = json.loads(artifact["governance_json"])
        target = (
            governance["readiness_audit"]
            if artifact["artifact_type"] == "SPEC_PACKAGE"
            else governance["contract_readiness"]
        )
        prefix = "SPEC" if artifact["artifact_type"] == "SPEC_PACKAGE" else "TECH"
        expected = {f"{prefix}-Q-{index:03d}" for index in range(1, 27)} - {
            f"{prefix}-Q-024"
        }
        actual = {
            item["rule_id"]
            for item in target["rule_results"]
            if item["result"] in {"pass", "not_applicable"}
        }
        failed_in_governance = {
            item["rule_id"]
            for item in target["rule_results"]
            if item["result"] == "fail"
        }
        if acceptance is None:
            if actual != expected or failed_in_governance:
                raise FoundationProtocolError(c.FoundationRejectionCode.INVALID_TRANSITION)
            return audit, governance, None

        if (
            artifact["artifact_type"] != "SPEC_PACKAGE"
            or audit["outcome"]
            not in {
                q.AuditOutcome.BLOCKED.value,
                q.AuditOutcome.NEEDS_CLARIFICATION.value,
            }
            or str(acceptance.audit_id) != audit["audit_id"]
        ):
            raise FoundationProtocolError(c.FoundationRejectionCode.CONFIRMATION_BINDING_FAILED)
        combined = q.ArtifactQualityAuditReceipt.model_validate_json(
            audit["receipt_json"]
        ).combined_rule_results
        failed = tuple(
            sorted(
                item.rule_id
                for item in combined
                if item.result is q.ComponentResult.FAIL
            )
        )
        pending = {
            item.rule_id
            for item in combined
            if item.result is q.ComponentResult.PENDING
        }
        expected_pending = {"SPEC-Q-024", "SPEC-Q-025"}
        if (
            not failed
            or failed != acceptance.accepted_failed_rule_ids
            or pending != expected_pending
            or failed_in_governance != set(failed)
            or actual != expected - set(failed)
        ):
            raise FoundationProtocolError(c.FoundationRejectionCode.CONFIRMATION_BINDING_FAILED)
        finding_ids = tuple(
            sorted(
                {
                    finding_id
                    for item in combined
                    if item.rule_id in failed
                    for finding_id in item.finding_refs
                },
                key=str,
            )
        )
        return audit, governance, {
            "policy_id": acceptance.policy_id,
            "audit_id": str(acceptance.audit_id),
            "audit_outcome": audit["outcome"],
            "accepted_failed_rule_ids": list(failed),
            "approved_finding_ids": [str(value) for value in finding_ids],
            "acceptance_statement": acceptance.acceptance_statement,
        }

    def finalize_confirmation_quality(
        self,
        connection,
        *,
        audit,
        governance,
        artifact,
        view,
        command,
        confirmed_case_revision,
        residual_risk,
    ):
        prefix = "SPEC" if artifact["artifact_type"] == "SPEC_PACKAGE" else "TECH"
        authority_validation_id = self.new_id()
        q24 = {
            "rule_id": f"{prefix}-Q-024",
            "result": "pass",
            "finding_refs": [],
            "explanation": (
                "Foundation atomically validated the current audit, exact immutable review "
                "projection, participant transcript, actor authority, artifact binding, and "
                + (
                    "the explicit residual-quality risk acceptance."
                    if residual_risk is not None
                    else "the no-failure quality confirmation gate."
                )
            ),
        }
        target = (
            governance["readiness_audit"]
            if artifact["artifact_type"] == "SPEC_PACKAGE"
            else governance["contract_readiness"]
        )
        target["rule_results"] = [
            item for item in target["rule_results"] if item["rule_id"] != q24["rule_id"]
        ] + [q24]
        review_binding = self._review_binding(
            artifact,
            UUID(view["view_id"]),
            view["view_hash"],
            view["view_mode"],
            datetime.fromisoformat(view["generated_at"].replace("Z", "+00:00")),
        )
        now = self.now()
        if artifact["artifact_type"] == "SPEC_PACKAGE":
            target["result"] = "pass"
            governance["authority_validation_ids"] = sorted(
                {*governance["authority_validation_ids"], str(authority_validation_id)}
            )
            for item in governance["item_governance"]:
                item["readiness"] = "READY"
                item["review_obligation"] = "NONE"
            governance["confirmation"] = {
                "confirmation_id": str(command.binding.confirmation_id),
                "artifact_id": artifact["artifact_id"],
                "artifact_key": artifact["artifact_key"],
                "artifact_version": artifact["artifact_version"],
                "record_revision": artifact["record_revision"],
                "confirmed_case_revision": confirmed_case_revision,
                "payload_hash": artifact["payload_hash"],
                "confirmer_actor_ref": str(command.acting_actor_id),
                "authority_validation_id": str(authority_validation_id),
                "transcript_ref": str(command.confirmation_transcript_event_id),
                "review_view": review_binding,
                "confirmed_at": _instant(now),
                "exceptions": (
                    residual_risk["approved_finding_ids"]
                    if residual_risk is not None
                    else [str(value) for value in command.approved_exception_ids]
                ),
            }
        else:
            target.update(
                result="ready",
                downstream_handoff_allowed=True,
                handoff_reasons=[],
            )
            governance["approvals"].append(
                {
                    "approval_id": str(command.binding.confirmation_id),
                    "domain": "technical",
                    "actor_ref": str(command.acting_actor_id),
                    "decision": "approved",
                    "authority_validation_id": str(authority_validation_id),
                    "review_view": review_binding,
                    "approved_at": _instant(now),
                }
            )
            governance["handoff_authorization"] = {
                "authorization_id": str(self.new_id()),
                "artifact_id": artifact["artifact_id"],
                "artifact_key": artifact["artifact_key"],
                "artifact_version": artifact["artifact_version"],
                "record_revision": artifact["record_revision"],
                "payload_hash": artifact["payload_hash"],
                "case_revision": confirmed_case_revision,
                "allowed": True,
                "restrictions": [],
                "authorized_at": _instant(now),
            }
        phases = ARTIFACT_QUALITY_TABLES["workshop_artifact_quality_gate_phases"]
        if residual_risk is not None:
            connection.execute(
                insert(phases).values(
                    phase_id=str(self.new_id()),
                    audit_id=audit["audit_id"],
                    case_id=artifact["case_id"],
                    phase="RESIDUAL_RISK_ACCEPTANCE",
                    rule_id=q24["rule_id"],
                    artifact_id=artifact["artifact_id"],
                    artifact_version=artifact["artifact_version"],
                    record_revision=artifact["record_revision"],
                    payload_hash=artifact["payload_hash"],
                    view_id=view["view_id"],
                    view_hash=view["view_hash"],
                    result="PASS",
                    evidence_json=_json(
                        {
                            **residual_risk,
                            "confirmation_id": str(command.binding.confirmation_id),
                            "actor_id": str(command.acting_actor_id),
                            "transcript_event_id": str(
                                command.confirmation_transcript_event_id
                            ),
                            "artifact_id": artifact["artifact_id"],
                            "artifact_version": artifact["artifact_version"],
                            "record_revision": artifact["record_revision"],
                            "payload_hash": artifact["payload_hash"],
                            "view_id": view["view_id"],
                            "view_hash": view["view_hash"],
                        }
                    ),
                    created_at=_instant(now),
                )
            )
        connection.execute(
            insert(phases).values(
                phase_id=str(self.new_id()),
                audit_id=audit["audit_id"],
                case_id=artifact["case_id"],
                phase="CONFIRMATION_HANDOFF",
                rule_id=q24["rule_id"],
                artifact_id=artifact["artifact_id"],
                artifact_version=artifact["artifact_version"],
                record_revision=artifact["record_revision"],
                payload_hash=artifact["payload_hash"],
                view_id=view["view_id"],
                view_hash=view["view_hash"],
                result="PASS",
                evidence_json=_json(
                    {
                        "confirmation_id": str(command.binding.confirmation_id),
                        "actor_id": str(command.acting_actor_id),
                        "authority_validation_id": str(authority_validation_id),
                        "transcript_event_id": str(command.confirmation_transcript_event_id),
                        "confirmed_case_revision": confirmed_case_revision,
                    }
                ),
                created_at=_instant(now),
            )
        )
        case_table = WORKSHOP_PROTOCOL_TABLES["workshop_protocol_cases"]
        connection.execute(
            update(case_table)
            .where(case_table.c.case_id == artifact["case_id"])
            .values(
                readiness=c.Readiness.READY.value,
                review_obligation=c.ReviewObligation.NONE.value,
                updated_at=_instant(now),
            )
        )
        return governance

    @staticmethod
    def _quality_reject(code):
        from .workshop_protocol import FoundationProtocolError

        raise FoundationProtocolError(code)
