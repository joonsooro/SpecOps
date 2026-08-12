from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from pathlib import Path


class SourceName(StrEnum):
    PM_SPEC = "PM_SPEC"
    TECHNICAL_SPEC = "TECHNICAL_SPEC"
    TECHNICAL_CONTRACT = "TECHNICAL_CONTRACT"
    DELEGATION_FIXTURE = "DELEGATION_FIXTURE"


@dataclass(frozen=True)
class SourceDocument:
    name: SourceName
    path: Path
    media_type: str


class SourceCatalog:
    """Closed source catalog; callers cannot supply filesystem paths."""

    def __init__(self, spec_eng_root: Path) -> None:
        root = spec_eng_root.resolve(strict=True)
        self._documents = {
            SourceName.PM_SPEC: SourceDocument(
                SourceName.PM_SPEC, root / "docs" / "PM_Specs.md", "text/markdown"
            ),
            SourceName.TECHNICAL_SPEC: SourceDocument(
                SourceName.TECHNICAL_SPEC,
                root / "docs" / "technical-specs" / "filtered-orders-csv-export-technical-spec.md",
                "text/markdown",
            ),
            SourceName.TECHNICAL_CONTRACT: SourceDocument(
                SourceName.TECHNICAL_CONTRACT,
                root
                / "docs"
                / "technical-specs"
                / "filtered-orders-csv-export-technical-contract.md",
                "text/markdown",
            ),
            SourceName.DELEGATION_FIXTURE: SourceDocument(
                SourceName.DELEGATION_FIXTURE,
                root / "fixtures" / "dev-lead-delegation-email.md",
                "text/markdown",
            ),
        }

    def document(self, name: SourceName) -> SourceDocument:
        return self._documents[name]

    def read_bytes(self, name: SourceName) -> bytes:
        document = self.document(name)
        return document.path.read_bytes()

    def read_text(self, name: SourceName) -> str:
        return self.read_bytes(name).decode("utf-8")

    def digest(self, name: SourceName) -> str:
        return sha256(self.read_bytes(name)).hexdigest()

    def numbered_lines(self, name: SourceName) -> list[tuple[int, str]]:
        return list(enumerate(self.read_text(name).splitlines(), start=1))
