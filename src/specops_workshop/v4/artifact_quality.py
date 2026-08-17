"""Provider-neutral construction and validation for artifact quality audits."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable
from uuid import UUID

import specops_contracts
from specops_contracts import artifact_quality_v1 as q
from specops_contracts import workshop_v1 as c
from specops_contracts.canonical import canonical_bytes, domain_hash, payload_hash, transcript_hash

from .openai_adapter import ProviderSourceUpload


ZERO_HASH = "sha256:" + "0" * 64
QUALITY_CONTRACT_PATH = (
    Path(specops_contracts.__file__).resolve().parent / "semantic-quality-contract.yaml"
)


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def quality_contract_hash(path: Path = QUALITY_CONTRACT_PATH) -> str:
    return _sha256_bytes(path.read_bytes())


def _yaml_scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def load_quality_rules(
    artifact_type: q.ArtifactType,
    path: Path = QUALITY_CONTRACT_PATH,
) -> tuple[q.QualityRuleManifestEntry, ...]:
    """Parse the deliberately regular normative rule list without a YAML runtime dependency."""

    section = (
        "spec_package_rules:"
        if artifact_type is q.ArtifactType.SPEC_PACKAGE
        else "technical_contract_rules:"
    )
    lines = path.read_text(encoding="utf-8").splitlines()
    try:
        index = lines.index(section) + 1
    except ValueError as exc:
        raise ValueError(f"quality contract omits {section}") from exc

    raw_rules: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    while index < len(lines):
        line = lines[index]
        if line and not line.startswith(" "):
            break
        if line.startswith("  - id: "):
            if current is not None:
                raw_rules.append(current)
            current = {"rule_id": _yaml_scalar(line.split(":", 1)[1])}
            index += 1
            continue
        if current is None or not line.startswith("    ") or line.startswith("      "):
            index += 1
            continue
        key, separator, raw_value = line.strip().partition(":")
        if not separator:
            raise ValueError(f"malformed quality-contract rule line: {line}")
        raw_value = raw_value.strip()
        if raw_value in {">-", "|", "|-"}:
            folded: list[str] = []
            index += 1
            while index < len(lines) and (
                lines[index].startswith("      ") or not lines[index].strip()
            ):
                if lines[index].strip():
                    folded.append(lines[index].strip())
                index += 1
            current[key] = " ".join(folded)
            continue
        if raw_value.startswith("[") and raw_value.endswith("]"):
            current[key] = tuple(
                _yaml_scalar(item) for item in raw_value[1:-1].split(",") if item.strip()
            )
        else:
            current[key] = _yaml_scalar(raw_value)
        index += 1
    if current is not None:
        raw_rules.append(current)
    if len(raw_rules) != 26:
        raise ValueError(f"expected 26 quality rules, found {len(raw_rules)}")
    for item in raw_rules:
        item["check_types"] = tuple(q.QualityCheckType(value) for value in item["check_types"])
        item["gate"] = q.QualityGate(item["gate"])
        item["primary_evaluator"] = q.PrimaryEvaluator(item["primary_evaluator"])
        item["failure_effect"] = q.FailureEffect(item["failure_effect"])
    return tuple(q.QualityRuleManifestEntry.model_validate(item) for item in raw_rules)


def source_set_hash(sources: Iterable[q.AuditSourceDocument]) -> str:
    return domain_hash(
        "SPECOPS:SOURCE_SET:v1",
        [
            {
                "source_id": str(item.source_id),
                "role": item.role.value,
                "version": item.version,
                "payload_hash": item.payload_hash,
            }
            for item in sources
        ],
    )


def transcript_manifest_hash(transcripts: Iterable[q.AuditTranscript]) -> str:
    return domain_hash(
        "SPECOPS:ARTIFACT_QUALITY_TRANSCRIPTS:v1",
        [
            {
                "event_id": str(item.event_id),
                "sequence_number": item.sequence_number,
                "transcript_artifact_id": str(item.transcript_artifact_id),
                "transcript_version": item.transcript_version,
                "transcript_hash": item.transcript_hash,
                "actor": item.actor.value,
                "speaker_actor_id": str(item.speaker_actor_id),
            }
            for item in transcripts
        ],
    )


def audit_scope_manifest_hash(value: q.ArtifactQualityAuditBundle | dict[str, Any]) -> str:
    material = (
        value.model_dump(mode="json", exclude_none=False)
        if isinstance(value, q.ArtifactQualityAuditBundle)
        else dict(value)
    )
    source_values = [
        item.model_dump(mode="json", exclude_none=False)
        if hasattr(item, "model_dump")
        else dict(item)
        for item in material["sources"]
    ]
    return domain_hash(
        "SPECOPS:ARTIFACT_QUALITY_SCOPE:v1",
        {
            "audit_id": material["audit_id"],
            "evaluator_run_id": material["evaluator_run_id"],
            "case_id": material["case_id"],
            "session_id": material["session_id"],
            "based_on_case_revision": material["based_on_case_revision"],
            "subject": material["subject"],
            "quality_contract": material["quality_contract"],
            "source_set_hash": material["source_set_hash"],
            "sources": [
                {key: item[key] for key in item if key != "complete_text"}
                for item in source_values
            ],
            "transcript_manifest_hash": material["transcript_manifest_hash"],
            "semantic_state_hash": material["semantic_state_hash"],
            "confirmed_spec": material["confirmed_spec"],
            "rule_manifest": material["rule_manifest"],
            "semantic_rule_ids": material["semantic_rule_ids"],
        },
    )


def audit_request_hash(value: q.ArtifactQualityAuditBundle | dict[str, Any]) -> str:
    material = (
        value.model_dump(mode="json", exclude_none=False)
        if isinstance(value, q.ArtifactQualityAuditBundle)
        else dict(value)
    )
    material.pop("request_hash", None)
    return domain_hash("SPECOPS:ARTIFACT_QUALITY_REQUEST:v1", material)


def build_audit_sources(
    sources: tuple[ProviderSourceUpload, ProviderSourceUpload],
) -> tuple[q.AuditSourceDocument, q.AuditSourceDocument]:
    """Project the exact two admitted sources into the evaluator contract."""

    return tuple(
        q.AuditSourceDocument(
            source_id=item.source.source_id,
            role=q.SourceRole(item.source.role.value),
            version=item.source.version,
            payload_hash=item.source.payload_hash,
            canonical_locator=item.source.canonical_locator,
            filename=item.source.filename,
            media_type=item.source.media_type,
            complete_text=item.content.decode("utf-8", errors="strict"),
        )
        for item in sources
    )


def build_audit_bundle(
    *,
    audit_id: UUID,
    evaluator_run_id: UUID,
    case_id: UUID,
    session_id: UUID,
    based_on_case_revision: int,
    artifact_record: dict[str, Any],
    sources: tuple[ProviderSourceUpload, ProviderSourceUpload],
    transcripts: tuple[c.TranscriptFinalizedEvent, ...],
    semantic_snapshot: c.FoundationSemanticSnapshot,
    semantic_quality_contract_hash: str,
    confirmed_spec: c.ConfirmedSpecSynthesisBinding | None,
) -> q.ArtifactQualityAuditBundle:
    artifact_type = q.ArtifactType(artifact_record["artifact_type"])
    audit_sources = build_audit_sources(sources)
    audit_transcripts = tuple(
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
        for item in transcripts
    )
    rules = load_quality_rules(artifact_type)
    confirmed = None
    if confirmed_spec is not None:
        confirmed = q.ConfirmedSpecAuditBinding(
            artifact_id=confirmed_spec.foundation_artifact_id,
            artifact_key=confirmed_spec.artifact_key,
            artifact_version=confirmed_spec.artifact_version,
            record_revision=confirmed_spec.record_revision,
            payload_hash=confirmed_spec.payload_hash,
            confirmation_id=confirmed_spec.confirmation_id,
            confirmed_case_revision=confirmed_spec.confirmed_case_revision,
            canonical_payload_json=confirmed_spec.canonical_payload_json,
        )
    snapshot_json = canonical_bytes(semantic_snapshot).decode("utf-8")
    values: dict[str, Any] = {
        "protocol_version": q.PROTOCOL_VERSION,
        "audit_id": audit_id,
        "evaluator_run_id": evaluator_run_id,
        "case_id": case_id,
        "session_id": session_id,
        "based_on_case_revision": based_on_case_revision,
        "subject": q.ArtifactAuditSubject(
            artifact_type=artifact_type,
            artifact_id=UUID(artifact_record["artifact_id"]),
            artifact_key=artifact_record["artifact_key"],
            artifact_version=artifact_record["artifact_version"],
            record_revision=artifact_record["record_revision"],
            payload_hash=artifact_record["payload_hash"],
            canonical_payload_json=artifact_record["payload_json"],
        ),
        "quality_contract": q.QualityContractBinding(
            contract_id="SEMANTIC-QUALITY-CONTRACT",
            version="2.2.0",
            content_hash=semantic_quality_contract_hash,
        ),
        "source_set_hash": source_set_hash(audit_sources),
        "sources": audit_sources,
        "transcript_count": len(audit_transcripts),
        "first_transcript_sequence": audit_transcripts[0].sequence_number if audit_transcripts else None,
        "last_transcript_sequence": audit_transcripts[-1].sequence_number if audit_transcripts else None,
        "transcript_manifest_hash": transcript_manifest_hash(audit_transcripts),
        "transcripts": audit_transcripts,
        "semantic_state_hash": domain_hash(
            "SPECOPS:SEMANTIC_STATE:v1", semantic_snapshot.model_dump(mode="json")
        ),
        "canonical_semantic_snapshot_json": snapshot_json,
        "confirmed_spec": confirmed,
        "rule_manifest": rules,
        "semantic_rule_ids": tuple(
            item.rule_id for item in rules if q.QualityCheckType.SEMANTIC in item.check_types
        ),
        "audit_scope_manifest_hash": ZERO_HASH,
        "request_hash": ZERO_HASH,
    }
    values["audit_scope_manifest_hash"] = audit_scope_manifest_hash(values)
    values["request_hash"] = audit_request_hash(values)
    bundle = q.ArtifactQualityAuditBundle.model_validate(values)
    validate_audit_bundle(bundle)
    return bundle


def validate_audit_bundle(
    bundle: q.ArtifactQualityAuditBundle,
    *,
    expected_quality_hash: str | None = None,
) -> None:
    if expected_quality_hash is None:
        expected_quality_hash = quality_contract_hash()
    if bundle.quality_contract.content_hash != expected_quality_hash:
        raise ValueError("quality contract hash does not match the generated runtime resource")
    payload = json.loads(bundle.subject.canonical_payload_json)
    if canonical_bytes(payload).decode("utf-8") != bundle.subject.canonical_payload_json:
        raise ValueError("audit subject payload JSON is not RFC 8785 canonical JSON")
    if payload_hash(payload) != bundle.subject.payload_hash:
        raise ValueError("audit subject payload hash is invalid")
    for source in bundle.sources:
        if _sha256_bytes(source.complete_text.encode("utf-8")) != source.payload_hash:
            raise ValueError("audit source text does not match its Foundation hash")
    if source_set_hash(bundle.sources) != bundle.source_set_hash:
        raise ValueError("audit source-set hash is invalid")
    for transcript in bundle.transcripts:
        if transcript_hash(transcript.complete_text) != transcript.transcript_hash:
            raise ValueError("audit transcript text does not match its Foundation hash")
    if transcript_manifest_hash(bundle.transcripts) != bundle.transcript_manifest_hash:
        raise ValueError("audit transcript manifest hash is invalid")
    semantic = json.loads(bundle.canonical_semantic_snapshot_json)
    if canonical_bytes(semantic).decode("utf-8") != bundle.canonical_semantic_snapshot_json:
        raise ValueError("audit semantic snapshot is not canonical JSON")
    if domain_hash("SPECOPS:SEMANTIC_STATE:v1", semantic) != bundle.semantic_state_hash:
        raise ValueError("audit semantic-state hash is invalid")
    if bundle.rule_manifest != load_quality_rules(bundle.subject.artifact_type):
        raise ValueError("audit rule manifest differs from the generated quality contract")
    if audit_scope_manifest_hash(bundle) != bundle.audit_scope_manifest_hash:
        raise ValueError("audit scope manifest hash is invalid")
    if audit_request_hash(bundle) != bundle.request_hash:
        raise ValueError("artifact quality request hash is invalid")


def resolve_payload_pointer(payload: Any, pointer: str) -> Any:
    current = payload
    if pointer == "":
        return current
    for raw in pointer[1:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        current = current[int(token)] if isinstance(current, list) else current[token]
    return current
