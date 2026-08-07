from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from .enums import ArtifactKind
from .errors import DomainError, ErrorCode
from .models import ArtifactBinding


@dataclass(frozen=True)
class ArtifactVersion:
    version: int
    semantic_hash: str
    content_schema_version: int
    hash_schema_version: int
    payload: Any
    state: str


@dataclass
class ArtifactRoot:
    kind: ArtifactKind
    artifact_id: UUID
    versions: list[ArtifactVersion] = field(default_factory=list)

    @property
    def current(self) -> ArtifactVersion:
        if not self.versions: raise DomainError(ErrorCode.RECORD_NOT_FOUND)
        return self.versions[-1]

    @property
    def binding(self) -> ArtifactBinding:
        current = self.current
        return ArtifactBinding(artifact_kind=self.kind, artifact_id=self.artifact_id, version=current.version, semantic_hash=current.semantic_hash)

    def assert_binding(self, artifact_id: UUID, version: int, semantic_hash: str) -> None:
        if artifact_id != self.artifact_id or version != self.current.version or semantic_hash != self.current.semantic_hash:
            raise DomainError(ErrorCode.STALE_ARTIFACT_BINDING)

    def revise(self, version: ArtifactVersion) -> None:
        if version.semantic_hash == self.current.semantic_hash: raise DomainError(ErrorCode.NO_SEMANTIC_CHANGE)
        if version.version != self.current.version + 1: raise DomainError(ErrorCode.INVALID_TRANSITION)
        self.versions.append(version)

