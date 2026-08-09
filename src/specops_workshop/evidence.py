from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from typing import Annotated, Generic, Literal, TypeVar
from uuid import UUID

from pydantic import Field, model_validator

from specops_workflow.enums import SourceArtifactType
from specops_workflow.models import LineRange, SourceArtifactIdentity, SourceRef

from .contracts import WorkshopModel


EvidenceAlias = Annotated[
    str,
    Field(pattern=r"^[a-z][a-z0-9-]{0,63}:[a-z][a-z0-9-]{0,63}$"),
]
Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
RETRIEVAL_POLICY_VERSION = "agenda-retrieval-v1"


class EvidenceSourceRole(StrEnum):
    PM_SPEC = "PM_SPEC"
    TECHNICAL_SPEC = "TECHNICAL_SPEC"


class EvidenceUnitKind(StrEnum):
    DECISION_AGENDA_ROW = "DECISION_AGENDA_ROW"


class RetrievalOutcome(StrEnum):
    READY = "READY"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"


class RegisteredMarkdownSnapshot(WorkshopModel):
    """Exact local bytes-as-text paired with a registered source identity."""

    source_role: EvidenceSourceRole
    case_id: UUID
    artifact_id: UUID
    version: int = Field(ge=1)
    content_hash: Hash
    media_type: Literal["text/markdown"]
    registered_at: datetime
    content: str = Field(min_length=1, max_length=250_000, exclude=True, repr=False)

    @model_validator(mode="after")
    def exact_registered_markdown(self) -> RegisteredMarkdownSnapshot:
        if self.media_type != "text/markdown":
            raise ValueError("evidence snapshots must be registered Markdown")
        actual = sha256(self.content.encode("utf-8")).hexdigest()
        if actual != self.content_hash:
            raise ValueError("source snapshot content hash mismatch")
        return self

    @classmethod
    def from_registered(
        cls,
        source_role: EvidenceSourceRole,
        identity: SourceArtifactIdentity,
        content: str,
    ) -> RegisteredMarkdownSnapshot:
        if identity.registered_at is None:
            raise ValueError("source artifact is not registered")
        expected_type = {
            EvidenceSourceRole.PM_SPEC: SourceArtifactType.BUSINESS_SPEC,
            EvidenceSourceRole.TECHNICAL_SPEC: SourceArtifactType.TECHNICAL_CONTRACT,
        }[source_role]
        if identity.type != expected_type:
            raise ValueError("source role does not match registered artifact type")
        return cls(
            source_role=source_role,
            case_id=identity.case_id,
            artifact_id=identity.artifact_id,
            version=identity.version,
            content_hash=identity.content_hash,
            media_type=identity.media_type,
            registered_at=identity.registered_at,
            content=content,
        )

    @property
    def lines(self) -> tuple[str, ...]:
        return tuple(self.content.splitlines())


class EvidenceUnit(WorkshopModel):
    """Provider-visible evidence semantics; authoritative addressing is absent."""

    alias: EvidenceAlias
    source_role: EvidenceSourceRole
    unit_kind: EvidenceUnitKind
    heading_path: tuple[str, ...] = Field(min_length=1, max_length=20)
    display_label: str = Field(min_length=1, max_length=240)
    text: str = Field(min_length=1, max_length=20_000)


class EvidenceBinding(WorkshopModel):
    """Local-only authoritative binding for one provider-visible alias."""

    alias: EvidenceAlias
    source_role: EvidenceSourceRole
    case_id: UUID
    artifact_id: UUID
    version: int = Field(ge=1)
    content_hash: Hash
    media_type: Literal["text/markdown"]
    location: LineRange

    @model_validator(mode="after")
    def exact_markdown_location(self) -> EvidenceBinding:
        if self.media_type != "text/markdown":
            raise ValueError("line evidence must bind registered Markdown")
        return self


class SourceSnapshotSeal(WorkshopModel):
    source_role: EvidenceSourceRole
    case_id: UUID
    artifact_id: UUID
    version: int = Field(ge=1)
    content_hash: Hash
    media_type: Literal["text/markdown"]
    line_count: int = Field(ge=1)


class EvidenceIndex(WorkshopModel):
    """Validated local index. Bindings and source seals never serialize outward."""

    case_id: UUID
    units: tuple[EvidenceUnit, ...] = Field(min_length=1, max_length=500)
    bindings: tuple[EvidenceBinding, ...] = Field(
        min_length=1, max_length=500, exclude=True, repr=False
    )
    source_seals: tuple[SourceSnapshotSeal, ...] = Field(
        min_length=1, max_length=10, exclude=True, repr=False
    )

    @model_validator(mode="after")
    def closed_aliases_and_bindings(self) -> EvidenceIndex:
        unit_aliases = [unit.alias for unit in self.units]
        binding_aliases = [binding.alias for binding in self.bindings]
        if len(unit_aliases) != len(set(unit_aliases)):
            raise ValueError("duplicate evidence alias")
        if len(binding_aliases) != len(set(binding_aliases)):
            raise ValueError("duplicate evidence binding alias")
        if set(unit_aliases) != set(binding_aliases):
            raise ValueError("evidence units and bindings must have identical aliases")
        if any(binding.case_id != self.case_id for binding in self.bindings):
            raise ValueError("cross-case evidence binding")
        seals = {
            (seal.artifact_id, seal.version): seal for seal in self.source_seals
        }
        if len(seals) != len(self.source_seals):
            raise ValueError("duplicate registered source snapshot")
        if any(seal.case_id != self.case_id for seal in self.source_seals):
            raise ValueError("cross-case source snapshot")
        for binding in self.bindings:
            seal = seals.get((binding.artifact_id, binding.version))
            if seal is None:
                raise ValueError("evidence binding references an unregistered snapshot")
            if (
                seal.content_hash != binding.content_hash
                or seal.media_type != binding.media_type
            ):
                raise ValueError("evidence binding does not match registered snapshot")
            unit = next(value for value in self.units if value.alias == binding.alias)
            if (
                unit.source_role != binding.source_role
                or seal.source_role != binding.source_role
            ):
                raise ValueError("evidence source role does not match registered snapshot")
            if binding.location.end > seal.line_count:
                raise ValueError("evidence binding range exceeds registered snapshot")
        return self

    def unit_for(self, alias: str) -> EvidenceUnit:
        try:
            return next(unit for unit in self.units if unit.alias == alias)
        except StopIteration as error:
            raise ValueError("unknown evidence alias") from error


class RankedEvidenceCandidate(WorkshopModel):
    alias: EvidenceAlias
    score: int = Field(ge=0, le=10_000)
    matched_features: tuple[str, ...] = Field(max_length=100)
    display_label: str = Field(min_length=1, max_length=240)
    text: str = Field(min_length=1, max_length=20_000)


class CandidateEvidenceSet(WorkshopModel):
    retrieval_policy_version: str = Field(min_length=1, max_length=64)
    final_turn_fingerprint: Hash
    candidates: tuple[RankedEvidenceCandidate, ...] = Field(min_length=1, max_length=20)
    selected_aliases: tuple[EvidenceAlias, ...] = Field(max_length=5)
    outcome: RetrievalOutcome
    clarification: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def exact_outcome_shape(self) -> CandidateEvidenceSet:
        ranks = [(candidate.score, candidate.alias) for candidate in self.candidates]
        if ranks != sorted(ranks, key=lambda value: (-value[0], value[1])):
            raise ValueError("evidence candidates must be deterministically ranked")
        candidate_aliases = {candidate.alias for candidate in self.candidates}
        if len(candidate_aliases) != len(self.candidates):
            raise ValueError("candidate aliases must be unique")
        if len(self.selected_aliases) != len(set(self.selected_aliases)):
            raise ValueError("selected aliases must be unique")
        if not set(self.selected_aliases).issubset(candidate_aliases):
            raise ValueError("selected alias is not a retrieved candidate")
        if self.outcome == RetrievalOutcome.READY:
            if not self.selected_aliases or self.clarification is not None:
                raise ValueError("READY requires selected evidence and no clarification")
        else:
            if self.selected_aliases or self.clarification is None:
                raise ValueError("NEEDS_CLARIFICATION cannot select evidence")
            if (
                len(self.clarification.split()) > 25
                or not self.clarification.rstrip().endswith("?")
                or self.clarification.count("?") != 1
            ):
                raise ValueError("clarification must be one focused question of at most 25 words")
        return self


class EvidenceIndexer:
    _agenda_id = re.compile(r"D-[0-9]{2}", re.IGNORECASE)
    _heading = re.compile(r"^(#{1,6})\s+(.+?)\s*$")

    def build(
        self,
        *,
        case_id: UUID,
        snapshots: Iterable[RegisteredMarkdownSnapshot],
    ) -> EvidenceIndex:
        values = tuple(snapshots)
        if not values:
            raise ValueError("at least one registered source snapshot is required")
        if any(snapshot.case_id != case_id for snapshot in values):
            raise ValueError("cross-case source snapshot")
        snapshot_keys = [(value.artifact_id, value.version) for value in values]
        if len(snapshot_keys) != len(set(snapshot_keys)):
            raise ValueError("duplicate registered source snapshot")

        units: list[EvidenceUnit] = []
        bindings: list[EvidenceBinding] = []
        for snapshot in values:
            if snapshot.source_role == EvidenceSourceRole.TECHNICAL_SPEC:
                new_units, new_bindings = self._technical_agenda(snapshot)
                units.extend(new_units)
                bindings.extend(new_bindings)
        if not units:
            raise ValueError("registered technical decision agenda is missing")
        return EvidenceIndex(
            case_id=case_id,
            units=tuple(sorted(units, key=lambda value: value.alias)),
            bindings=tuple(sorted(bindings, key=lambda value: value.alias)),
            source_seals=tuple(
                SourceSnapshotSeal(
                    source_role=value.source_role,
                    case_id=value.case_id,
                    artifact_id=value.artifact_id,
                    version=value.version,
                    content_hash=value.content_hash,
                    media_type=value.media_type,
                    line_count=len(value.lines),
                )
                for value in values
            ),
        )

    def _technical_agenda(
        self, snapshot: RegisteredMarkdownSnapshot
    ) -> tuple[list[EvidenceUnit], list[EvidenceBinding]]:
        heading_path: list[str] = []
        units: list[EvidenceUnit] = []
        bindings: list[EvidenceBinding] = []
        for line_number, line in enumerate(snapshot.lines, start=1):
            heading = self._heading.match(line)
            if heading:
                level = len(heading.group(1))
                heading_path = heading_path[: level - 1]
                heading_path.append(heading.group(2))
                continue
            if not any("decision agenda" in value.casefold() for value in heading_path):
                continue
            columns = [value.strip() for value in line.strip().strip("|").split("|")]
            if len(columns) < 5 or self._agenda_id.fullmatch(columns[0]) is None:
                continue
            decision_id = columns[0].upper()
            alias = f"technical-agenda:{decision_id.lower()}"
            units.append(
                EvidenceUnit(
                    alias=alias,
                    source_role=EvidenceSourceRole.TECHNICAL_SPEC,
                    unit_kind=EvidenceUnitKind.DECISION_AGENDA_ROW,
                    heading_path=tuple(heading_path),
                    display_label=f"{decision_id} — {columns[1]}",
                    text=line,
                )
            )
            bindings.append(
                EvidenceBinding(
                    alias=alias,
                    source_role=EvidenceSourceRole.TECHNICAL_SPEC,
                    case_id=snapshot.case_id,
                    artifact_id=snapshot.artifact_id,
                    version=snapshot.version,
                    content_hash=snapshot.content_hash,
                    media_type=snapshot.media_type,
                    location=LineRange(start=line_number, end=line_number),
                )
            )
        return units, bindings


_TOKEN = re.compile(r"[a-z0-9]+")
_TURN_ID = re.compile(r"\bd\s*[-_]?\s*0*([0-9]{1,2})\b", re.IGNORECASE)
_STOPWORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "before", "by", "do",
        "does", "for", "from", "how", "if", "in", "is", "it", "of", "on",
        "or", "our", "should", "the", "this", "to", "use", "we", "what",
        "when", "which", "why", "with",
    }
)


def _normalized(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).casefold().split())


def _tokens(value: str) -> frozenset[str]:
    return frozenset(
        token for token in _TOKEN.findall(_normalized(value))
        if token not in _STOPWORDS and len(token) >= 3
    )


def evidence_turn_fingerprint(final_turn: str) -> str:
    normalized = _normalized(final_turn)
    if not normalized:
        raise ValueError("final PM turn must not be empty")
    return sha256(normalized.encode("utf-8")).hexdigest()


class DeterministicEvidenceRetriever:
    def __init__(
        self,
        evidence_index: EvidenceIndex,
        *,
        minimum_score: int = 5,
        minimum_margin: int = 3,
        maximum_candidates: int = 5,
    ) -> None:
        if minimum_score < 1 or minimum_margin < 1 or maximum_candidates < 2:
            raise ValueError("retrieval thresholds must be positive and inspectable")
        self._index = evidence_index
        self._minimum_score = minimum_score
        self._minimum_margin = minimum_margin
        self._maximum_candidates = maximum_candidates

    def retrieve(self, final_turn: str) -> CandidateEvidenceSet:
        normalized_turn = _normalized(final_turn)
        if not normalized_turn:
            raise ValueError("final PM turn must not be empty")
        turn_tokens = _tokens(final_turn)
        explicit_ids = {
            f"d-{int(match):02d}" for match in _TURN_ID.findall(normalized_turn)
        }
        unit_tokens = {unit.alias: _tokens(unit.text) for unit in self._index.units}
        frequencies = Counter(token for values in unit_tokens.values() for token in values)
        candidates: list[RankedEvidenceCandidate] = []
        for unit in self._index.units:
            features: list[str] = []
            score = 0
            decision_id = unit.alias.rsplit(":", 1)[1]
            if decision_id in explicit_ids:
                features.append(f"exact-id:{decision_id}")
                score += 100
            title = _normalized(unit.display_label.split("—", 1)[-1])
            if title and title in normalized_turn:
                features.append(f"phrase:{title}")
                score += 40
            distinctive = sorted(
                token for token in turn_tokens & unit_tokens[unit.alias]
                if frequencies[token] == 1
            )
            for token in distinctive:
                features.append(f"distinctive-token:{token}")
                score += 5
            if "agenda" in turn_tokens and any(
                "decision agenda" in heading.casefold() for heading in unit.heading_path
            ):
                features.append("heading:decision-agenda")
                score += 1
            candidates.append(
                RankedEvidenceCandidate(
                    alias=unit.alias,
                    score=score,
                    matched_features=tuple(features),
                    display_label=unit.display_label,
                    text=unit.text,
                )
            )
        ranked = tuple(
            sorted(candidates, key=lambda value: (-value.score, value.alias))[
                : self._maximum_candidates
            ]
        )
        top = ranked[0]
        runner_up_score = ranked[1].score if len(ranked) > 1 else 0
        ready = (
            top.score >= self._minimum_score
            and top.score - runner_up_score >= self._minimum_margin
        )
        clarification = None
        selected: tuple[str, ...] = ()
        outcome = RetrievalOutcome.NEEDS_CLARIFICATION
        if ready:
            selected = (top.alias,)
            outcome = RetrievalOutcome.READY
        else:
            first, second = ranked[:2]
            clarification = (
                "Which agenda decision should I ground: "
                f"{first.display_label} or {second.display_label}?"
            )
        return CandidateEvidenceSet(
            retrieval_policy_version=RETRIEVAL_POLICY_VERSION,
            final_turn_fingerprint=evidence_turn_fingerprint(final_turn),
            candidates=ranked,
            selected_aliases=selected,
            outcome=outcome,
            clarification=clarification,
        )


class AliasMaterializer:
    def __init__(self, evidence_index: EvidenceIndex) -> None:
        self._index = evidence_index

    def materialize(
        self,
        *,
        case_id: UUID,
        requested_aliases: Iterable[str],
        selected_aliases: Iterable[str],
        current_snapshots: Iterable[RegisteredMarkdownSnapshot],
    ) -> tuple[SourceRef, ...]:
        requested = tuple(requested_aliases)
        selected = tuple(selected_aliases)
        if not requested:
            raise ValueError("at least one evidence alias is required")
        if len(requested) != len(set(requested)) or len(selected) != len(set(selected)):
            raise ValueError("duplicate evidence alias")
        if case_id != self._index.case_id:
            raise ValueError("cross-case evidence access")
        known = {binding.alias: binding for binding in self._index.bindings}
        if any(alias not in known for alias in selected + requested):
            raise ValueError("unknown evidence alias")
        if not set(requested).issubset(selected):
            raise ValueError("evidence alias was not selected")

        snapshots = tuple(current_snapshots)
        if any(snapshot.case_id != case_id for snapshot in snapshots):
            raise ValueError("cross-case source snapshot")
        current = {
            (snapshot.artifact_id, snapshot.version): snapshot for snapshot in snapshots
        }
        if len(current) != len(snapshots):
            raise ValueError("duplicate registered source snapshot")
        units = {unit.alias: unit for unit in self._index.units}
        refs: list[SourceRef] = []
        for alias in requested:
            binding = known[alias]
            snapshot = current.get((binding.artifact_id, binding.version))
            if snapshot is None or snapshot.content_hash != binding.content_hash:
                raise ValueError("stale evidence source snapshot")
            if snapshot.media_type != binding.media_type:
                raise ValueError("evidence source media type changed")
            if snapshot.source_role != binding.source_role:
                raise ValueError("evidence source role changed")
            if binding.location.end > len(snapshot.lines):
                raise ValueError("evidence binding range exceeds current snapshot")
            exact_text = "\n".join(
                snapshot.lines[binding.location.start - 1 : binding.location.end]
            )
            if exact_text != units[alias].text:
                raise ValueError("evidence text does not match authoritative location")
            refs.append(
                SourceRef(
                    artifact_id=binding.artifact_id,
                    version=binding.version,
                    content_hash=binding.content_hash,
                    location=binding.location,
                )
            )
        return tuple(refs)


ProviderValue = TypeVar("ProviderValue")


class EvidenceSelectionGate(Generic[ProviderValue]):
    """Calls a provider-neutral callback only after deterministic selection is READY."""

    def __init__(self, retriever: DeterministicEvidenceRetriever) -> None:
        self._retriever = retriever

    async def dispatch_when_ready(
        self,
        final_turn: str,
        provider_call: Callable[[CandidateEvidenceSet], Awaitable[ProviderValue]],
    ) -> tuple[CandidateEvidenceSet, ProviderValue | None]:
        candidates = self._retriever.retrieve(final_turn)
        if candidates.outcome != RetrievalOutcome.READY:
            return candidates, None
        return candidates, await provider_call(candidates)
