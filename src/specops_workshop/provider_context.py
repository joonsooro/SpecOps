from __future__ import annotations

from .sources import SourceCatalog, SourceName


_PM_BUSINESS_CONTEXT = "\n".join((
    "Product: Orders Admin",
    "Feature: Export filtered orders to CSV.",
    "Business objective: authorized operations managers export every order matching the captured active filters for reconciliation and offline analysis.",
    "Business constraints: exports are complete, never silently truncated, use a fixed six-column CSV schema, preserve permission enforcement, and give clear progress or failure feedback.",
    "PM decisions: pagination never limits export contents; the organization business timezone is used with identified UTC fallback; dates are ISO 8601; currency and decimal total are separate fields.",
))


def build_analyzer_business_context(catalog: SourceCatalog) -> str:
    """Return a bounded PM semantic projection, never the source document."""

    # The PM document remains a locally registered foundation source. The
    # analyzer needs only this deliberate business projection; reading it here
    # would turn an allowed context into a full-document egress path.
    catalog.document(SourceName.PM_SPEC)
    return _PM_BUSINESS_CONTEXT
