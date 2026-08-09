from __future__ import annotations

from .bootstrap import BootstrapView
from .contracts import (
    AnalyzerSourceContext,
    ProviderAuthorityContext,
    ProviderSourceDocument,
)
from .sources import SourceCatalog, SourceName


def build_analyzer_source_context(
    catalog: SourceCatalog,
    bootstrap: BootstrapView,
) -> AnalyzerSourceContext:
    """Build the closed, explicitly authorized Terra source projection."""
    pm = catalog.document(SourceName.PM_SPEC)
    technical = catalog.document(SourceName.TECHNICAL_SPEC)
    return AnalyzerSourceContext(
        authority=ProviderAuthorityContext(
            case_id=bootstrap.case_id,
            pm_actor_id=bootstrap.pm_actor_id,
            dev_lead_actor_id=bootstrap.dev_lead_actor_id,
            technical_delegation_id=bootstrap.delegation_id,
            delegation_domain=bootstrap.delegation_domain,
            delegation_valid_from=bootstrap.delegation_valid_from,
            delegation_valid_until=bootstrap.delegation_valid_until,
            delegation_command_scope=bootstrap.delegation_command_scope,
            later_review_required=bootstrap.delegation_later_review_required,
        ),
        documents=(
            ProviderSourceDocument(
                source_name="PM_SPECS",
                artifact_id=bootstrap.pm_source_id,
                version=1,
                media_type="text/markdown",
                canonical_locator=str(pm.path),
                content_hash=catalog.digest(SourceName.PM_SPEC),
                content=catalog.read_text(SourceName.PM_SPEC),
            ),
            ProviderSourceDocument(
                source_name="DEV_LEAD_TECHNICAL_SPEC",
                artifact_id=bootstrap.technical_source_id,
                version=1,
                media_type="text/markdown",
                canonical_locator=str(technical.path),
                content_hash=catalog.digest(SourceName.TECHNICAL_SPEC),
                content=catalog.read_text(SourceName.TECHNICAL_SPEC),
            ),
        ),
    )
