from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict

from specops_workflow import Clock, WorkflowService, migrate
from specops_workflow.enums import Domain, SourceArtifactType
from specops_workflow.models import (
    CreateCaseCommand,
    GrantDelegationCommand,
    RegisterSourceArtifactCommand,
    SourceArtifactIdentity,
)

from .config import Settings
from .delegation import DEV_LEAD_ACTOR_ID, PM_ACTOR_ID, DelegationFixture
from .sources import SourceCatalog, SourceName


IDENTITY_NAMESPACE = UUID("1bedb460-265a-598c-94f1-d9bc21a79f70")


def stable_id(name: str) -> UUID:
    return uuid5(IDENTITY_NAMESPACE, name)


CASE_ID = stable_id("csv-export-workshop:case")
DELEGATION_ID = stable_id("csv-export-workshop:delegation")
PM_SOURCE_ID = stable_id("csv-export-workshop:source:pm-spec")
TECHNICAL_SOURCE_ID = stable_id("csv-export-workshop:source:technical-spec")


class BootstrapView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    case_id: UUID
    pm_actor_id: UUID
    dev_lead_actor_id: UUID
    delegation_id: UUID
    delegation_valid_from: datetime
    delegation_valid_until: datetime
    delegation_command_scope: tuple[str, ...]
    pm_source_id: UUID
    technical_source_id: UUID
    technical_source_lines: tuple[tuple[int, str], ...]


def bootstrap_foundation(
    settings: Settings,
    catalog: SourceCatalog,
    fixture: DelegationFixture,
    *,
    clock: Clock,
) -> tuple[WorkflowService, BootstrapView]:
    migrate(settings.specops_database_url)
    service = WorkflowService(clock=clock, database_url=settings.specops_database_url)
    created = service.create_case(
        CreateCaseCommand(
            command_id=stable_id("command:create-case"),
            case_id=CASE_ID,
            acting_actor_id=PM_ACTOR_ID,
            pm_actor_id=PM_ACTOR_ID,
            dev_lead_actor_id=DEV_LEAD_ACTOR_ID,
        )
    )
    delegation = service.grant_delegation(
        GrantDelegationCommand(
            command_id=stable_id("command:grant-delegation"),
            case_id=CASE_ID,
            acting_actor_id=DEV_LEAD_ACTOR_ID,
            expected_case_revision=created.stored_result.receipt.revision if not created.mutated else created.receipt.revision,
            delegation_id=DELEGATION_ID,
            delegate_id=PM_ACTOR_ID,
            domain=Domain.TECHNICAL,
            command_names=list(fixture.command_scope),
            valid_from=fixture.valid_from,
            valid_until=fixture.valid_until,
            later_review_required=True,
        )
    )
    revision = delegation.stored_result.receipt.revision if not delegation.mutated else delegation.receipt.revision
    pm_source = _register(
        service,
        catalog,
        SourceName.PM_SPEC,
        source_id=PM_SOURCE_ID,
        source_type=SourceArtifactType.BUSINESS_SPEC,
        actor="SYSTEM",
        revision=revision,
    )
    revision = pm_source.stored_result.receipt.revision if not pm_source.mutated else pm_source.receipt.revision
    _register(
        service,
        catalog,
        SourceName.TECHNICAL_SPEC,
        source_id=TECHNICAL_SOURCE_ID,
        source_type=SourceArtifactType.TECHNICAL_CONTRACT,
        actor=PM_ACTOR_ID,
        revision=revision,
    )
    return service, BootstrapView(
        case_id=CASE_ID,
        pm_actor_id=PM_ACTOR_ID,
        dev_lead_actor_id=DEV_LEAD_ACTOR_ID,
        delegation_id=DELEGATION_ID,
        delegation_valid_from=fixture.valid_from,
        delegation_valid_until=fixture.valid_until,
        delegation_command_scope=fixture.command_scope,
        pm_source_id=PM_SOURCE_ID,
        technical_source_id=TECHNICAL_SOURCE_ID,
        technical_source_lines=tuple(catalog.numbered_lines(SourceName.TECHNICAL_SPEC)),
    )


def _register(
    service: WorkflowService,
    catalog: SourceCatalog,
    name: SourceName,
    *,
    source_id: UUID,
    source_type: SourceArtifactType,
    actor: UUID | str,
    revision: int,
):
    document = catalog.document(name)
    return service.register_source_artifact(
        RegisterSourceArtifactCommand(
            command_id=stable_id(f"command:register:{name.value.lower()}"),
            case_id=CASE_ID,
            acting_actor_id=actor,
            expected_case_revision=revision,
            identity=SourceArtifactIdentity(
                artifact_id=source_id,
                case_id=CASE_ID,
                type=source_type,
                version=1,
                media_type=document.media_type,
                canonical_locator=str(document.path),
                content_hash=catalog.digest(name),
            ),
        )
    )
