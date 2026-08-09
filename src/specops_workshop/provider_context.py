from __future__ import annotations

from .sources import SourceCatalog, SourceName


def build_analyzer_business_context(catalog: SourceCatalog) -> str:
    """Return only the authorized identity-free PM business specification."""
    return catalog.read_text(SourceName.PM_SPEC)
