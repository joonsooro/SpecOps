from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from specops_workflow.enums import SourceArtifactType
from specops_workflow.models import LineRange, SourceArtifactIdentity, SourceRef
from specops_workshop.bootstrap import CASE_ID, TECHNICAL_SOURCE_ID
from specops_workshop.evidence import (
    AliasMaterializer,
    DeterministicEvidenceRetriever,
    EvidenceBinding,
    EvidenceIndex,
    EvidenceIndexer,
    EvidenceSelectionGate,
    EvidenceSourceRole,
    RegisteredMarkdownSnapshot,
    RetrievalOutcome,
)
from specops_workshop.sources import SourceCatalog, SourceName


ROOT = Path("/Users/rudinro/Desktop/SpecOps/Spec_Eng")
OTHER_CASE_ID = UUID("00000000-0000-4000-8000-000000000021")
NOW = datetime(2026, 8, 9, 20, tzinfo=timezone.utc)


def registered_identity(
    content: str,
    *,
    case_id: UUID = CASE_ID,
    artifact_id: UUID = TECHNICAL_SOURCE_ID,
    version: int = 1,
    registered: bool = True,
    media_type: str = "text/markdown",
) -> SourceArtifactIdentity:
    return SourceArtifactIdentity(
        artifact_id=artifact_id,
        case_id=case_id,
        type=SourceArtifactType.TECHNICAL_CONTRACT,
        version=version,
        media_type=media_type,
        canonical_locator="/registered/dev-lead-technical-spec.md",
        content_hash=sha256(content.encode("utf-8")).hexdigest(),
        registered_at=NOW if registered else None,
    )


def snapshot(
    content: str | None = None,
    *,
    case_id: UUID = CASE_ID,
    version: int = 1,
) -> RegisteredMarkdownSnapshot:
    value = (
        content
        if content is not None
        else SourceCatalog(ROOT).read_text(SourceName.TECHNICAL_SPEC)
    )
    return RegisteredMarkdownSnapshot.from_registered(
        EvidenceSourceRole.TECHNICAL_SPEC,
        registered_identity(value, case_id=case_id, version=version),
        value,
    )


def evidence_index(
    content: str | None = None,
    *,
    case_id: UUID = CASE_ID,
    version: int = 1,
) -> tuple[EvidenceIndex, RegisteredMarkdownSnapshot]:
    value = snapshot(content, case_id=case_id, version=version)
    return EvidenceIndexer().build(case_id=case_id, snapshots=(value,)), value


def binding(index: EvidenceIndex, alias: str) -> EvidenceBinding:
    return next(value for value in index.bindings if value.alias == alias)


def test_ar_ev_001_exact_agenda_units_aliases_and_authoritative_lines():
    index, current = evidence_index()
    expected = {
        "technical-agenda:d-01": 527,
        "technical-agenda:d-02": 528,
        "technical-agenda:d-05": 531,
    }
    materializer = AliasMaterializer(index)
    source_lines = current.lines
    for alias, line_number in expected.items():
        unit = index.unit_for(alias)
        exact_binding = binding(index, alias)
        assert unit.text == source_lines[line_number - 1]
        assert unit.display_label.startswith(alias.rsplit(":", 1)[1].upper())
        assert unit.heading_path[-1] == "16. PM-Engineering decision agenda"
        assert exact_binding.location == LineRange(start=line_number, end=line_number)
        assert materializer.materialize(
            case_id=CASE_ID,
            requested_aliases=(alias,),
            selected_aliases=(alias,),
            current_snapshots=(current,),
        ) == (
            exact_binding_to_ref(exact_binding),
        )

    provider_shape = index.unit_for("technical-agenda:d-01").model_dump()
    assert set(provider_shape) == {
        "alias", "source_role", "unit_kind", "heading_path", "display_label", "text"
    }
    assert not {
        "case_id", "artifact_id", "version", "content_hash", "location", "line_range"
    } & set(provider_shape)
    assert "bindings" not in index.model_dump()
    assert "source_seals" not in index.model_dump()
    assert "content" not in current.model_dump()


def exact_binding_to_ref(value: EvidenceBinding) -> SourceRef:
    return SourceRef(
        artifact_id=value.artifact_id,
        version=value.version,
        content_hash=value.content_hash,
        location=value.location,
    )


def test_ar_ev_002_reorder_preserves_alias_and_moves_exact_line():
    original = SourceCatalog(ROOT).read_text(SourceName.TECHNICAL_SPEC)
    lines = original.splitlines()
    d01, d05 = lines[526], lines[530]
    lines[526], lines[530] = d05, d01
    reordered = "\n".join(lines) + ("\n" if original.endswith("\n") else "")
    index, current = evidence_index(reordered, version=2)

    assert binding(index, "technical-agenda:d-05").location == LineRange(
        start=527, end=527
    )
    assert binding(index, "technical-agenda:d-01").location == LineRange(
        start=531, end=531
    )
    refs = AliasMaterializer(index).materialize(
        case_id=CASE_ID,
        requested_aliases=("technical-agenda:d-05", "technical-agenda:d-01"),
        selected_aliases=("technical-agenda:d-01", "technical-agenda:d-05"),
        current_snapshots=(current,),
    )
    assert tuple(ref.location.start for ref in refs) == (527, 531)
    assert all(ref.version == 2 and ref.content_hash == current.content_hash for ref in refs)


def test_ar_ev_002_hash_version_snapshot_range_registration_and_case_fail_closed():
    index, original = evidence_index()
    materializer = AliasMaterializer(index)
    changed_content = original.content.replace(
        "Canonical business date", "Canonical order date", 1
    )
    changed_same_version = snapshot(changed_content)
    changed_version = snapshot(original.content, version=2)
    for changed in (changed_same_version, changed_version):
        with pytest.raises(ValueError, match="stale evidence source snapshot"):
            materializer.materialize(
                case_id=CASE_ID,
                requested_aliases=("technical-agenda:d-01",),
                selected_aliases=("technical-agenda:d-01",),
                current_snapshots=(changed,),
            )

    wrong_hash_identity = registered_identity(original.content).model_copy(
        update={"content_hash": "0" * 64}
    )
    with pytest.raises(ValidationError, match="content hash mismatch"):
        RegisteredMarkdownSnapshot.from_registered(
            EvidenceSourceRole.TECHNICAL_SPEC, wrong_hash_identity, original.content
        )
    with pytest.raises(ValueError, match="not registered"):
        RegisteredMarkdownSnapshot.from_registered(
            EvidenceSourceRole.TECHNICAL_SPEC,
            registered_identity(original.content, registered=False),
            original.content,
        )
    with pytest.raises(ValidationError, match="text/markdown"):
        RegisteredMarkdownSnapshot.from_registered(
            EvidenceSourceRole.TECHNICAL_SPEC,
            registered_identity(original.content, media_type="application/json"),
            original.content,
        )
    with pytest.raises(ValidationError, match="start must not exceed end"):
        LineRange(start=2, end=1)

    first_binding = index.bindings[0]
    bad_binding = first_binding.model_copy(
        update={
            "location": LineRange(
                start=len(original.lines) + 1, end=len(original.lines) + 1
            )
        }
    )
    with pytest.raises(ValidationError, match="range exceeds registered snapshot"):
        EvidenceIndex(
            case_id=index.case_id,
            units=index.units,
            bindings=(bad_binding, *index.bindings[1:]),
            source_seals=index.source_seals,
        )
    with pytest.raises(ValidationError, match="duplicate evidence alias"):
        EvidenceIndex(
            case_id=index.case_id,
            units=(*index.units, index.units[0]),
            bindings=(*index.bindings, index.bindings[0]),
            source_seals=index.source_seals,
        )
    duplicated_row = original.content.replace(
        "| D-02 | Timezone configuration",
        "| D-01 | Timezone configuration",
        1,
    )
    with pytest.raises(ValidationError, match="duplicate evidence alias"):
        evidence_index(duplicated_row)
    with pytest.raises(ValueError, match="cross-case source snapshot"):
        EvidenceIndexer().build(case_id=OTHER_CASE_ID, snapshots=(original,))


@pytest.mark.parametrize(
    ("turn", "expected", "feature_prefix"),
    [
        ("Resolve D-01 before contract sign-off.", "technical-agenda:d-01", "exact-id:"),
        (
            "We need the timezone configuration from the decision agenda.",
            "technical-agenda:d-02",
            "phrase:",
        ),
        (
            "Set the amount using currency exponent and avoid rounding errors.",
            "technical-agenda:d-05",
            "distinctive-token:",
        ),
    ],
)
def test_ar_ev_003_retrieval_is_deterministic_ranked_and_inspectable(
    turn: str, expected: str, feature_prefix: str
):
    index, _ = evidence_index()
    first = DeterministicEvidenceRetriever(index).retrieve(turn)
    second = DeterministicEvidenceRetriever(index).retrieve(turn)
    assert first == second
    assert first.outcome == RetrievalOutcome.READY
    assert first.selected_aliases == (expected,)
    assert first.candidates[0].alias == expected
    assert first.candidates[0].score > first.candidates[1].score
    assert any(
        feature.startswith(feature_prefix)
        for feature in first.candidates[0].matched_features
    )
    assert first.candidates[0].text == index.unit_for(expected).text


def test_ar_ev_003_ambiguous_selection_clarifies_before_zero_provider_calls():
    index, _ = evidence_index()
    calls = []

    async def provider_call(candidates):
        calls.append(candidates)
        return "must-not-run"

    candidates, result = asyncio.run(
        EvidenceSelectionGate(DeterministicEvidenceRetriever(index)).dispatch_when_ready(
            "Should we settle the date or timezone decision?", provider_call
        )
    )
    assert result is None
    assert calls == []
    assert candidates.outcome == RetrievalOutcome.NEEDS_CLARIFICATION
    assert candidates.selected_aliases == ()
    assert candidates.clarification is not None
    assert len(candidates.clarification.split()) <= 25
    assert candidates.candidates[0].score == candidates.candidates[1].score
    assert all(
        candidate.text and candidate.display_label for candidate in candidates.candidates
    )


def test_ar_ev_005_alias_materialization_rejects_unknown_duplicate_unselected_and_cross_case():
    index, current = evidence_index()
    materializer = AliasMaterializer(index)
    common = {
        "case_id": CASE_ID,
        "selected_aliases": ("technical-agenda:d-01",),
        "current_snapshots": (current,),
    }
    with pytest.raises(ValueError, match="unknown evidence alias"):
        materializer.materialize(
            **common, requested_aliases=("technical-agenda:d-99",)
        )
    with pytest.raises(ValueError, match="duplicate evidence alias"):
        materializer.materialize(
            **common,
            requested_aliases=("technical-agenda:d-01", "technical-agenda:d-01"),
        )
    with pytest.raises(ValueError, match="was not selected"):
        materializer.materialize(
            **common, requested_aliases=("technical-agenda:d-02",)
        )
    with pytest.raises(ValueError, match="cross-case evidence access"):
        materializer.materialize(
            case_id=OTHER_CASE_ID,
            requested_aliases=("technical-agenda:d-01",),
            selected_aliases=("technical-agenda:d-01",),
            current_snapshots=(current,),
        )
    other_case = snapshot(current.content, case_id=OTHER_CASE_ID)
    with pytest.raises(ValueError, match="cross-case source snapshot"):
        materializer.materialize(
            case_id=CASE_ID,
            requested_aliases=("technical-agenda:d-01",),
            selected_aliases=("technical-agenda:d-01",),
            current_snapshots=(other_case,),
        )


def test_feature_21_reads_only_closed_source_catalog_and_has_no_provider_runtime_surface(monkeypatch):
    reads: list[Path] = []
    original = Path.read_bytes

    def observed(path: Path) -> bytes:
        reads.append(path)
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", observed)
    catalog = SourceCatalog(ROOT)
    content = catalog.read_text(SourceName.TECHNICAL_SPEC)
    EvidenceIndexer().build(case_id=CASE_ID, snapshots=(snapshot(content),))
    excluded = ROOT / "docs/technical-specs/filtered-orders-csv-export-technical-contract.md"
    assert excluded not in reads
    assert set(reads) == {catalog.document(SourceName.TECHNICAL_SPEC).path}

    module = Path(__file__).parents[2] / "src/specops_workshop/evidence.py"
    source = module.read_text(encoding="utf-8").casefold()
    assert all(
        forbidden not in source
        for forbidden in (
            "openai", "api_key", "credential", "http://", "https://", "jira", "github"
        )
    )
